import json
from contextlib import contextmanager

import pytest
from conftest import CID, IMG_CFG, NEW_IMG, make_ctr

from app import selfupdate as su
from app.selfupdate import SelfUpdateError, SelfUpdater, run_helper
from app.updater import UnhealthyError, Updater


class Notifier:
    def __init__(self):
        self.sent = []

    def notify(self, event, container, message, details=None):
        self.sent.append((event, container, message, details))

    @contextmanager
    def batch(self):
        yield


def updock_ctr(fake):
    c = make_ctr("updock", image="ghcr.io/me/updock:latest")
    c["Config"]["Labels"] = {"updock.self": "true", "com.docker.compose.project": "infra",
                             "com.docker.compose.service": "updock"}
    c["HostConfig"].update(PortBindings={"8080/tcp": [{"HostPort": "8080"}]},
                           PublishAllPorts=False, RestartPolicy={"Name": "unless-stopped"},
                           Binds=["/var/run/docker.sock:/var/run/docker.sock", "updock-data:/data"])
    c["Config"]["ExposedPorts"] = {"8080/tcp": {}}
    return fake.add(c)


def publish(fake):
    fake.images[NEW_IMG] = {"Id": NEW_IMG, "Config": IMG_CFG}
    fake.tags["ghcr.io/me/updock:latest"] = NEW_IMG
    fake.tags[NEW_IMG] = NEW_IMG


@pytest.fixture
def env(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "zzz")
    updock_ctr(fake)
    up, n = Updater(fake), Notifier()
    s = SelfUpdater(fake, up, n, data_dir=str(tmp_path), version="0.5.0", allowed=True,
                    self_ref="updock")
    return s, up, n, fake, tmp_path


def test_container_lookup(env, fake):
    s, *_ = env
    assert s.container()["Name"] == "/updock"
    assert SelfUpdater(fake, None, None, self_ref="missing").container() is None
    fake.add(make_ctr("plain", cid="p" * 64))
    assert SelfUpdater(fake, None, None, self_ref="plain").container() is None
    nobody = SelfUpdater(fake, None, None)
    nobody.self_ref = ""
    assert nobody.container() is None


def test_status_and_check(env):
    s, up, n, fake, _ = env
    st = s.status()
    assert st["found"] and st["name"] == "updock" and not st["update_available"]
    publish(fake)
    assert s.check() is True
    assert s.status()["update_available"] is True
    s.self_ref = "missing"
    assert "set UPDOCK_SELF" in s.status()["reason"]
    with pytest.raises(SelfUpdateError):
        s.check()


def test_preflight_refusals(env):
    s, up, n, fake, _ = env
    s.allowed = False
    with pytest.raises(SelfUpdateError, match="disabled"):
        s.preflight()
    s.allowed = True
    s.in_progress = True
    with pytest.raises(SelfUpdateError, match="already in progress"):
        s.preflight()
    s.in_progress = False
    up.run = {"kind": "check"}
    with pytest.raises(SelfUpdateError, match="a run is in progress"):
        s.preflight()
    up.run = None
    up.locks["web"].acquire()
    with pytest.raises(SelfUpdateError, match=r"jobs are running \(web\)"):
        s.preflight()
    up.locks["web"].release()
    s.self_ref = "nope"
    with pytest.raises(SelfUpdateError, match="UPDOCK_SELF"):
        s.preflight()


def test_start_creates_helper(env):
    s, up, n, fake, tmp = env
    publish(fake)
    fake.add(make_ctr("updock-self-update", cid="h" * 64))  # leftover from an earlier try
    assert s.start() == "updock-self-update"
    body = fake.created_body
    assert body["Image"] == NEW_IMG
    assert body["Cmd"] == ["python", "-m", "app.selfupdate", CID, "ghcr.io/me/updock:latest"]
    assert body["Entrypoint"] is None and body["Healthcheck"] == {"Test": ["NONE"]}
    assert "ExposedPorts" not in body
    hc = body["HostConfig"]
    assert "PortBindings" not in hc and hc["AutoRemove"] and hc["RestartPolicy"] == {"Name": "no"}
    assert "/var/run/docker.sock:/var/run/docker.sock" in hc["Binds"]
    assert body["Labels"]["updock.self"] == "true" and body["Labels"]["updock.self-helper"] == "true"
    assert not any(k.startswith("com.docker.compose.") for k in body["Labels"])
    assert ("net2", {}) in fake.connected  # extra network, without aliases
    res = json.loads((tmp / "self-update.json").read_text())
    assert res["status"] == "running" and res["from_version"] == "0.5.0" and s.in_progress
    assert s.pending()


def test_start_when_already_newest(env):
    s, up, n, fake, _ = env
    with pytest.raises(SelfUpdateError, match="already running the newest image"):
        s.start()
    assert not s.in_progress
    assert s.start_safely() is None and "newest image" in s.error
    assert s.status()["error"] == s.error


def test_helper_body_without_image_config(env):
    s, up, n, fake, _ = env
    ctr = fake.inspect("updock")
    fake.fail["image"] = RuntimeError("gone")
    body, extra = s.helper_body(ctr, NEW_IMG, "ref")
    assert body["Image"] == NEW_IMG


def run_report(s):
    return s.report()


