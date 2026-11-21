import logging

import pytest

from app.updater import Updater, base_repo, build_create_body, image_info
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
    assert not avail and up.state["web"]["job"] == "pinned"
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
    assert any("rolling back" in l["msg"] for l in up.logs)
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
    monkeypatch.setattr(up, "update", lambda n: seen.append(("u", n)))
    monkeypatch.setattr(up, "check_only", lambda n: seen.append(("c", n)))
    up.run_all()
    up.run_all(update=False)
    assert seen == [("u", "web"), ("c", "web")]
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
