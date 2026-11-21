import logging

import pytest

from app.updater import Updater, base_repo, build_create_body, compose_depends, compose_order, image_info
from conftest import CID, IMG_CFG, NEW_IMG, OLD_IMG, make_ctr


# ---------- build_create_body ----------

def test_body_strips_image_defaults():
    body, extra = build_create_body(make_ctr(), IMG_CFG)
    assert body["Env"] == ["USER_VAR=x"]
    assert "Cmd" not in body and "Entrypoint" not in body and "WorkingDir" not in body
    assert body["Labels"] == {"foo": "bar"}
    assert "Hostname" not in body and "MacAddress" not in body
    assert body["Image"] == "test/app:latest"


def test_body_keeps_user_overrides():
    ctr = make_ctr()
    ctr["Config"].update(Entrypoint=["/custom"], Cmd=["/run"], Hostname="myhost", WorkingDir="/w")
    body, _ = build_create_body(ctr, IMG_CFG)
    assert body["Entrypoint"] == ["/custom"]
    assert body["Cmd"] == ["/run"]  # kept because entrypoint overridden
    assert body["Hostname"] == "myhost" and body["WorkingDir"] == "/w"


def test_body_cmd_override_kept():
    ctr = make_ctr()
    ctr["Config"]["Cmd"] = ["/other"]
    body, _ = build_create_body(ctr, IMG_CFG)
    assert body["Cmd"] == ["/other"] and "Entrypoint" not in body


def test_body_no_image_cfg():
    body, _ = build_create_body(make_ctr(), None)
    assert body["Env"] == ["USER_VAR=x", "V=1"]
    assert body["Labels"] == {"foo": "bar", "img.label": "1"}


def test_body_anonymous_volumes_reattached():
    body, _ = build_create_body(make_ctr(), IMG_CFG)
    assert body["HostConfig"]["Mounts"] == [{"Type": "volume", "Source": "anonvol", "Target": "/anon"}]


def test_body_existing_mounts_respected():
    ctr = make_ctr()
    ctr["HostConfig"]["Mounts"] = [{"Type": "volume", "Source": "anonvol", "Target": "/anon"}]
    ctr["HostConfig"]["Binds"] = None
    ctr["Mounts"] = ctr["Mounts"][1:]  # only anon + bind
    body, _ = build_create_body(ctr, IMG_CFG)
    assert len(body["HostConfig"]["Mounts"]) == 1


def test_body_no_anon_volumes():
    ctr = make_ctr(Mounts=None)
    body, _ = build_create_body(ctr, IMG_CFG)
    assert body["HostConfig"]["Mounts"] is None


def test_body_links_converted():
    ctr = make_ctr()
    ctr["HostConfig"]["Links"] = ["/db:/web/database"]
    body, _ = build_create_body(ctr, IMG_CFG)
    assert body["HostConfig"]["Links"] == ["db:database"]


def test_body_networks_primary_and_extra():
    body, extra = build_create_body(make_ctr(), IMG_CFG)
    assert body["NetworkingConfig"] == {"EndpointsConfig": {"bridge": {}}}
    assert extra == [("net2", {"Aliases": ["webby"]})]


def test_body_default_mode_uses_bridge():
    ctr = make_ctr()
    ctr["HostConfig"]["NetworkMode"] = "default"
    body, extra = build_create_body(ctr, IMG_CFG)
    assert "bridge" in body["NetworkingConfig"]["EndpointsConfig"]


def test_body_custom_mode_not_in_nets_uses_first():
    ctr = make_ctr()
    ctr["HostConfig"]["NetworkMode"] = "abc123networkid"
    ctr["NetworkSettings"]["Networks"] = {"proj_default": {"Aliases": ["svc"]}}
    body, extra = build_create_body(ctr, IMG_CFG)
    assert body["NetworkingConfig"]["EndpointsConfig"] == {"proj_default": {"Aliases": ["svc"]}}
    assert extra == []


def test_body_no_networks():
    ctr = make_ctr()
    ctr["NetworkSettings"]["Networks"] = None
    body, extra = build_create_body(ctr, IMG_CFG)
    assert "NetworkingConfig" not in body and extra == []


@pytest.mark.parametrize("mode", ["host", "none"])
def test_body_host_none_mode(mode):
    ctr = make_ctr()
    ctr["HostConfig"]["NetworkMode"] = mode
    body, extra = build_create_body(ctr, IMG_CFG)
    assert "NetworkingConfig" not in body and extra == []


def test_body_container_mode_strips_hostname():
    ctr = make_ctr()
    ctr["HostConfig"]["NetworkMode"] = "container:vpn"
    ctr["Config"].update(Hostname="h", Domainname="d", ExposedPorts={"80/tcp": {}})
    body, extra = build_create_body(ctr, IMG_CFG)
    for k in ("Hostname", "Domainname", "ExposedPorts", "NetworkingConfig"):
        assert k not in body
    assert extra == []


# ---------- Updater ----------

