import logging

import pytest

from app.updater import Updater, build_create_body
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
    assert up.logs[-1]["msg"] == "web: up to date"
    publish_new(fake)
    up.check_only("web")
    assert up.logs[-1]["msg"] == "web: update available"
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
    assert "create" not in fake.calls and up.logs[-1]["msg"] == "web: up to date"


def test_update_full_flow(up, fake):
    fake.add(make_ctr())
    fake.add(make_ctr("other", cid="o" * 64))  # shares old image? no: other uses OLD_IMG too
    publish_new(fake)
    up.update("web")
    assert up.state["web"]["job"] == "updated"
    assert fake.ctrs["web"]["Image"] == NEW_IMG
    assert "web-updock-old" not in fake.ctrs
    assert fake.connected == [("net2", {"Aliases": ["webby"]})]
    assert fake.removed_image == OLD_IMG
    assert fake.stop_timeout == 10
    assert up.logs[-1]["msg"] == "web: updated"


def test_update_force_recreate_no_cleanup_same_image(up, fake):
    fake.add(make_ctr())
    up.update("web", force=True)
    assert "create" in fake.calls and "remove_image" not in fake.calls
    assert up.logs[-1]["msg"] == "web: recreated"


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
    assert up.logs[-1]["msg"] == "Run finished"
