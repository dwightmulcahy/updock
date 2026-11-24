import time
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


def entry_of(name="web", image="test/app:latest", cid=CID, **labels):
    return {"Id": cid, "Names": [f"/{name}"], "Image": image, "Labels": labels or None}


def test_eligible_rules(up):
    assert up.eligible(entry_of())
    assert up.exclusion(entry_of(**{"updock.enable": "false"})) == "label updock.enable=false"
    assert up.exclusion(entry_of(**{"updock.self": "true"})) == "this is Updock"
    assert up.exclusion(entry_of(cid="selfhost123")) == "this is Updock"


def test_eligible_label_enable(fake):
    u = Updater(fake, label_enable=True)
    assert u.exclusion(entry_of()) == "no updock.enable=true label"
    assert u.eligible(entry_of(**{"updock.enable": "true"}))


def test_scope(fake):
    plain, scoped = entry_of(), entry_of(**{"updock.scope": "nas"})
    u = Updater(fake)
    assert u.eligible(plain)
    assert u.exclusion(scoped) == "scope 'nas' belongs to another Updock"
    u = Updater(fake, scope="nas")
    assert u.eligible(scoped) and u.exclusion(plain) == "not in scope 'nas'"
    assert u.exclusion(entry_of(**{"updock.scope": "other"})) == "scope 'other' belongs to another Updock"


def test_name_and_image_patterns(fake):
    u = Updater(fake, exclude_names=["db-*", "", "cache"], exclude_images=["*:pinned"],
                include_images=["test/*", "ghcr.io/acme/*"])
    assert u.eligible(entry_of())
    assert u.exclusion(entry_of("db-main")) == "name matches UPDOCK_EXCLUDE 'db-*'"
    assert u.exclusion(entry_of("cache")) == "name matches UPDOCK_EXCLUDE 'cache'"
    assert u.eligible(entry_of("cache2"))
    assert u.exclusion(entry_of(image="nginx:1")) == "image doesn't match UPDOCK_INCLUDE_IMAGES"
    assert u.eligible(entry_of(image="ghcr.io/acme/api:2"))
    assert u.exclusion(entry_of(image="test/app:pinned")) == "image matches UPDOCK_EXCLUDE_IMAGES '*:pinned'"


def test_image_pattern_uses_known_ref_when_tag_moved(fake):
    u = Updater(fake, include_images=["test/*"])
    e = entry_of(image="sha256:abc")
    assert not u.eligible(e)
    u.state["web"]["ref"] = "test/app:latest"
    assert u.eligible(e)


def test_monitor_only_label_scheduled_runs_only_check(fake, monkeypatch):
    c = make_ctr()
    c["Config"]["Labels"]["updock.monitor-only"] = "true"
    fake.add(c)
    fake.add(make_ctr("db", cid="d" * 64))
    u = Updater(fake)
    seen = []
    monkeypatch.setattr(u, "update", lambda n, scheduled=False: seen.append(("u", n)))
    monkeypatch.setattr(u, "check_only", lambda n, scheduled=False: seen.append(("c", n)))
    u.run_all()
    assert sorted(seen) == [("c", "web"), ("u", "db")]
    rows = {r["name"]: r for r in u.list()}
    assert rows["web"]["monitor_only"] is True and rows["db"]["monitor_only"] is False
    assert u.monitor_only("web", {"updock.monitor-only": "YES"})
    assert not u.monitor_only("web", {"updock.monitor-only": "no"})


def test_list_shows_exclusion_reason(fake):
    u = Updater(fake, exclude_names=["web"])
    fake.add(make_ctr())
    row = u.list()[0]
    assert row["eligible"] is False and row["excluded"] == "name matches UPDOCK_EXCLUDE 'web'"


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
                    "version": "1.2.3", "revision": "f" * 12, "created": "2026-01-01T00:00:00Z",
                    "source": None}


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
     HC, "not healthy after 60s; last check: db refused (raise updock.health-timeout"),
    ([{"Running": True, "Health": {"Status": "unhealthy", "Log": []}}], HC,
     "not healthy after 60s (raise updock.health-timeout"),
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
                                        "version": None, "revision": None, "created": None,
                                        "note": "details unavailable: blob 500"}
    assert any(e["level"] == "WARNING" and "details couldn't be read" in e["msg"] for e in u.logs)


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

    def notify(self, event, container, msg, details=None):
        (self._batch if self._batch is not None else self.items).append((event, container, msg))
        self.details = getattr(self, "details", [])
        self.details.append(details)

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


def test_manual_update_honors_cooldown_unless_overridden(cup, fake):
    u = cup(_iso(60))
    u.update("web")
    assert u.state["web"]["job"] == "cooldown" and "pull" not in fake.calls
    rows = {r["name"]: r for r in u.list()}
    cool = rows["web"]["cooldown"]
    assert cool["delay"] == 86400 and cool["until"] - time.time() == pytest.approx(86400 - 60, abs=5)
    u.update("web", override_cooldown=True)
    assert u.state["web"]["job"] == "updated"
    assert "cooldown" not in {r["name"]: r for r in u.list()}["web"]  # no update pending