@pytest.fixture
def up(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    return Updater(fake)


def publish_new(fake, ref="test/app:latest"):
    fake.images[NEW_IMG] = {"Id": NEW_IMG, "Config": IMG_CFG}
    fake.tags[ref] = NEW_IMG


def test_eligible_rules(up):
    assert up.eligible({"Id": CID, "Labels": None})
    assert not up.eligible({"Id": CID, "Labels": {"updock.enable": "false"}})
    assert not up.eligible({"Id": CID, "Labels": {"updock.self": "true"}})
    assert not up.eligible({"Id": "selfhost123", "Labels": {}})


def test_eligible_label_enable(fake):
    u = Updater(fake, label_enable=True)
    assert not u.eligible({"Id": CID, "Labels": {}})
    assert u.eligible({"Id": CID, "Labels": {"updock.enable": "true"}})


def test_list_sorted_with_state(up, fake):
    fake.add(make_ctr("zeta", cid="z" * 64))
    fake.add(make_ctr("alpha", cid="a" * 64))
    up.state["alpha"]["job"] = "up-to-date"
    names = [c["name"] for c in up.list()]
    assert names == ["alpha", "zeta"]
    assert up.list()[0]["job"] == "up-to-date"


def test_check_pinned(up, fake):
    fake.add(make_ctr(image="nginx@sha256:abc"))
    _, avail = up.check("web")
    assert not avail and up.state["web"]["job"] == "pinned-digest"
    assert "pull" not in fake.calls


def test_check_up_to_date_and_available(up, fake):
    fake.add(make_ctr())
    assert up.check("web")[1] is False
    assert up.state["web"]["job"] == "up-to-date"
    publish_new(fake)
    assert up.check("web")[1] is True
    assert up.state["web"]["job"] == "update-available"
    assert fake.pulled == "test/app:latest"


def test_check_only_logs_and_errors(up, fake):
    fake.add(make_ctr())
    up.check_only("web")
    assert up.logs[-1]["msg"] == "up to date" and up.logs[-1]["container"] == "web"
    publish_new(fake)
    up.check_only("web")
    assert up.logs[-1]["msg"] == "update available"
    fake.fail["pull"] = RuntimeError("net down")
    up.check_only("web")
    assert up.state["web"]["job"] == "error" and up.logs[-1]["level"] == "ERROR"


def test_check_only_skips_when_locked(up, fake):
    fake.add(make_ctr())
    up.locks["web"].acquire()
    up.check_only("web")
    assert fake.calls == []


def test_update_noop_when_current(up, fake):
    fake.add(make_ctr())
    up.update("web")
    assert "create" not in fake.calls and up.logs[-1]["msg"] == "up to date" and up.logs[-1]["container"] == "web"


def test_update_full_flow(up, fake):
    fake.add(make_ctr())
    publish_new(fake)
    up.update("web")
    assert up.state["web"]["job"] == "updated"
    assert fake.ctrs["web"]["Image"] == NEW_IMG
    assert "web-updock-old" not in fake.ctrs
    assert fake.connected == [("net2", {"Aliases": ["webby"]})]
    assert fake.removed == [OLD_IMG] and OLD_IMG not in fake.images
    assert fake.stop_timeout == 10
    assert up.logs[-1]["msg"] == "updated" and up.logs[-1]["event"] == "update"


def test_update_force_recreate_no_cleanup_same_image(up, fake):
    fake.add(make_ctr())
    up.update("web", force=True)
    assert "create" in fake.calls and "remove_image" not in fake.calls
    assert up.logs[-1]["msg"] == "recreated"


def test_update_force_recreates_when_pull_fails(up, fake):
    fake.add(make_ctr())
    fake.fail["pull"] = RuntimeError("registry down")
    up.update("web", force=True)
    assert up.state["web"]["job"] == "updated" and "create" in fake.calls
    assert any("pull failed" in entry["msg"] for entry in up.logs)


def test_update_pull_failure_without_force_errors(up, fake):
    fake.add(make_ctr())
    fake.fail["pull"] = RuntimeError("registry down")
    up.update("web")
    assert up.state["web"]["job"] == "error" and "create" not in fake.calls


def test_update_cleanup_disabled(fake):
    u = Updater(fake, cleanup=False)
    fake.add(make_ctr())
    publish_new(fake)
    u.update("web")
    assert "remove_image" not in fake.calls


def test_update_cleanup_image_in_use_ignored(up, fake):
    fake.add(make_ctr())
    publish_new(fake)
    fake.fail["remove_image"] = RuntimeError("in use")
    up.update("web")
    assert up.state["web"]["job"] == "updated"


def test_update_stopped_container_not_started(up, fake):
    ctr = make_ctr(State={"Running": False})
    ctr["Config"]["StopTimeout"] = 30
    fake.add(ctr)
    publish_new(fake)
    up.update("web")
    assert "stop" not in fake.calls and "start" not in fake.calls


def test_update_custom_stop_timeout(up, fake):
    ctr = make_ctr()
    ctr["Config"]["StopTimeout"] = 30
    fake.add(ctr)
    publish_new(fake)
    up.update("web")
    assert fake.stop_timeout == 30


def test_update_old_image_inspect_fails(up, fake):
    fake.add(make_ctr())
    publish_new(fake)
    orig = fake.image
    fake.image = lambda ref: (_ for _ in ()).throw(KeyError(ref)) if ref == OLD_IMG else orig(ref)
    up.update("web")
    assert fake.created_body["Env"] == ["USER_VAR=x", "V=1"]


@pytest.mark.parametrize("op", ["create", "connect", "start"])
def test_update_rollback(up, fake, op):
    fake.add(make_ctr())
    publish_new(fake)
    fake.fail[op] = RuntimeError(f"{op} failed")
    up.update("web")
    assert up.state["web"]["job"] == "error"
    assert fake.ctrs["web"]["Id"] == CID  # old container restored
    assert set(fake.ctrs) == {"web"}
    assert any(f"{op} failed; rolling back" in e["msg"] for e in up.logs)
    assert fake.calls.count("start") == (2 if op == "start" else 1)


def test_update_rollback_stopped_container(up, fake):
    fake.add(make_ctr(State={"Running": False}))
    publish_new(fake)
    fake.fail["create"] = RuntimeError("x")
    up.update("web")
    assert "start" not in fake.calls and fake.ctrs["web"]["Id"] == CID


def test_update_skips_when_locked(up, fake, caplog):
    fake.add(make_ctr())
    up.locks["web"].acquire()
    with caplog.at_level(logging.WARNING, logger="updock"):
        up.update("web")
    assert fake.calls == [] and "already running" in up.logs[-1]["msg"]


def test_run_all(up, fake, monkeypatch):
    fake.add(make_ctr())
    skip = make_ctr("skip", cid="s" * 64)
    skip["Config"]["Labels"] = {"updock.enable": "false"}
    fake.add(skip)
    seen = []
    monkeypatch.setattr(up, "update", lambda n, scheduled=False: seen.append(("u", n, scheduled)))
    monkeypatch.setattr(up, "check_only", lambda n, scheduled=False: seen.append(("c", n, scheduled)))
    up.run_all()
    up.run_all(update=False)
    assert seen == [("u", "web", True), ("c", "web", True)]
    assert up.logs[-1]["msg"].startswith("run finished in ")


def test_body_image_ref_override():
    body, _ = build_create_body(make_ctr(), IMG_CFG, "test/app:1.0")
    assert body["Image"] == "test/app:1.0"


# ---------- image info / listing ----------

def test_image_info_labels_and_digest():
    info = image_info({"Id": "sha256:" + "a" * 64, "RepoDigests": ["x@sha256:" + "d" * 64],
                       "RepoTags": ["x:1"], "Created": "2026-01-01T00:00:00Z",
                       "Config": {"Labels": {"org.opencontainers.image.version": "1.2.3",
                                             "org.opencontainers.image.revision": "f" * 40}}})
    assert info == {"id": "a" * 12, "digest": "sha256:" + "d" * 64, "tags": ["x:1"],
                    "version": "1.2.3", "revision": "f" * 12, "created": "2026-01-01T00:00:00Z"}


def test_image_info_fallback_labels_and_missing():
    info = image_info({"Id": "sha256:b", "Config": {"Labels": {"build_version": "v9"}}})
    assert info["version"] == "v9" and info["digest"] is None and info["revision"] is None
    assert image_info({"Id": "sha256:c", "Config": None})["version"] is None


def test_base_repo():
    assert base_repo("ghcr.io/o/app:1") == "ghcr.io/o/app"
    assert base_repo("nginx@sha256:abc") == "nginx"


def test_list_shows_current_latest_and_ref(up, fake):
    fake.add(make_ctr())
    publish_new(fake)
    up.check("web")
    row = up.list()[0]
    assert row["image"] == "test/app:latest" and "ref" not in row
    assert row["current"]["id"] == "old" and row["latest"]["id"] == "new"
    assert row["update_available"] is True


def test_list_image_info_cached_and_missing(up, fake, monkeypatch):
    fake.add(make_ctr())
    up.list()
    calls = []
    monkeypatch.setattr(fake, "image", lambda r: calls.append(r))
    up.list()
    assert calls == []  # cached
    fake.ctrs["web"]["Image"] = "sha256:gone"
    fake.images.pop("sha256:gone", None)
    monkeypatch.undo()
    assert up.list()[0]["current"] is None


# ---------- old image cleanup ----------

def test_cleanup_untags_same_repo_tags(up, fake):
    fake.add(make_ctr())
    fake.tags["test/app:1.0"] = OLD_IMG  # old image also carries a version tag
    publish_new(fake)
    up.update("web")
    assert "test/app:1.0" not in fake.tags and OLD_IMG not in fake.images
    assert fake.removed == ["test/app:1.0", OLD_IMG]
    assert any("removed old image" in entry["msg"] for entry in up.logs)


def test_cleanup_keeps_image_used_by_other_container(up, fake):
    fake.add(make_ctr())
    fake.add(make_ctr("other", cid="o" * 64))
    publish_new(fake)
    up.update("web")
    assert OLD_IMG in fake.images
    assert any("used by other" in entry["msg"] for entry in up.logs)


def test_cleanup_keeps_image_with_foreign_tag(up, fake):
    fake.add(make_ctr())
    fake.tags["mirror/app:keep"] = OLD_IMG
    publish_new(fake)
    up.update("web")
    assert OLD_IMG in fake.images
    assert any("also tagged mirror/app:keep" in entry["msg"] for entry in up.logs)


def test_cleanup_reports_unexpected_errors(up, fake):
    from app.docker_api import DockerError
    fake.add(make_ctr())
    publish_new(fake)
    fake.fail["remove_image"] = DockerError(409, "conflict")
    up.update("web")
    assert up.state["web"]["job"] == "updated"
    assert any("could not remove old image" in entry["msg"] for entry in up.logs)


def test_remove_old_image_tolerates_404_after_untag(up, fake, monkeypatch):
    from app.docker_api import DockerError
    fake.tags["test/app:1.0"] = OLD_IMG
    real = fake.remove_image

    def rm(ref):
        if ref == OLD_IMG:
            raise DockerError(404, "gone")
        real(ref)
    monkeypatch.setattr(fake, "remove_image", rm)
    assert up.remove_old_image(OLD_IMG, "test/app:latest") is True


# ---------- tags / deploy ----------

class FakeRegistry:
    def __init__(self, tags):
        self._tags = tags
        self.refs = []

    def tags(self, ref):
        self.refs.append(ref)
        return self._tags


def test_tags_lists_registry(fake):
    fake.add(make_ctr())
    u = Updater(fake, registry=FakeRegistry(["2.0", "1.0"]))
    assert u.tags("web") == {"image": "test/app", "current": "latest", "tags": ["2.0", "1.0"]}


def test_tags_without_registry(up, fake):
    fake.add(make_ctr())
    with pytest.raises(RuntimeError):
        up.tags("web")


def test_deploy_older_tag_keeps_previous_image(up, fake):
    fake.add(make_ctr())
    fake.images["sha256:v1"] = {"Id": "sha256:v1", "Config": IMG_CFG}
    fake.tags["test/app:1.0"] = "sha256:v1"
    up.deploy("web", "1.0")
    assert fake.pulled == "test/app:1.0"
    assert fake.created_body["Image"] == "test/app:1.0"
    assert fake.ctrs["web"]["Image"] == "sha256:v1"
    assert OLD_IMG in fake.images and "remove_image" not in fake.calls
    st = up.state["web"]
    assert st["job"] == "deployed" and st["ref"] == "test/app:1.0"
    assert up.list()[0]["image"] == "test/app:1.0"


def test_deploy_from_digest_pinned(up, fake):
    fake.add(make_ctr(image="test/app@sha256:abc"))
    fake.tags["test/app:2.0"] = OLD_IMG
    up.deploy("web", "2.0")
    assert fake.created_body["Image"] == "test/app:2.0"


def test_deploy_pull_failure(up, fake):
    fake.add(make_ctr())
    fake.fail["pull"] = RuntimeError("manifest unknown")
    up.deploy("web", "nope")
    assert up.state["web"]["job"] == "error" and "create" not in fake.calls


def test_errors_logged_with_container_event_and_traceback(up, fake):
    fake.add(make_ctr())
    fake.fail["pull"] = RuntimeError("registry down")
    up.check_only("web")
    e = up.logs[-1]
    assert e["level"] == "ERROR" and e["container"] == "web" and e["event"] == "error"
    assert e["msg"] == "RuntimeError: registry down" and "Traceback" in e["exc"]


def test_recreate_steps_logged_at_debug(up, fake):
    fake.add(make_ctr())
    publish_new(fake)
    up.update("web")
    debug = [e["msg"] for e in up.logs if e["level"] == "DEBUG"]
    assert "pulling test/app:latest" in debug and "started" in debug
    assert any(m.startswith("created ") for m in debug)
    assert any(m == "connected to network net2" for m in debug)


def test_expected_docker_errors_logged_without_traceback(up, fake):
    from app.docker_api import DockerError
    fake.add(make_ctr())
    fake.fail["pull"] = DockerError(404, "manifest unknown")
    up.check_only("web")
    assert up.logs[-1]["msg"] == "DockerError: 404: manifest unknown"
    assert "exc" not in up.logs[-1]


# ---------- network-namespace dependents ----------

def add_dependent(fake, name="app", mode=f"container:{CID}", running=True):
    dep = make_ctr(name, cid=name[0] * 64, State={"Running": running})
    dep["HostConfig"]["NetworkMode"] = mode
    return fake.add(dep)


def test_dependent_recreated_on_parent_update(up, fake):
    fake.add(make_ctr())
    add_dependent(fake)
    publish_new(fake)
    up.update("web")
    new_parent = fake.ctrs["web"]["Id"]
    dep = fake.ctrs["app"]
    assert dep["Id"] != "a" * 64
    assert dep["HostConfig"]["NetworkMode"] == f"container:{new_parent}"
    assert "NetworkingConfig" not in fake.created_body
    assert fake.stopped.index("a" * 64) < fake.stopped.index(CID)  # dependent stopped first
    assert dep["Id"] in fake.started
    assert up.state["app"]["job"] == "recreated"
    assert any(e["container"] == "app" and e["event"] == "dependent"
               and "join web" in e["msg"] for e in up.logs)


def test_dependent_by_name_and_stopped_not_started(up, fake):
    fake.add(make_ctr())
    add_dependent(fake, mode="container:web", running=False)
    up.update("web", force=True)
    dep = fake.ctrs["app"]
    assert dep["HostConfig"]["NetworkMode"].startswith("container:new")
    assert dep["Id"] not in fake.started and "a" * 64 not in fake.stopped


def test_unrelated_containers_untouched(up, fake):
    fake.add(make_ctr())
    add_dependent(fake, name="other", mode="container:zzz")
    up.update("web", force=True)
    assert fake.ctrs["other"]["Id"] == "o" * 64


def test_dependents_restarted_when_parent_rolls_back(up, fake):
    fake.add(make_ctr())
    add_dependent(fake)
    publish_new(fake)
    fake.fail["create"] = RuntimeError("boom")
    up.update("web")
    assert fake.ctrs["app"]["Id"] == "a" * 64  # not recreated
    assert fake.started == [CID, "a" * 64]  # parent restored, then dependent


def test_dependent_restart_failure_logged(up, fake, monkeypatch):
    fake.add(make_ctr())
    add_dependent(fake)
    fake.fail["create"] = RuntimeError("boom")
    real = fake.start
    monkeypatch.setattr(fake, "start", lambda cid: (_ for _ in ()).throw(RuntimeError("no net"))
                        if cid == "a" * 64 else real(cid))
    up.update("web", force=True)
    assert any(e["container"] == "app" and "could not restart: no net" in e["msg"]
               for e in up.logs)


def test_dependent_recreate_failure_does_not_fail_parent(up, fake, monkeypatch):
    fake.add(make_ctr())
    add_dependent(fake)
    publish_new(fake)
    real = fake.create
    monkeypatch.setattr(fake, "create", lambda name, body: (_ for _ in ()).throw(
        RuntimeError("bad dep")) if name == "app" else real(name, body))
    up.update("web")
    assert up.state["web"]["job"] == "updated"
    assert up.state["app"]["job"] == "error" and "failed to follow web" in up.state["app"]["message"]
    assert fake.ctrs["app"]["Id"] == "a" * 64  # rolled back to original dependent


def test_stopped_dependent_not_restarted_on_rollback(up, fake):
    fake.add(make_ctr())
    add_dependent(fake, running=False)
    fake.fail["create"] = RuntimeError("boom")
    up.update("web", force=True)
    assert fake.started == [CID]



# ---------- health-verified updates ----------

class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def hup(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    clk = Clock()
    u = Updater(fake, health_timeout=60, startup_grace=10, sleep=clk.sleep, clock=clk)
    u.clk = clk
    return u


HC = {"Test": ["CMD", "true"]}


def script_new_container(fake, monkeypatch, states, healthcheck=None):
    """Make inspect() of the replacement container return `states` in order
    (a state may carry a RestartCount, which is lifted to the top level)."""
    real = fake.inspect
    seq = iter(states)

    def inspect(cid):
        c = real(cid)
        if c["Id"].startswith("new"):
            c["Config"]["Healthcheck"] = healthcheck
            st = dict(next(seq, states[-1]))
            c["RestartCount"] = st.pop("RestartCount", 0)
            c["State"] = st
        return c
    monkeypatch.setattr(fake, "inspect", inspect)


def test_no_healthcheck_waits_grace_then_succeeds(hup, fake, monkeypatch):
    fake.add(make_ctr())
    publish_new(fake)
    script_new_container(fake, monkeypatch, [{"Running": True}])
    hup.update("web")
    assert hup.state["web"]["job"] == "updated"
    assert hup.clk.t >= 10 and all(s <= 2 for s in hup.clk.sleeps)
    assert CID not in [c["Id"] for c in fake.ctrs.values()]


def test_healthcheck_becomes_healthy(hup, fake, monkeypatch):
    fake.add(make_ctr())
    publish_new(fake)
    starting = {"Running": True, "Health": {"Status": "starting"}}
    script_new_container(fake, monkeypatch,
                         [starting, starting, {"Running": True, "Health": {"Status": "healthy"}}], HC)
    hup.update("web")
    assert hup.state["web"]["job"] == "updated"
    assert any(e["event"] == "health" and e["msg"] == "new container is healthy" for e in hup.logs)


@pytest.mark.parametrize("states,hc,reason", [
    ([{"Running": False, "ExitCode": 3}], None, "exited (code 3)"),
    ([{"Running": True}, {"Running": True, "Restarting": True, "ExitCode": 1}], None, "exited (code 1)"),
    ([{"Running": True}, {"Running": True, "ExitCode": 1, "RestartCount": 1}], None,
     "crashed and was restarted (exit code 1)"),
    ([{"Running": True, "Health": {"Status": "unhealthy", "Log": [{"Output": " db refused \n"}]}}],
     HC, "unhealthy: db refused"),
    ([{"Running": True, "Health": {"Status": "unhealthy", "Log": []}}], HC, "is unhealthy"),
    ([{"Running": True, "Health": {"Status": "starting"}}], HC, "not healthy after 60s"),
])
def test_unhealthy_rolls_back(hup, fake, monkeypatch, states, hc, reason):
    fake.add(make_ctr())
    publish_new(fake)
    script_new_container(fake, monkeypatch, states, hc)
    hup.update("web")
    assert fake.ctrs["web"]["Id"] == CID and set(fake.ctrs) == {"web"}
    st = hup.state["web"]
    assert st["job"] == "rolled-back" and reason in st["message"]
    assert st["failed_image"] == "new"
    assert OLD_IMG in fake.images  # nothing cleaned up
    e = hup.logs[-1]
    assert e["level"] == "ERROR" and e["event"] == "rollback" and "exc" not in e


def test_healthcheck_none_treated_as_absent(hup, fake, monkeypatch):
    fake.add(make_ctr())
    publish_new(fake)
    script_new_container(fake, monkeypatch, [{"Running": True}], {"Test": ["NONE"]})
    hup.update("web")
    assert hup.state["web"]["job"] == "updated"


def test_stopped_container_not_verified(hup, fake, monkeypatch):
    fake.add(make_ctr(State={"Running": False}))
    publish_new(fake)
    script_new_container(fake, monkeypatch, [{"Running": False, "ExitCode": 0}])
    hup.update("web")
    assert hup.state["web"]["job"] == "updated"


def test_scheduled_run_skips_image_that_failed(hup, fake, monkeypatch):
    fake.add(make_ctr())
    publish_new(fake)
    script_new_container(fake, monkeypatch, [{"Running": False, "ExitCode": 1}])
    hup.update("web")
    creates = fake.calls.count("create")
    hup.update("web", scheduled=True)
    assert fake.calls.count("create") == creates
    assert "failed its health check before" in hup.logs[-1]["msg"]
    assert hup.state["web"]["job"] == "rolled-back"
    # manual retry still attempts it, and success clears the marker
    monkeypatch.undo()
    hup.update("web")
    assert hup.state["web"]["job"] == "updated" and hup.state["web"]["failed_image"] is None


def test_force_recreate_rollback_does_not_mark_image(hup, fake, monkeypatch):
    fake.add(make_ctr())
    script_new_container(fake, monkeypatch, [{"Running": False}])
    hup.update("web", force=True)
    assert hup.state["web"]["job"] == "rolled-back" and "failed_image" not in hup.state["web"]


def test_deploy_rolls_back_when_unhealthy(hup, fake, monkeypatch):
    fake.add(make_ctr())
    fake.tags["test/app:1.0"] = OLD_IMG
    script_new_container(fake, monkeypatch, [{"Running": False, "ExitCode": 2}])
    hup.deploy("web", "1.0")
    assert hup.state["web"]["job"] == "rolled-back" and fake.ctrs["web"]["Id"] == CID


# ---------- registry digest check (no pull) ----------

class DigestRegistry:
    def __init__(self, digest="sha256:remote", info=None, fail=None, info_fail=None):
        self._digest, self._info, self.fail, self.info_fail = digest, info, fail, info_fail
        self.platforms = []

    def digest(self, ref):
        if self.fail:
            raise self.fail
        return self._digest

    def remote_info(self, ref, platform):
        self.platforms.append(platform)
        if self.info_fail:
            raise self.info_fail
        return self._info or {"id": "new", "digest": self._digest, "tags": [],
                              "version": "2.0", "revision": None, "created": None}


@pytest.fixture
def dup(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    fake.images[OLD_IMG].update(RepoDigests=["test/app@sha256:local"], Os="linux",
                                Architecture="arm64", Variant="v8")
    return lambda reg, **kw: Updater(fake, registry=reg, **kw)


def test_digest_match_skips_pull(dup, fake):
    u = dup(DigestRegistry(digest="sha256:local"))
    fake.add(make_ctr())
    assert u.check("web")[1] is False
    assert "pull" not in fake.calls
    assert u.state["web"]["job"] == "up-to-date" and u.state["web"]["latest"]["id"] == "old"


def test_digest_differs_reports_remote_details_without_pull(dup, fake):
    reg = DigestRegistry()
    u = dup(reg)
    fake.add(make_ctr())
    u.check_only("web")
    st = u.state["web"]
    assert st["job"] == "update-available" and st["latest"]["version"] == "2.0"
    assert "pull" not in fake.calls
    assert reg.platforms == [{"os": "linux", "architecture": "arm64", "variant": "v8"}]


def test_update_pulls_only_after_digest_says_new(dup, fake):
    u = dup(DigestRegistry())
    fake.add(make_ctr())
    publish_new(fake)
    u.update("web")
    assert fake.calls.count("pull") == 1
    assert u.state["web"]["job"] == "updated" and fake.ctrs["web"]["Image"] == NEW_IMG


def test_update_digest_differs_but_pulled_image_same(dup, fake):
    # e.g. locally built image without RepoDigests: digest "differs", pull shows no change
    u = dup(DigestRegistry())
    fake.add(make_ctr())
    u.update("web")
    assert "create" not in fake.calls and u.logs[-1]["msg"] == "up to date"


def test_remote_info_failure_keeps_digest(dup, fake):
    u = dup(DigestRegistry(info_fail=RuntimeError("blob 500")))
    fake.add(make_ctr())
    u.check("web")
    assert u.state["web"]["latest"] == {"id": None, "digest": "sha256:remote", "tags": [],
                                        "version": None, "revision": None, "created": None}


def test_registry_failure_falls_back_to_pull(dup, fake):
    u = dup(DigestRegistry(fail=RuntimeError("401")))
    fake.add(make_ctr())
    publish_new(fake)
    assert u.check("web")[1] is True and "pull" in fake.calls
    assert any("pulling instead" in e["msg"] for e in u.logs)


def test_digest_check_disabled(dup, fake):
    u = dup(DigestRegistry(digest="sha256:local"), digest_check=False)
    fake.add(make_ctr())
    u.check("web")
    assert "pull" in fake.calls



# ---------- notifications ----------

class RecNotifier:
    def __init__(self):
        self.items, self.batches, self._batch = [], [], None

    def notify(self, event, container, msg):
        (self._batch if self._batch is not None else self.items).append((event, container, msg))

    def batch(self):
        import contextlib

        @contextlib.contextmanager
        def cm():
            self._batch = []
            yield
            self.batches.append(self._batch)
            self._batch = None
        return cm()


@pytest.fixture
def nup(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    fake.images[OLD_IMG]["Config"] = {**IMG_CFG, "Labels": {"org.opencontainers.image.version": "1.0"}}
    u = Updater(fake, notifier=RecNotifier())
    return u


def test_notify_on_update_with_versions(nup, fake):
    fake.add(make_ctr())
    publish_new(fake)
    fake.images[NEW_IMG]["Config"] = {**IMG_CFG, "Labels": {"org.opencontainers.image.version": "2.0"}}
    nup.update("web")
    assert nup.notifier.items == [("update", "web", "test/app:latest 1.0 (old) -> 2.0 (new)")]


def test_no_notify_on_force_recreate_or_up_to_date(nup, fake):
    fake.add(make_ctr())
    nup.update("web")
    nup.update("web", force=True)
    assert nup.notifier.items == []


def test_notify_on_rollback(nup, fake):
    fake.add(make_ctr())
    publish_new(fake)
    fake.ctrs["web"]["Config"]["StopTimeout"] = 1
    nup.startup_grace = 0
    orig = fake.inspect

    def inspect(cid):
        c = orig(cid)
        if c["Id"].startswith("new"):
            c["State"] = {"Running": False, "ExitCode": 9}
        return c
    fake.inspect = inspect
    nup.update("web")
    assert nup.notifier.items == [("rollback", "web", "update rolled back: new container exited (code 9)")]


def test_notify_on_update_error_but_not_check_error(nup, fake):
    fake.add(make_ctr())
    fake.fail["pull"] = RuntimeError("down")
    nup.check_only("web")
    assert nup.notifier.items == []
    nup.update("web")
    assert nup.notifier.items == [("error", "web", "RuntimeError: down")]


def test_notify_deploy_and_dependent_failure(nup, fake, monkeypatch):
    fake.add(make_ctr())
    add_dependent(fake)
    fake.tags["test/app:1.0"] = OLD_IMG
    real = fake.create
    monkeypatch.setattr(fake, "create", lambda name, body: (_ for _ in ()).throw(
        RuntimeError("x")) if name == "app" else real(name, body))
    nup.deploy("web", "1.0")
    events = [(e, c) for e, c, _ in nup.notifier.items]
    assert events == [("error", "app"), ("deploy", "web")]


def test_available_notified_once_per_image_on_scheduled_checks(nup, fake):
    fake.add(make_ctr())
    publish_new(fake)
    nup.check_only("web")  # manual: never notifies
    nup.check_only("web", scheduled=True)
    nup.check_only("web", scheduled=True)
    assert [e for e, _, _ in nup.notifier.items] == ["available"]
    assert nup.notifier.items[0][2] == "test/app:latest 1.0 (old) -> (new)"


def test_describe_change_without_any_info(nup, fake):
    ctr = make_ctr(image_id="sha256:missing")
    fake.add(ctr)
    assert nup._describe_change(ctr) == "test/app:latest ? -> ?"


def test_run_all_batches_notifications(nup, fake):
    fake.add(make_ctr())
    fake.add(make_ctr("db", cid="d" * 64))
    publish_new(fake)
    nup.run_all()
    assert nup.notifier.items == []
    assert [[e for e, _, _ in b] for b in nup.notifier.batches] == [["update", "update"]]


# ---------- persistence ----------

def test_state_persisted_and_restored(fake, tmp_path):
    from app.store import Store
    u = Updater(fake, store=Store(str(tmp_path)))
    fake.add(make_ctr())
    publish_new(fake)
    u.check("web")
    u2 = Updater(fake, store=Store(str(tmp_path)))
    st = u2.state["web"]
    assert st["job"] == "update-available" and st["latest"]["id"] == "new" and st["last_check"]
    assert u2.list()[0]["update_available"] is True



# ---------- Compose awareness ----------

P, S, D = "com.docker.compose.project", "com.docker.compose.service", "com.docker.compose.depends_on"


def compose_ctr(fake, name, service, depends="", project="proj", **labels):
    c = make_ctr(name, cid=name[0] * 63 + "x")
    c["Config"]["Labels"] = {P: project, S: service, **({D: depends} if depends else {}), **labels}
    return fake.add(c)


@pytest.mark.parametrize("value,expected", [
    ("", {}),
    ("db:service_started:false", {"db": False}),
    ("db:service_healthy:true,cache:service_started", {"db": True, "cache": False}),
    ("db:service_started:TRUE, ,redis:x:false", {"db": True, "redis": False}),
])
def test_compose_depends(value, expected):
    assert compose_depends({D: value} if value else {}) == expected
    assert compose_depends(None) == {}


def entry(name, service=None, depends="", project="proj"):
    labels = {P: project, S: service, D: depends} if service else {}
    return {"Id": name, "Labels": labels}


def test_compose_order_dependencies_first():
    cs = [entry("web", "web", "api:service_started"), entry("api", "api", "db:service_healthy:true"),
          entry("db", "db"), entry("plain"), entry("other-db", "db", project="other")]
    assert [c["Id"] for c in compose_order(cs)] == ["db", "api", "web", "plain", "other-db"]


def test_compose_order_cycles_and_unknown_services():
    cs = [entry("a", "a", "b:x"), entry("b", "b", "a:x"), entry("c", "c", "missing:x")]
    assert sorted(c["Id"] for c in compose_order(cs)) == ["a", "b", "c"]
    assert len(compose_order(cs)) == 3


def test_run_all_updates_in_dependency_order(fake, monkeypatch):
    u = Updater(fake)
    compose_ctr(fake, "web", "web", "db:service_started")
    compose_ctr(fake, "db", "db")
    seen = []
    monkeypatch.setattr(u, "update", lambda n, scheduled=False: seen.append(n))
    u.run_all()
    assert seen == ["db", "web"]


def test_compose_image_label_refreshed(up, fake):
    compose_ctr(fake, "web", "web", **{"com.docker.compose.image": OLD_IMG})
    publish_new(fake)
    up.update("web")
    assert fake.created_body["Labels"]["com.docker.compose.image"] == NEW_IMG


def test_compose_image_label_refresh_failure_is_harmless(up, fake, monkeypatch):
    compose_ctr(fake, "web", "web", **{"com.docker.compose.image": OLD_IMG})
    real = fake.image

    def image(ref):  # by-tag lookup during recreate is only the label refresh
        if ref == "test/app:latest" and up.state["web"].get("job") == "updating":
            raise RuntimeError("gone")
        return real(ref)
    monkeypatch.setattr(fake, "image", image)
    up.update("web", force=True)
    assert up.state["web"]["job"] == "updated"
    assert fake.created_body["Labels"]["com.docker.compose.image"] == OLD_IMG
    assert any("could not refresh" in e["msg"] for e in up.logs)


def test_depends_on_restart_true_restarts_dependents(up, fake):
    compose_ctr(fake, "db", "db")
    compose_ctr(fake, "api", "api", "db:service_healthy:true")
    compose_ctr(fake, "web", "web", "db:service_started:false")
    compose_ctr(fake, "elsewhere", "api", "db:service_started:true", project="other")
    publish_new(fake)
    up.update("db")
    assert fake.restarted == [fake.ctrs["api"]["Id"]]
    assert any(e["container"] == "api" and e["event"] == "compose" for e in up.logs)


def test_depends_on_restart_failure_logged(up, fake):
    compose_ctr(fake, "db", "db")
    compose_ctr(fake, "api", "api", "db:service_healthy:true")
    fake.fail["restart"] = RuntimeError("nope")
    up.update("db", force=True)
    assert up.state["db"]["job"] == "updated"
    assert any(e["level"] == "ERROR" and "failed: nope" in e["msg"] for e in up.logs)


def test_deploy_also_restarts_compose_dependents(up, fake):
    compose_ctr(fake, "db", "db")
    compose_ctr(fake, "api", "api", "db:service_healthy:true")
    fake.tags["test/app:1.0"] = OLD_IMG
    up.deploy("db", "1.0")
    assert fake.restarted == [fake.ctrs["api"]["Id"]]


def test_non_compose_container_restarts_nothing(up, fake):
    fake.add(make_ctr())
    up.update("web", force=True)
    assert not hasattr(fake, "restarted")


# ---------- small additions: progress, stopped, pin, tag info ----------

def _progress_seen(up, monkeypatch):
    seen = []
    real = up._set

    def spy(name, **kw):
        if "progress" in kw:
            seen.append(kw["progress"])
        real(name, **kw)
    monkeypatch.setattr(up, "_set", spy)
    return seen


def test_pull_progress_throttled_by_percent(up, fake, monkeypatch):
    fake.add(make_ctr())
    fake.progress_steps = [0, 2, 5, 6, 30, 100]
    monkeypatch.setattr("app.updater.time.monotonic", lambda: 0.0)  # time never advances
    seen = _progress_seen(up, monkeypatch)
    up.check("web")
    assert seen == [0, 5, 30, 100, None]
    assert up.state["web"]["progress"] is None


def test_pull_progress_throttled_by_time(up, fake, monkeypatch):
    fake.add(make_ctr())
    fake.progress_steps = [1, 2, 3]
    ticks = iter(range(0, 1000, 2))  # every call is 2s later
    monkeypatch.setattr("app.updater.time.monotonic", lambda: float(next(ticks)))
    seen = _progress_seen(up, monkeypatch)
    up.check("web")
    assert seen == [1, 2, 3, None]


def test_list_includes_stopped(up, fake):
    fake.add(make_ctr())
    fake.add(make_ctr("off", cid="f" * 64, State={"Running": False}))
    rows = {r["name"]: r for r in up.list()}
    assert rows["off"]["state"] == "exited" and rows["web"]["state"] == "running"


def test_run_all_skips_stopped_unless_enabled(fake, monkeypatch):
    fake.add(make_ctr())
    fake.add(make_ctr("off", cid="f" * 64, State={"Running": False}))
    for include, expected in ((False, ["web"]), (True, ["web", "off"])):
        u = Updater(fake, include_stopped=include)
        seen = []
        monkeypatch.setattr(u, "update", lambda n, scheduled=False: seen.append(n))
        u.run_all()
        assert seen == expected


def test_pin_and_scheduled_runs_only_check(up, fake, monkeypatch):
    fake.add(make_ctr())
    up.pin("web", True)
    assert up.state["web"]["pinned"] is True and up.logs[-1]["event"] == "pin"
    seen = []
    monkeypatch.setattr(up, "update", lambda n, scheduled=False: seen.append(("u", n)))
    monkeypatch.setattr(up, "check_only", lambda n, scheduled=False: seen.append(("c", n)))
    up.run_all()
    up.pin("web", False)
    up.run_all()
    assert seen == [("c", "web"), ("u", "web")]
    assert "unpinned" in [e for e in up.logs if e["event"] == "pin"][-1]["msg"]


def test_pin_unknown_container_raises(up):
    with pytest.raises(KeyError):
        up.pin("ghost", True)


def test_manual_update_ignores_pin(up, fake):
    fake.add(make_ctr())
    up.pin("web", True)
    publish_new(fake)
    up.update("web")
    assert up.state["web"]["job"] == "updated"


def test_tag_info(fake):
    fake.images[OLD_IMG].update(Os="linux", Architecture="amd64")
    reg = DigestRegistry()
    u = Updater(fake, registry=reg)
    fake.add(make_ctr())
    info = u.tag_info("web", "2.0")
    assert info["ref"] == "test/app:2.0" and info["version"] == "2.0"
    assert reg.platforms == [{"os": "linux", "architecture": "amd64", "variant": None}]
    fake.images.pop(OLD_IMG)
    u.tag_info("web", "2.0")
    assert reg.platforms[-1] is None
    with pytest.raises(RuntimeError):
        Updater(fake).tag_info("web", "1")


def test_deploy_reports_pull_progress(up, fake):
    fake.add(make_ctr())
    fake.tags["test/app:1.0"] = OLD_IMG
    fake.progress_steps = [50, 100]
    up.deploy("web", "1.0")
    assert up.state["web"]["progress"] is None and up.state["web"]["job"] == "deployed"


# ---------- image cooldown ----------

def _iso(age_s):
    import datetime as dt
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_s)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def cup(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    fake.images[OLD_IMG].update(RepoDigests=["test/app@sha256:local"])

    def make(created, cooldown=86400, labels=None):
        reg = DigestRegistry(info={"id": "new", "digest": "sha256:remote", "tags": [],
                                   "version": "2.0", "revision": None, "created": created})
        ctr = make_ctr()
        if labels:
            ctr["Config"]["Labels"].update(labels)
        fake.add(ctr)
        publish_new(fake)
        return Updater(fake, registry=reg, cooldown=cooldown)
    return make


def test_cooldown_defers_young_image_without_pulling(cup, fake):
    u = cup(_iso(3 * 3600 + 30))
    u.update("web", scheduled=True)
    st = u.state["web"]
    assert st["job"] == "cooldown" and "2.0 is 3h old; applying after 1d (in 20h59m)" in st["message"]
    assert "pull" not in fake.calls and "create" not in fake.calls
    assert u.logs[-1]["event"] == "cooldown"


def test_cooldown_allows_old_enough_image(cup, fake):
    u = cup(_iso(2 * 86400))
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "updated"


@pytest.mark.parametrize("created,reason", [(None, "has no build date"),
                                            (_iso(-3600), "build date in the future")])
def test_cooldown_defers_unknown_or_future_dates(cup, fake, created, reason):
    u = cup(created)
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "cooldown" and reason in u.state["web"]["message"]


def test_manual_update_bypasses_cooldown(cup, fake):
    u = cup(_iso(60))
    u.update("web")
    assert u.state["web"]["job"] == "updated"


def test_cooldown_label_overrides_and_disables(cup, fake):
    u = cup(_iso(3600), cooldown=0, labels={"updock.cooldown": "2h"})
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "cooldown"
    u2 = Updater(fake, registry=u.registry, cooldown=86400)
    fake.ctrs["web"]["Config"]["Labels"]["updock.cooldown"] = "0"
    u2.update("web", scheduled=True)
    assert u2.state["web"]["job"] == "updated"


def test_invalid_cooldown_label_warns_once_and_uses_global(cup, fake):
    u = cup(_iso(60), cooldown=3600, labels={"updock.cooldown": "soon"})
    ctr = fake.inspect("web")
    assert u.cooldown_for("web", ctr) == 3600
    assert u.cooldown_for("web", ctr) == 3600
    warns = [e for e in u.logs if "invalid updock.cooldown" in e["msg"]]
    assert len(warns) == 1 and warns[0]["level"] == "WARNING"


def test_no_cooldown_by_default(cup, fake):
    u = cup(None, cooldown=0)
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "updated"


def test_cooldown_label_uses_id_when_no_version(fake, monkeypatch):
    fake.images[OLD_IMG].update(RepoDigests=["test/app@sha256:local"])
    reg = DigestRegistry(info={"id": "abc123", "digest": "sha256:r", "tags": [], "version": None,
                               "revision": None, "created": _iso(10)})
    fake.add(make_ctr())
    u = Updater(fake, registry=reg, cooldown=3600)
    u.update("web", scheduled=True)
    assert u.state["web"]["message"].startswith("cooldown: abc123 is ")
    reg._info = {"id": None, "digest": "sha256:r", "tags": [], "version": None, "revision": None,
                 "created": _iso(10)}
    u.update("web", scheduled=True)
    assert u.state["web"]["message"].startswith("cooldown: new image is ")


# ---------- lifecycle hooks ----------

@pytest.fixture
def kup(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    fake.execs = []
    fake.exec_codes = {}

    def exec_run(cid, argv, timeout=60):
        fake.execs.append((cid, argv, timeout))
        key = argv[-1] if argv[:2] == ["sh", "-c"] else argv[0]
        res = fake.exec_codes.get(key, 0)
        if isinstance(res, Exception):
            raise res
        return res, f"output of {key}\n"
    fake.exec_run = exec_run
    return Updater(fake, hooks=True)


def hooked(fake, **hooks):
    c = make_ctr()
    c["Config"]["Labels"].update({f"updock.hook.{k.replace('_', '-')}": v for k, v in hooks.items()})
    return fake.add(c)


def test_hooks_off_by_default(fake):
    hooked(fake, pre_update="backup")
    fake.exec_run = lambda *a, **k: pytest.fail("hook ran")
    Updater(fake).update("web", force=True)


def test_all_hooks_run_in_order(kup, fake):
    hooked(fake, pre_check="pc", pre_update="pu", post_update="po", post_check="pk")
    publish_new(fake)
    kup.update("web")
    cmds = [argv[-1] for _, argv, _ in fake.execs]
    assert cmds == ["pc", "pu", "po", "pk"]
    assert fake.execs[0][0] == CID and fake.execs[2][0] != CID  # post-update on the new one
    assert kup.state["web"]["job"] == "updated"
    msgs = [e["msg"] for e in kup.logs if e["event"] == "hook"]
    assert "pre-update hook exited 0: output of pu" in msgs


def test_pre_update_exit_75_skips(kup, fake):
    hooked(fake, pre_update="busy")
    fake.exec_codes["busy"] = 75
    publish_new(fake)
    kup.update("web")
    assert kup.state["web"]["job"] == "skipped" and "exit 75" in kup.state["web"]["message"]
    assert "stop" not in fake.calls and fake.ctrs["web"]["Id"] == CID


def test_pre_update_failure_aborts(kup, fake):
    hooked(fake, pre_update="backup")
    fake.exec_codes["backup"] = 2
    kup.update("web", force=True)
    assert kup.state["web"]["job"] == "error"
    assert "pre-update hook exited 2; update aborted" in kup.state["web"]["message"]
    assert "stop" not in fake.calls


def test_pre_update_exception_aborts(kup, fake):
    hooked(fake, pre_update="backup")
    fake.exec_codes["backup"] = RuntimeError("exec failed")
    kup.update("web", force=True)
    assert "exited -1" in kup.state["web"]["message"]
    assert any(e["msg"] == "pre-update hook failed: exec failed" for e in kup.logs)


def test_other_hook_failures_only_logged(kup, fake):
    hooked(fake, pre_check="pc", post_update="po", post_check="pk")
    fake.exec_codes.update(pc=1, po=1, pk=TimeoutError("still running after 60s"))
    kup.update("web", force=True)
    assert kup.state["web"]["job"] == "updated"
    lv = {e["msg"].split(" hook")[0]: e["level"] for e in kup.logs
          if e["event"] == "hook" and e["level"] != "DEBUG"}
    assert lv == {"pre-check": "WARNING", "post-update": "WARNING", "post-check": "ERROR"}


def test_check_hooks_on_check_only_and_after_errors(kup, fake):
    hooked(fake, pre_check="pc", post_check="pk")
    fake.fail["pull"] = RuntimeError("down")
    kup.check_only("web")
    assert [argv[-1] for _, argv, _ in fake.execs] == ["pc", "pk"]
    assert kup.state["web"]["job"] == "error"


def test_post_check_inspect_failure_is_ignored(kup, fake, monkeypatch):
    hooked(fake, post_check="pk")
    real = fake.inspect
    calls = {"n": 0}

    def inspect(cid):
        calls["n"] += 1
        if calls["n"] > 2:  # wrapper's first inspect + check's inspect succeed
            raise RuntimeError("gone")
        return real(cid)
    monkeypatch.setattr(fake, "inspect", inspect)
    kup.check_only("web")
    assert any("post-check hook skipped: gone" in e["msg"] for e in kup.logs)


def test_hooks_not_run_on_stopped_container(kup, fake):
    c = hooked(fake, pre_update="pu", pre_check="pc")
    c["State"] = {"Running": False}
    kup.update("web", force=True)
    assert fake.execs == [] and kup.state["web"]["job"] == "updated"


def test_exec_form_and_timeouts(kup, fake):
    hooked(fake, pre_update='["/bin/backup", "--fast"]', pre_update_timeout="5m",
           post_update="po", post_update_timeout="soon")
    kup.update("web", force=True)
    assert fake.execs[0][1:] == (["/bin/backup", "--fast"], 300)
    assert fake.execs[1][1:] == (["sh", "-c", "po"], 60)
    assert any("invalid updock.hook.post-update-timeout" in e["msg"] for e in kup.logs)


def test_invalid_json_falls_back_to_shell(kup, fake):
    hooked(fake, pre_update="[not json")
    kup.update("web", force=True)
    assert fake.execs[0][1] == ["sh", "-c", "[not json"]
    assert any("not a valid JSON array" in e["msg"] for e in kup.logs)


def test_hook_output_empty(kup, fake):
    hooked(fake, pre_update="quiet")
    fake.exec_run = lambda cid, argv, timeout=60: (0, "  ")
    kup.update("web", force=True)
    assert any(e["msg"] == "pre-update hook exited 0" for e in kup.logs)


def test_dependents_do_not_run_parent_hooks(kup, fake):
    hooked(fake)
    dep = add_dependent(fake)
    dep["Config"]["Labels"]["updock.hook.pre-update"] = "dep-hook"
    kup.update("web", force=True)
    assert all(argv[-1] != "dep-hook" for _, argv, _ in fake.execs)


def test_deploy_runs_update_hooks(kup, fake):
    hooked(fake, pre_update="pu")
    fake.tags["test/app:1.0"] = OLD_IMG
    kup.deploy("web", "1.0")
    assert [argv[-1] for _, argv, _ in fake.execs] == ["pu"]


# ---------- disk usage guard ----------

@pytest.fixture
def gup(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    fake.usage = {"used": 10_000, "reclaimable": 2_000, "images": 3}

    def disk_usage():
        if isinstance(fake.usage, Exception):
            raise fake.usage
        return fake.usage
    fake.disk_usage = disk_usage
    fake.add(make_ctr())
    publish_new(fake)
    return lambda **kw: Updater(fake, notifier=RecNotifier(), **kw)


def test_disk_guard_off(gup, fake):
    u = gup()
    assert u.disk_status() == {"enabled": False}
    u.run_all()
    assert fake.ctrs["web"]["Image"] == NEW_IMG


def test_disk_blocked_run_only_checks(gup, fake):
    u = gup(disk_max=5_000)
    u.run_all()
    assert fake.ctrs["web"]["Image"] == OLD_IMG and u.state["web"]["job"] == "update-available"
    e = next(e for e in u.logs if e["event"] == "disk")
    assert e["level"] == "ERROR" and "this run only checks (2.0 KB reclaimable)" in e["msg"]
    assert [(ev, c) for ev, c, _ in u.notifier.batches[0]][0] == ("disk", None)


def test_disk_warning_continues_and_alerts_once(gup, fake):
    u = gup(disk_max=50_000, disk_warn=8_000)
    u.run_all()
    assert fake.ctrs["web"]["Image"] == NEW_IMG
    assert any(e["level"] == "WARNING" and "warning level of 8.0 KB" in e["msg"] for e in u.logs)
    u.run_all()
    disk_alerts = [x for b in u.notifier.batches for x in b if x[0] == "disk"]
    assert len(disk_alerts) == 1


def test_disk_ok_resets_alert(gup, fake):
    u = gup(disk_warn=8_000)
    u.run_all()
    fake.usage = {"used": 1_000, "reclaimable": 0, "images": 1}
    u.run_all()
    fake.usage = {"used": 9_000, "reclaimable": 0, "images": 1}
    u.run_all()
    assert len([x for b in u.notifier.batches for x in b if x[0] == "disk"]) == 2
    assert u.disk_status()["status"] == "warning"


def test_disk_unreadable_fails_closed(gup, fake):
    fake.usage = RuntimeError("df failed")
    u = gup(disk_max=5_000)
    u.run_all()
    assert fake.ctrs["web"]["Image"] == OLD_IMG
    assert any("could not read Docker disk usage (df failed)" in e["msg"] for e in u.logs)


def test_disk_gate_not_used_for_check_only_runs(gup, fake):
    fake.usage = RuntimeError("should not be called")
    u = gup(disk_max=5_000)
    u.run_all(update=False)
    assert not any(e["event"] == "disk" for e in u.logs)


def test_disk_status_cache(gup, fake):
    u = gup(disk_max=50_000)
    first = u.disk_status(max_age=60)
    fake.usage = {"used": 1, "reclaimable": 0, "images": 0}
    assert u.disk_status(max_age=60) is first
    assert u.disk_status()["used"] == 1
    assert first["max"] == 50_000 and first["warn"] is None


# ---------- counters for metrics ----------

def test_outcome_counters(fake, monkeypatch):
    u = Updater(fake)
    fake.add(make_ctr())
    fake.add(make_ctr("db", cid="d" * 64, image="other/db:1"))
    u.update("db", force=True)  # recreate, no new image
    publish_new(fake)
    u.update("web")
    fake.fail["pull"] = RuntimeError("x")
    u.check_only("web")
    u.update("web")
    c = u.counters
    assert (c["result_updated"], c["result_recreated"], c["result_check_failed"],
            c["result_failed"]) == (1, 1, 1, 1)


def test_run_counters_and_failure(fake, monkeypatch):
    u = Updater(fake)
    fake.add(make_ctr())
    u.run_all()
    assert u.counters["runs"] == 1 and u.last_run["duration"] >= 0
    monkeypatch.setattr(fake, "containers", lambda all=False: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        u.run_all()
    assert u.counters["runs"] == 2 and u.counters["run_failures"] == 1



def test_on_change_callback_and_errors(up, fake):
    seen = []
    up.on_change = lambda n, st: seen.append((n, st.get("job")))
    fake.add(make_ctr())
    up.check("web")
    assert ("web", "up-to-date") in seen
    up.on_change = lambda n, st: 1 / 0
    up.check("web")  # a broken listener doesn't break the job
    assert up.state["web"]["job"] == "up-to-date"