@pytest.mark.parametrize("res,status,event_msg", [
    ({"status": "ok", "from_version": "0.5.0", "to_version": "0.6.0", "log": ["recreated"]},
     "ok", "Updock updated 0.5.0 -> 0.6.0"),
    ({"status": "rolled-back", "error": "not healthy"}, "rolled-back",
     "Updock self-update rolled back: not healthy"),
    ({"status": "failed"}, "failed", "Updock self-update failed: unknown error"),
])
def test_report_once(env, res, status, event_msg):
    s, up, n, fake, tmp = env
    (tmp / "self-update.json").write_text(json.dumps({"name": "updock", **res}))
    assert s.report() == status
    assert n.sent[0][0] == "self-update" and n.sent[0][2] == event_msg
    assert s.report() is None and len(n.sent) == 1  # reported only once
    assert not s.pending()


def test_report_waits_for_running_helper(env):
    s, up, n, fake, tmp = env
    path = tmp / "self-update.json"
    path.write_text(json.dumps({"status": "running", "started": 1e12}))
    clock = {"t": 1e12}
    s.clock = lambda: clock["t"]

    def sleep(sec):
        clock["t"] += sec
        path.write_text(json.dumps({"status": "ok", "started": 1e12}))
    s.sleep = sleep
    assert s.report(wait=30) == "ok"
    path.write_text(json.dumps({"status": "running", "started": 1e12}))
    s.sleep = lambda sec: clock.__setitem__("t", clock["t"] + sec)
    assert s.report(wait=10) is None  # still running when the wait is over


def test_report_stale_running_is_failed(env):
    s, up, n, fake, tmp = env
    (tmp / "self-update.json").write_text(json.dumps({"status": "running", "started": 0}))
    assert s.report() == "failed" and "without reporting" in n.sent[0][2]


def test_report_in_background_and_errors(env, monkeypatch, caplog):
    s, up, n, fake, tmp = env
    monkeypatch.setattr(su.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    monkeypatch.setattr(s, "report", lambda wait: (_ for _ in ()).throw(RuntimeError("bad")))
    s.report_in_background(5)
    assert "could not report the self-update result: bad" in caplog.text


def test_read_write_edge_cases(tmp_path, caplog):
    (tmp_path / "x.json").write_text("[1]")
    assert su._read(str(tmp_path / "x.json")) is None
    assert su._read(str(tmp_path / "missing.json")) is None
    su._write(None, {})
    su._write(str(tmp_path / "no" / "dir.json"), {})
    assert "could not write" in caplog.text
    assert SelfUpdater(None, None, None).result() is None


def test_run_helper_success(fake, tmp_path):
    updock_ctr(fake)
    publish(fake)
    fake.images[NEW_IMG]["Config"] = {**IMG_CFG, "Labels": {"org.opencontainers.image.version": "0.6.0"}}
    (tmp_path / "self-update.json").write_text(json.dumps({"status": "running", "name": "updock"}))
    code = run_helper([CID, "ghcr.io/me/updock:latest"], docker=fake,
                      env={"UPDOCK_DATA_DIR": str(tmp_path), "UPDOCK_STARTUP_GRACE": "0"})
    res = json.loads((tmp_path / "self-update.json").read_text())
    assert code == 0 and res["status"] == "ok" and res["to_version"] == "0.6.0"
    assert fake.ctrs["updock"]["Image"] == NEW_IMG and res["log"]


def test_run_helper_rollback_and_failure(fake, tmp_path, monkeypatch):
    updock_ctr(fake)
    publish(fake)

    class Failing(Updater):
        def _verify(self, *a, **k):
            raise UnhealthyError("not healthy after 120s")
    env = {"UPDOCK_DATA_DIR": str(tmp_path), "UPDOCK_STARTUP_GRACE": "0"}
    assert run_helper([CID, "ghcr.io/me/updock:latest"], docker=fake, updater_cls=Failing, env=env) == 1
    res = json.loads((tmp_path / "self-update.json").read_text())
    assert res["status"] == "rolled-back" and "not healthy" in res["error"]
    assert fake.ctrs["updock"]["Id"] == CID  # the old container is back
    assert run_helper(["missing", "x"], docker=fake, env=env) == 1
    res = json.loads((tmp_path / "self-update.json").read_text())
    assert res["status"] == "failed" and res["error"].startswith("KeyError")


def test_run_helper_usage(capsys):
    assert run_helper([]) == 2
    assert "usage" in capsys.readouterr().err


def test_start_pulls_after_digest_check(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "zzz")
    updock_ctr(fake)

    class Reg:
        def digest(self, ref):
            return "sha256:remote"

        def remote_info(self, ref, platform=None):
            return {"id": "new", "digest": "sha256:remote", "tags": [], "version": "0.6.0",
                    "revision": None, "created": None}
    up = Updater(fake, registry=Reg())
    real_pull = fake.pull

    def pull(ref, progress=None):
        publish(fake)
        return real_pull(ref, progress)
    monkeypatch.setattr(fake, "pull", pull)
    s = SelfUpdater(fake, up, Notifier(), data_dir=str(tmp_path), allowed=True, self_ref="updock")
    assert s.start() == "updock-self-update" and fake.pulled == "ghcr.io/me/updock:latest"