def test_cooldown_info_without_build_date(cup, fake):
    u = cup(None)
    u.check("web")
    info = u.cooldown_info("web", fake.inspect("web"))
    assert info["until"] is None and "no build date" in info["message"]
    assert Updater(fake, registry=u.registry).cooldown_info("web", fake.inspect("web")) is None


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


# ---------- copy-file ----------

import io as _io  # noqa: E402
import tarfile as _tarfile  # noqa: E402

from app.docker_api import DockerError as _DockerError  # noqa: E402


def tar_of(name, data=b"x", kind=_tarfile.REGTYPE, size=None, extra=0, mode=0o640, uid=7, gid=8):
    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w") as tf:
        for i in range(1 + extra):
            ti = _tarfile.TarInfo(name if i == 0 else f"{name}{i}")
            ti.type, ti.mode, ti.uid, ti.gid = kind, mode, uid, gid
            if kind == _tarfile.REGTYPE:
                ti.size = len(data) if size is None else size
                tf.addfile(ti, _io.BytesIO(data.ljust(ti.size, b"\0")))
            else:
                tf.addfile(ti)
    return buf.getvalue()


@pytest.fixture
def fup(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    fake.archives, fake.written = {}, []

    def get_archive(cid, path, max_bytes):
        res = fake.archives.get(path, _DockerError(404, "not found"))
        if isinstance(res, Exception):
            raise res
        return res

    def put_archive(cid, path, data):
        if getattr(fake, "put_fails", None):
            raise fake.put_fails
        with _tarfile.open(fileobj=_io.BytesIO(data)) as tf:
            fake.written.append((cid, path, [(m.name, m.mode, m.uid, m.gid,
                                              tf.extractfile(m).read()) for m in tf]))
    fake.get_archive, fake.put_archive = get_archive, put_archive
    return Updater(fake)


def copy_ctr(fake, label, **hc):
    c = make_ctr()
    c["Config"]["Labels"]["updock.copy-file"] = label
    c["HostConfig"].update(hc)
    return fake.add(c)


def test_copy_files_carried_into_new_container(fup, fake):
    copy_ctr(fake, " /etc/app/config.yml , /data/db.sqlite,/opt/a.conf, /gone ")
    fake.archives["/etc/app/config.yml"] = tar_of("config.yml", b"key: v\n")
    fake.archives["/opt/a.conf"] = tar_of("a.conf", b"A", mode=0o600, uid=0, gid=0)
    fup.update("web", force=True)
    assert fup.state["web"]["job"] == "updated"
    (cid, path, files), = fake.written
    assert cid.startswith("new") and path == "/"
    assert files == [("etc/app/config.yml", 0o640, 7, 8, b"key: v\n"), ("opt/a.conf", 0o600, 0, 0, b"A")]
    msgs = [e["msg"] for e in fup.logs if e["event"] == "copy-file"]
    assert "/data/db.sqlite is on a volume/bind mount and survives anyway; not copied" in msgs
    assert "/gone doesn't exist; not copied" in msgs
    assert "copied /etc/app/config.yml, /opt/a.conf into the new container" in msgs
    assert fake.calls.index("create") < fake.calls.index("start")


def test_no_label_no_archive_calls(fup, fake):
    fake.add(make_ctr())
    fake.get_archive = lambda *a: pytest.fail("archive read without label")
    fup.update("web", force=True)
    assert fake.written == []


def test_only_missing_or_mounted_files_writes_nothing(fup, fake):
    copy_ctr(fake, "/data/x,/gone")
    fup.update("web", force=True)
    assert fake.written == [] and fup.state["web"]["job"] == "updated"


def test_read_only_rootfs_skipped(fup, fake):
    copy_ctr(fake, "/etc/app/config.yml", ReadonlyRootfs=True)
    fake.archives["/etc/app/config.yml"] = tar_of("config.yml")
    fup.update("web", force=True)
    assert fake.written == [] and any("read-only root filesystem" in e["msg"] for e in fup.logs)


@pytest.mark.parametrize("label,setup,reason", [
    ("etc/relative", None, "invalid path"),
    ("/etc/../shadow", None, "invalid path"),
    ("/etc/app/", None, "invalid path"),
    ("/etc/dir", tar_of("dir", kind=_tarfile.DIRTYPE), "not a regular file"),
    ("/etc/link", tar_of("link", kind=_tarfile.SYMTYPE), "not a regular file"),
    ("/etc/many", tar_of("many", extra=1), "not a regular file"),
    ("/etc/big", tar_of("big", size=1024 * 1024 + 1), "over 1 MiB"),
    ("/etc/huge", _DockerError(413, "too big"), "over 1 MiB"),
])
def test_unsafe_entries_abort_before_stopping(fup, fake, label, setup, reason):
    copy_ctr(fake, label)
    if setup is not None:
        fake.archives[label] = setup
    fup.update("web", force=True)
    st = fup.state["web"]
    assert st["job"] == "error" and reason in st["message"]
    assert "stop" not in fake.calls and fake.ctrs["web"]["Id"] == CID
    assert "exc" not in fup.logs[-1]  # expected error: no traceback


def test_total_size_limit(fup, fake, monkeypatch):
    import app.updater as upd
    monkeypatch.setattr(upd, "COPY_MAX_TOTAL", 3)
    copy_ctr(fake, "/a,/b")
    fake.archives["/a"] = tar_of("a", b"12")
    fake.archives["/b"] = tar_of("b", b"34")
    fup.update("web", force=True)
    assert "files total over 16 MiB" in fup.state["web"]["message"]


def test_other_archive_errors_propagate(fup, fake):
    copy_ctr(fake, "/etc/x")
    fake.archives["/etc/x"] = _DockerError(500, "daemon broke")
    fup.update("web", force=True)
    assert "daemon broke" in fup.state["web"]["message"] and "stop" not in fake.calls


def test_write_failure_after_stop_rolls_back(fup, fake):
    copy_ctr(fake, "/etc/x")
    fake.archives["/etc/x"] = tar_of("x")
    fake.put_fails = _DockerError(500, "no space")
    fup.update("web", force=True)
    assert fup.state["web"]["job"] == "error" and fake.ctrs["web"]["Id"] == CID
    assert set(fake.ctrs) == {"web"}
    assert any("no space; rolling back" in e["msg"] for e in fup.logs)



# ---------- registry rate limits ----------

from app.registry import RateLimited as _RL  # noqa: E402


class LimitedRegistry(DigestRegistry):
    def __init__(self, seq):
        super().__init__()
        self.seq, self.calls = list(seq), 0

    def digest(self, ref):
        self.calls += 1
        r = self.seq.pop(0) if self.seq else "sha256:local"
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def rup(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    fake.images[OLD_IMG].update(RepoDigests=["test/app@sha256:local"])
    clock = {"t": 1000.0}
    monkeypatch.setattr("app.updater.time.time", lambda: clock["t"])
    fake.add(make_ctr())
    fake.add(make_ctr("db", cid="d" * 64))
    def make(seq):
        u = Updater(fake, registry=LimitedRegistry(seq))
        u.clock = clock
        return u
    return make


def test_429_backs_off_whole_registry_without_pull_fallback(rup, fake):
    u = rup([_RL("registry-1.docker.io", None)])
    u.check_only("web")
    assert u.state["web"]["job"] == "rate-limited" and "next try in 1m" in u.state["web"]["message"]
    assert "pull" not in fake.calls
    u.check_only("db")  # same registry: not contacted at all
    assert u.registry.calls == 1 and u.state["db"]["job"] == "rate-limited"
    assert u.state["db"]["message"].startswith("backing off from registry-1.docker.io")
    assert u.counters["result_rate_limited"] == 2
    assert [e["level"] for e in u.logs if e["event"] == "rate-limit"][:2] == ["WARNING", "WARNING"]


def test_backoff_doubles_honours_retry_after_and_resets(rup, fake):
    u = rup([_RL("registry-1.docker.io", None), _RL("registry-1.docker.io", None),
             _RL("registry-1.docker.io", 900)])
    host = "registry-1.docker.io"
    u.check_only("web")
    assert u._backoff[host]["until"] == 1060
    u.clock["t"] = 1061
    u.check_only("web")
    assert u._backoff[host]["until"] == 1061 + 120
    u.clock["t"] = 2000
    u.check_only("web")
    assert u._backoff[host]["until"] == 2000 + 900  # Retry-After wins when longer
    u.clock["t"] = 5000
    u.check_only("web")  # success clears the backoff
    assert host not in u._backoff and u.state["web"]["job"] == "up-to-date"


def test_backoff_capped(rup, fake):
    u = rup([])
    u._backoff["registry-1.docker.io"] = {"hits": 30}
    e = u._rate_limited(_RL("registry-1.docker.io"))
    assert e.retry_after == 6 * 3600


def test_pull_toomanyrequests_becomes_backoff(fake, monkeypatch):
    from app.docker_api import DockerError
    fake.add(make_ctr())
    u = Updater(fake)
    fake.fail["pull"] = DockerError(500, "toomanyrequests: You have reached your pull rate limit.")
    u.check_only("web")
    assert u.state["web"]["job"] == "rate-limited"
    assert "registry-1.docker.io" in u._backoff
    fake.fail.pop("pull")
    u._backoff.clear()
    u.check_only("web")
    assert u.state["web"]["job"] == "up-to-date"


def test_other_pull_errors_unchanged(fake):
    from app.docker_api import DockerError
    fake.add(make_ctr())
    u = Updater(fake)
    fake.fail["pull"] = DockerError(404, "manifest unknown")
    u.check_only("web")
    assert u.state["web"]["job"] == "error" and not u._backoff


def test_update_pull_after_digest_rate_limited(fake):
    from app.docker_api import DockerError
    fake.images[OLD_IMG].update(RepoDigests=["test/app@sha256:local"])
    fake.add(make_ctr())
    publish_new(fake)
    u = Updater(fake, registry=DigestRegistry())
    fake.fail["pull"] = DockerError(500, "Error response: 429 Too Many Requests")
    u.update("web")
    assert u.state["web"]["job"] == "rate-limited" and "create" not in fake.calls



# ---------- changelog links ----------

GH = {"org.opencontainers.image.source": "https://github.com/acme/app.git",
      "org.opencontainers.image.version": "2.0"}


def test_list_adds_release_notes_links(up, fake):
    fake.images[OLD_IMG]["Config"] = {**IMG_CFG, "Labels": {**GH, "org.opencontainers.image.version": "1.0"}}
    fake.add(make_ctr())
    publish_new(fake)
    fake.images[NEW_IMG]["Config"] = {**IMG_CFG, "Labels": GH}
    up.check("web")
    row = up.list()[0]
    assert row["current"]["changelog"] == "https://github.com/acme/app/releases?q=1.0&expanded=true"
    assert row["latest"]["changelog"] == "https://github.com/acme/app/releases?q=2.0&expanded=true"
    assert "changelog" not in up.state["web"]["latest"]  # state itself untouched


def test_changelog_template_label(up, fake):
    c = make_ctr()
    c["Config"]["Labels"]["updock.changelog-url"] = "https://example.com/notes/{version}"
    fake.images[OLD_IMG]["Config"] = {**IMG_CFG, "Labels": GH}
    fake.add(c)
    assert up.list()[0]["current"]["changelog"] == "https://example.com/notes/2.0"


def test_update_email_includes_release_notes(nup, fake):
    fake.add(make_ctr())
    publish_new(fake)
    fake.images[NEW_IMG]["Config"] = {**IMG_CFG, "Labels": GH}
    nup.update("web")
    msg = nup.notifier.items[0][2]
    assert msg.endswith("\nRelease notes: https://github.com/acme/app/releases?q=2.0&expanded=true")


def test_tag_info_includes_changelog(fake):
    reg = DigestRegistry(info={"id": "x", "digest": "d", "tags": [], "version": "3.1",
                               "revision": None, "created": None,
                               "source": "https://github.com/acme/app"})
    fake.add(make_ctr())
    u = Updater(fake, registry=reg)
    assert u.tag_info("web", "3.1")["changelog"] == \
        "https://github.com/acme/app/releases?q=3.1&expanded=true"


# ---------- revive stopped containers ----------

def _stopped(fake, **labels):
    c = make_ctr(State={"Running": False})
    c["Config"]["Labels"].update(labels)
    fake.add(c)
    publish_new(fake)
    return c


def test_stopped_container_stays_stopped_by_default(fake):
    _stopped(fake)
    u = Updater(fake)
    u.update("web")
    assert u.state["web"]["job"] == "updated" and not getattr(fake, "started", [])


def test_revive_starts_after_image_update(fake):
    _stopped(fake)
    u = Updater(fake, revive_stopped=True)
    u.update("web")
    new = fake.ctrs["web"]["Id"]
    assert new.startswith("new") and fake.started == [new]
    assert any("will be started after the update" in e["msg"] for e in u.logs)


def test_revive_label_overrides_setting(fake):
    _stopped(fake, **{"updock.revive": "false"})
    Updater(fake, revive_stopped=True).update("web")
    assert not getattr(fake, "started", [])
    fake.ctrs.clear()
    _stopped(fake, **{"updock.revive": "true"})
    Updater(fake).update("web")
    assert len(fake.started) == 1


def test_revive_not_for_force_recreate_without_new_image(fake):
    c = make_ctr(State={"Running": False})
    fake.add(c)
    Updater(fake, revive_stopped=True).update("web", force=True)
    assert not getattr(fake, "started", [])


def test_revive_failure_rolls_back_to_stopped_original(fake):
    _stopped(fake)
    u = Updater(fake, revive_stopped=True)
    fake.fail["start"] = RuntimeError("port in use")
    u.update("web")
    assert fake.ctrs["web"]["Id"] == CID and u.state["web"]["job"] == "error"
    assert fake.calls.count("start") == 1  # only the failed revive; the original stays stopped
    assert "stop" not in fake.calls



# ---------- slow starters: unhealthy is not final ----------

def test_unhealthy_then_healthy_is_success(hup, fake, monkeypatch):
    """Docker flips to `unhealthy` when a slow app outlives its start period, then to
    `healthy` once it's up. That must not roll back (taplistbuilder case)."""
    fake.add(make_ctr())
    publish_new(fake)
    bad = {"Running": True, "Health": {"Status": "unhealthy", "Log": [{"Output": "Connection refused"}]}}
    script_new_container(fake, monkeypatch, [bad, bad, bad, {"Running": True, "Health": {"Status": "healthy"}}], HC)
    hup.update("web")
    assert hup.state["web"]["job"] == "updated"
    waits = [e["msg"] for e in hup.logs if e["event"] == "health" and "failing so far" in e["msg"]]
    assert waits == ["healthcheck failing so far; waiting up to 60s more (Connection refused)"]


def test_health_timeout_label(hup, fake, monkeypatch):
    c = make_ctr()
    fake.add(c)
    publish_new(fake)
    starting = {"Running": True, "Health": {"Status": "starting"}}
    real = fake.inspect

    def inspect(cid):
        x = real(cid)
        if x["Id"].startswith("new"):
            x["Config"]["Healthcheck"] = HC
            x["Config"]["Labels"] = {"updock.health-timeout": "5m"}
            x["State"] = starting
        return x
    monkeypatch.setattr(fake, "inspect", inspect)
    hup.update("web")
    assert "not healthy after 300s" in hup.state["web"]["message"]


def test_invalid_health_timeout_label(hup, fake):
    info = {"Config": {"Labels": {"updock.health-timeout": "a while"}}}
    assert hup.health_timeout_for("web", info) == 60
    assert any("invalid updock.health-timeout" in e["msg"] for e in hup.logs)
    assert hup.health_timeout_for("web", {"Config": {"Labels": None}}) == 60



# ---------- registry auth ----------

from app.registry import RegistryAuthError as _RAE  # noqa: E402


def test_auth_error_no_pull_fallback_and_state(fake):
    fake.images[OLD_IMG].update(RepoDigests=["test/app@sha256:local"])
    fake.add(make_ctr(image="ghcr.io/me/private:1"))
    reg = LimitedRegistry([_RAE("ghcr.io", "me/private")])
    u = Updater(fake, registry=reg, notifier=RecNotifier())
    u.update("web")
    st = u.state["web"]
    assert st["job"] == "auth-required" and st["auth_host"] == "ghcr.io"
    assert "read:packages" in st["message"] and "pull" not in fake.calls
    assert u.counters["result_auth_required"] == 1
    assert u.notifier.items[0][0] == "error"
    assert "exc" not in u.logs[-1] and u.logs[-1]["event"] == "auth"


def test_auth_error_on_check_does_not_email(fake):
    fake.add(make_ctr(image="ghcr.io/me/private:1"))
    u = Updater(fake, registry=LimitedRegistry([_RAE("ghcr.io", "me/private")]), notifier=RecNotifier())
    u.check_only("web")
    assert u.state["web"]["job"] == "auth-required" and u.notifier.items == []


@pytest.mark.parametrize("msg", [
    'Head "https://ghcr.io/v2/me/private/manifests/1": unauthorized',
    "pull access denied for me/private, repository does not exist or may require 'docker login'",
    "no basic auth credentials", "authentication required",
])
def test_engine_pull_auth_errors_explained(fake, msg):
    from app.docker_api import DockerError
    fake.add(make_ctr(image="ghcr.io/me/private:1"))
    u = Updater(fake)
    fake.fail["pull"] = DockerError(500, msg)
    u.check_only("web")
    st = u.state["web"]
    assert st["job"] == "auth-required"
    assert st["message"].startswith("ghcr.io refused access to me/private")


def test_deploy_auth_error(fake):
    from app.docker_api import DockerError
    fake.add(make_ctr())
    fake.fail["pull"] = DockerError(500, "unauthorized: authentication required")
    u = Updater(fake)
    u.deploy("web", "2.0")
    assert u.state["web"]["job"] == "auth-required"
    assert "docker.io refused access to test/app" in u.state["web"]["message"]



def test_already_unhealthy_container_judged_by_staying_up(hup, fake, monkeypatch):
    """If the old container already failed its healthcheck (e.g. a broken check in the
    image), requiring the new one to pass would only roll back to another broken one."""
    c = make_ctr()
    c["State"] = {"Running": True, "Health": {"Status": "unhealthy"}}
    fake.add(c)
    publish_new(fake)
    bad = {"Running": True, "Health": {"Status": "unhealthy", "Log": [{"Output": "refused"}]}}
    script_new_container(fake, monkeypatch, [bad], HC)
    hup.update("web")
    assert hup.state["web"]["job"] == "updated"
    assert any("already failing before the update" in e["msg"] for e in hup.logs)
    assert hup.clk.t >= 10  # startup grace was applied


def test_already_unhealthy_but_new_one_exits_still_rolls_back(hup, fake, monkeypatch):
    c = make_ctr()
    c["State"] = {"Running": True, "Health": {"Status": "unhealthy"}}
    fake.add(c)
    publish_new(fake)
    script_new_container(fake, monkeypatch, [{"Running": False, "ExitCode": 1}], HC)
    hup.update("web")
    assert hup.state["web"]["job"] == "rolled-back"



def test_recheck_auth_only_matching_host(fake, monkeypatch):
    u = Updater(fake)
    u.state["a"].update(job="auth-required", auth_host="ghcr.io")
    u.state["b"].update(job="auth-required", auth_host="registry-1.docker.io")
    u.state["c"].update(job="up-to-date")
    seen = []
    monkeypatch.setattr(u, "check_only", lambda n, scheduled=False: seen.append(n))
    assert u.recheck_auth("ghcr.io") == ["a"] and seen == ["a"]
    assert u.recheck_auth("https://index.docker.io/v1/") == ["b"]



# ---------- run progress & steps ----------

def test_run_progress_published_and_summarised(fake, monkeypatch):
    u = Updater(fake)
    fake.add(make_ctr())
    fake.add(make_ctr("db", cid="d" * 64))
    events = []
    u.on_run = lambda r: events.append(None if r is None else dict(r))
    queued_during = []
    real = u.check_only

    def spy(n, scheduled=False):
        queued_during.append((n, sorted(u.queue), [r["name"] for r in u.list() if r["queued"]]))
        real(n, scheduled)
    monkeypatch.setattr(u, "check_only", spy)
    assert u.run_all(update=False) is True
    assert events[0]["total"] == 0 and events[0]["kind"] == "check"
    assert any(e and e["total"] == 2 and e["current"] is None for e in events)
    assert [e["current"] for e in events if e and e["current"]] == ["web", "db"]
    assert events[-1] is None and u.run is None and u.queue == set()
    assert queued_during == [("web", ["db"], ["db"]), ("db", [], [])]
    assert u.last_run["total"] == 2 and u.last_run["results"] == {"up-to-date": 2}
    assert u.last_run["kind"] == "check"


def test_concurrent_run_refused(fake):
    u = Updater(fake)
    u.run = {"kind": "check"}
    assert u.run_all() is False
    assert any("already in progress" in e["msg"] for e in u.logs)


def test_disk_blocked_run_reports_check_kind(fake):
    fake.disk_usage = lambda: {"used": 10, "reclaimable": 0, "images": 1}
    u = Updater(fake, disk_max=5)
    u.run_all()
    assert u.last_run["kind"] == "check"


def test_broken_run_listener_ignored(fake):
    u = Updater(fake)
    u.on_run = lambda r: 1 / 0
    assert u.run_all() is True


def test_update_steps_recorded_in_order(hup, fake, monkeypatch):
    fake.add(make_ctr())
    publish_new(fake)
    script_new_container(fake, monkeypatch, [{"Running": True}])
    steps = []
    real = hup._set

    def spy(name, **kw):
        if kw.get("step") and (not steps or steps[-1] != kw["step"]):
            steps.append(kw["step"])
        real(name, **kw)
    monkeypatch.setattr(hup, "_set", spy)
    hup.update("web")
    assert steps == ["pull", "stop", "start", "health", "cleanup"]
    st = hup.state["web"]
    assert st["step"] is None and st["health_until"] is None and st["job_started"] > 0


def test_store_drops_transient_progress(tmp_path):
    from app.store import Store
    s = Store(str(tmp_path))
    s.save_state({"web": {"job": "updated", "step": "health", "health_until": 1, "progress": 40}})
    assert Store(str(tmp_path)).load_state() == {"web": {"job": "updated"}}



def test_action_recorded_per_job(fake):
    u = Updater(fake)
    fake.add(make_ctr())
    u.check_only("web")
    assert u.state["web"]["action"] == "check"
    u.update("web", force=True)
    assert u.state["web"]["action"] == "update"
    fake.tags["test/app:1.0"] = OLD_IMG
    u.deploy("web", "1.0")
    assert u.state["web"]["action"] == "deploy"



def test_list_hides_previous_container_during_update(fake):
    u = Updater(fake)
    fake.add(make_ctr())
    fake.add(make_ctr("web-updock-old", cid="o" * 64))
    assert [r["name"] for r in u.list()] == ["web"]


# ---------- manual rollback ----------

def _updated(fake, **kw):
    fake.images[OLD_IMG]["RepoDigests"] = ["test/app@sha256:olddigest"]
    fake.images[OLD_IMG]["Config"] = {**IMG_CFG, "Labels": {"org.opencontainers.image.version": "1.0"}}
    fake.add(make_ctr())
    publish_new(fake)
    u = Updater(fake, **kw)
    u.update("web")
    assert u.state["web"]["job"] == "updated"
    return u


def _tagger(fake):
    def tag_image(src, ref):
        fake.calls.append(f"tag:{src}>{ref}")
        fake.tags[ref] = fake.tags.get(src, src)
    fake.tag_image = tag_image


def test_update_records_previous(fake):
    u = _updated(fake, cleanup=False)
    prev = u.state["web"]["previous"]
    assert prev["ref"] == "test/app:latest" and prev["image"] == OLD_IMG
    assert prev["digest"] == "sha256:olddigest" and prev["version"] == "1.0"


def test_rollback_with_local_image_and_toggle(fake):
    u = _updated(fake, cleanup=False)
    _tagger(fake)
    u.rollback("web")
    st = u.state["web"]
    assert st["job"] == "reverted" and st["pinned"] is True and "1.0" in st["message"]
    assert f"tag:{OLD_IMG}>test/app:latest" in fake.calls
    assert fake.ctrs["web"]["Image"] == OLD_IMG and "pull" not in fake.calls[-12:]
    assert st["previous"]["image"] == NEW_IMG and NEW_IMG in fake.images  # newer one kept
    u.rollback("web")
    assert fake.ctrs["web"]["Image"] == NEW_IMG


def test_rollback_pulls_deleted_image_by_digest(fake, monkeypatch):
    u = _updated(fake)  # cleanup removed the old image
    assert OLD_IMG not in fake.images
    _tagger(fake)

    def pull(ref, progress=None):
        fake.pulled = ref
        fake.images[OLD_IMG] = {"Id": OLD_IMG, "Config": IMG_CFG}
        fake.tags[ref] = OLD_IMG
    monkeypatch.setattr(fake, "pull", pull)
    u.rollback("web")
    assert fake.pulled == "test/app@sha256:olddigest"
    assert fake.ctrs["web"]["Image"] == OLD_IMG and u.state["web"]["job"] == "reverted"


def test_rollback_without_digest_fails(fake):
    fake.add(make_ctr())
    publish_new(fake)
    u = Updater(fake)
    u.update("web")
    u.rollback("web")
    assert u.state["web"]["job"] == "error" and "no registry digest" in u.state["web"]["message"]


def test_rollback_without_history(up, fake):
    fake.add(make_ctr())
    up.rollback("web")
    assert up.state["web"]["job"] == "error" and "no earlier version" in up.state["web"]["message"]


def test_rollback_digest_pinned_ref(fake):
    fake.add(make_ctr(image="test/app@sha256:abc"))
    fake.tags["test/app:2.0"] = NEW_IMG
    fake.images[NEW_IMG] = {"Id": NEW_IMG, "Config": IMG_CFG}
    fake.tags["sha256:old"] = OLD_IMG
    u = Updater(fake)
    u.deploy("web", "2.0")
    assert u.state["web"]["previous"]["ref"] == "test/app@sha256:abc"
    u.rollback("web")
    assert fake.created_body["Image"] == OLD_IMG and u.state["web"]["ref"] == OLD_IMG


# ---------- updock.health-url ----------

def _url_ctr(url="http://app:80/"):
    c = make_ctr()
    c["Config"]["Labels"] = {**c["Config"]["Labels"], "updock.health-url": url}
    return c


def _url_up(fake, codes, **kw):
    seq = list(codes)
    calls = []

    def probe(url, timeout):
        calls.append((url, timeout))
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(v, Exception):
            raise v
        return v
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s
    u = Updater(fake, probe=probe, sleep=sleep, clock=lambda: clock["t"], startup_grace=0,
                health_timeout=10, **kw)
    return u, calls


def test_health_url_passes_after_retries(fake):
    fake.add(_url_ctr())
    publish_new(fake)
    u, calls = _url_up(fake, [ConnectionError("refused"), 503, 302])
    u.update("web")
    assert u.state["web"]["job"] == "updated"
    assert len(calls) == 3 and calls[0] == ("http://app:80/", 10)
    assert any("answered 302" in e["msg"] for e in u.logs)


def test_health_url_timeout_rolls_back(fake):
    fake.add(_url_ctr())
    publish_new(fake)
    u, _ = _url_up(fake, [500])
    u.update("web")
    st = u.state["web"]
    assert st["job"] == "rolled-back" and "HTTP 500" in st["message"]
    assert fake.ctrs["web"]["Id"] == CID


def test_health_url_container_exits(fake, monkeypatch):
    fake.add(_url_ctr())
    publish_new(fake)
    u, _ = _url_up(fake, [ConnectionError("x")])
    real = fake.inspect

    def inspect(cid):
        c = real(cid)
        if c["Id"] != CID and u.state["web"].get("message", "").startswith("Waiting up to"):
            c["State"] = {"Running": False, "ExitCode": 3}
        return c
    monkeypatch.setattr(fake, "inspect", inspect)
    u.update("web")
    assert u.state["web"]["job"] == "rolled-back" and "exited (code 3)" in u.state["web"]["message"]


def test_health_url_invalid_scheme_skipped(fake):
    fake.add(_url_ctr("ftp://x"))
    publish_new(fake)
    u, calls = _url_up(fake, [200])
    u.update("web")
    assert u.state["web"]["job"] == "updated" and calls == []
    assert any("invalid updock.health-url" in e["msg"] for e in u.logs)


def test_http_probe(monkeypatch):
    from app import updater as mod
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return type("R", (), {"status_code": 204})()
    monkeypatch.setattr(mod.httpx, "get", get)
    assert mod.http_probe("http://x/", 5) == 204
    assert seen == {"url": "http://x/", "timeout": 5, "follow_redirects": False, "trust_env": False}


# ---------- per-project runs ----------

def test_run_all_limited_to_project(fake, monkeypatch):
    u = Updater(fake)
    compose_ctr(fake, "web", "web", "db:service_started")
    compose_ctr(fake, "db", "db")
    compose_ctr(fake, "other", "app", project="blog")
    fake.add(make_ctr("solo", cid="s" * 64))
    seen = []
    monkeypatch.setattr(u, "update", lambda n, scheduled=False: seen.append(n))
    u.run_all(project="proj")
    assert seen == ["db", "web"]
    assert u.last_run["project"] == "proj" and u.last_run["total"] == 2
    assert any("project proj" in e["msg"] for e in u.logs)
    assert u.projects() == ["blog", "proj"]
    rows = {r["name"]: r for r in u.list()}
    assert rows["web"]["project"] == "proj" and rows["web"]["service"] == "web"
    assert rows["solo"]["project"] is None


# ---------- delete ----------

def test_delete_running_container_and_unused_image(fake):
    fake.add(make_ctr())
    events = []
    u = Updater(fake)
    u.on_change = lambda n, st: events.append(st)
    u.state["web"]["job"] = "up-to-date"
    u.delete("web", remove_image=True, remove_volumes=True)
    assert "web" not in fake.ctrs and fake.stopped == [CID] and fake.removed == ["test/app:latest"]
    assert fake.removed_volumes is True
    assert "web" not in u.state and events[-1] == {"deleted": True}
    msg = [e for e in u.logs if e["event"] == "delete"][-1]
    assert msg["level"] == "WARNING" and "anonymous volumes and image test/app:latest" in msg["msg"]


def test_delete_keeps_shared_image_and_reports_image_errors(fake):
    fake.add(make_ctr())
    fake.add(make_ctr("web2", cid="b" * 64))
    u = Updater(fake)
    u.delete("web", remove_image=True)
    assert "image kept (still used by web2)" in u.logs[-1]["msg"]
    fake.fail["remove_image"] = _DockerError(409, "conflict")
    u.delete("web2", remove_image=True)
    assert "image kept (" in u.logs[-1]["msg"] and "web2" not in fake.ctrs


def test_delete_digest_pinned_image_by_id(fake):
    fake.add(make_ctr(image="test/app@sha256:abc"))
    u = Updater(fake)
    u.delete("web", remove_image=True)
    assert fake.removed == [OLD_IMG]


def test_delete_stopped_without_image(fake):
    c = make_ctr()
    c["State"] = {"Running": False}
    fake.add(c)
    u = Updater(fake)
    u.on_change = lambda n, st: 1 / 0  # a broken listener doesn't break deleting
    u.delete("web")
    assert "web" not in fake.ctrs and not getattr(fake, "stopped", []) and not getattr(fake, "removed", [])
    assert u.logs[-1]["msg"] == "deleted container web"


def test_delete_refuses_updock_itself(fake, monkeypatch):
    monkeypatch.setenv("HOSTNAME", CID[:12])
    fake.add(make_ctr())
    u = Updater(fake)
    u.delete("web")
    assert "web" in fake.ctrs and u.state["web"]["job"] == "error"
    assert "won't delete its own container" in u.state["web"]["message"]


def test_delete_forgets_saved_state(fake, tmp_path):
    from app.store import Store
    fake.add(make_ctr())
    u = Updater(fake, store=Store(str(tmp_path)))
    u.state["web"]["pinned"] = True
    u.delete("web")
    assert "web" not in Updater(fake, store=Store(str(tmp_path))).state


def test_update_email_details(fake):
    fake.images[OLD_IMG]["Config"] = {**IMG_CFG, "Labels": {"org.opencontainers.image.version": "1.0",
                                                            "com.docker.compose.project": "p"}}
    c = make_ctr()
    c["Config"]["Labels"] = {**c["Config"]["Labels"], "com.docker.compose.project": "shop",
                             "com.docker.compose.service": "web"}
    fake.add(c)
    publish_new(fake)
    rec = RecNotifier()
    u = Updater(fake, notifier=rec)
    u.update("web")
    d = rec.details[-1]
    assert d["image"] == "test/app:latest" and d["from"]["version"] == "1.0"
    assert d["project"] == "shop / web" and d["cleanup"] is True and d["duration"] >= 0


def test_error_email_context_survives_missing_container(fake):
    rec = RecNotifier()
    u = Updater(fake, notifier=rec)
    u.state["ghost"]["ref"] = "test/ghost:1"
    assert u._context("ghost") == {"image": "test/ghost:1"}
