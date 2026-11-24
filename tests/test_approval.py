"""Version-step approval: updock.allow / UPDOCK_ALLOW."""
import pytest

from app import semver
from app.updater import Updater
from conftest import IMG_CFG, OLD_IMG, make_ctr
import test_semver
from test_semver import TagRegistry, semver_ctr
from test_updater import DigestRegistry, RecNotifier, publish_new


@pytest.mark.parametrize("old,new,want", [
    ("1.2.3", "1.2.9", "patch"), ("v1.2.3", "v1.3.0", "minor"), ("1.2.3", "2.0.0", "major"),
    ("1.2", "1.2.1", "patch"), ("1.2.3-alpine", "1.3.0-alpine", "minor"),
    ("1.2.3", "1.2.3", None), ("2.0.0", "1.9.9", None), ("latest", "1.0.0", None), (None, "1", None),
])
def test_jump(old, new, want):
    assert semver.jump(old, new) == want


def test_exceeds():
    assert semver.exceeds("major", "minor") and semver.exceeds("minor", "patch")
    assert not semver.exceeds("patch", "minor") and not semver.exceeds("major", "major")


@pytest.fixture
def aup(fake, monkeypatch):
    """web on test/app:latest at image version 1.2.3; the registry offers `new`."""
    monkeypatch.setenv("HOSTNAME", "selfhost")
    fake.images[OLD_IMG].update(RepoDigests=["test/app@sha256:local"],
                                Config={**IMG_CFG, "Labels": {"org.opencontainers.image.version": "1.2.3"}})

    def make(new="2.0.0", allow=None, labels=None):
        reg = DigestRegistry(info={"id": "new", "digest": "sha256:remote", "tags": [],
                                   "version": new, "revision": None, "created": None})
        ctr = make_ctr()
        ctr["Config"]["Labels"].update(labels or {})
        fake.add(ctr)
        publish_new(fake)
        return Updater(fake, registry=reg, allow=allow, notifier=RecNotifier())
    return make


def test_major_step_waits_for_approval_and_notifies_once(aup, fake):
    u = aup(allow="minor")
    u.update("web", scheduled=True)
    st = u.state["web"]
    assert st["job"] == "approval" and "1.2.3 -> 2.0.0 is a major update" in st["message"]
    assert "pull" not in fake.calls and "create" not in fake.calls
    assert u.counters["result_approval"] == 1
    u.update("web", scheduled=True)
    u.check_only("web", scheduled=True)
    assert [i[0] for i in u.notifier.items] == ["approval"]
    assert u.notifier.details[0]["reason"].startswith("1.2.3 -> 2.0.0")
    row = u.list()[0]
    assert row["approval"]["level"] == "major" and row["approval"]["allow"] == "minor"
    assert row["approval"]["from"] == "1.2.3" and row["approval"]["to"] == "2.0.0"


def test_approve_updates_and_remembers(aup, fake):
    u = aup(allow="patch")
    u.check_only("web")
    assert u.state["web"]["job"] == "approval"
    u.update("web", approve=True)
    st = u.state["web"]
    assert st["job"] == "updated" and st["approved"] == "sha256:remote"
    assert any(e["msg"] == "approved: 1.2.3 -> 2.0.0" for e in u.logs)
    assert u.list()[0].get("approval") is None


def test_approval_survives_cooldown(aup, fake):
    u = aup(allow="patch")
    u.cooldown = 86400
    u.update("web", approve=True)
    assert u.state["web"]["job"] == "cooldown" and u.state["web"]["approved"] == "sha256:remote"
    u.cooldown = 0
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "updated"


def test_approve_without_pending_gate_just_updates(aup, fake):
    u = aup(new="1.2.4", allow="patch")
    u.update("web", approve=True)
    assert u.state["web"]["job"] == "updated" and "approved" not in u.state["web"]


@pytest.mark.parametrize("new,allow,labels", [
    ("1.2.9", "patch", None), ("1.9.0", "minor", None), ("2.0.0", None, None),
    ("2.0.0", "minor", {"updock.allow": "major"}),
])
def test_within_allowed_versions_update(aup, fake, new, allow, labels):
    u = aup(new=new, allow=allow, labels=labels)
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "updated" and u.notifier.items[-1][0] == "update"


def test_label_tightens_global_and_bad_values_warn_once(aup, fake):
    u = aup(new="1.3.0", labels={"updock.allow": "patch"})
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "approval"
    fake.ctrs["web"]["Config"]["Labels"]["updock.allow"] = "huge"
    u.update("web", scheduled=True)
    u.update("web", scheduled=True)
    assert [e["msg"] for e in u.logs if "invalid" in e["msg"]] == [
        "invalid updock.allow='huge' (use patch, minor or major); ignoring it"]
    assert Updater(fake, allow="nope").allow is None
    assert Updater(fake, allow=" Minor ").allow == "minor"


sfake = pytest.fixture(test_semver.sfake.__wrapped__)


def test_semver_tag_following_compares_tags(sfake):
    ctr = semver_ctr(level="major")
    ctr["Config"]["Labels"]["updock.allow"] = "minor"
    sfake.add(ctr)
    sfake.tags["test/app:2.0.0"] = sfake.tags["test/app:1.4.10"]
    u = Updater(sfake, registry=TagRegistry(), notifier=RecNotifier())
    u.update("web", scheduled=True)
    st = u.state["web"]
    assert st["job"] == "approval" and st["message"].startswith("1.4.2 -> 2.0.0 is a major update")
    u.update("web", approve=True)
    assert u.state["web"]["job"] == "updated" and sfake.created_body["Image"] == "test/app:2.0.0"


@pytest.mark.parametrize("new", ["latest-build", None, "1.2.2"])
def test_unclassifiable_or_downgrade_requires_approval(aup, fake, new):
    u = aup(new=new, allow="patch")
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "approval"
    assert "create" not in fake.calls
    assert u.notifier.items[0][0] == "approval"
    u.update("web", approve=True)
    assert u.state["web"]["job"] == "updated"


def test_same_version_rebuild_allowed(aup):
    u = aup(new="1.2.3", allow="patch")
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "updated"


def test_gui_policy_persists_and_overrides_label(aup, fake, tmp_path):
    from app.store import Store
    u = aup(new="1.3.0", allow="major", labels={"updock.allow": "minor"})
    u.store = Store(str(tmp_path))
    assert u.set_version_policy("web", "patch") == {
        "override": "patch", "effective": "patch", "source": "gui"}
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "approval"
    v = Updater(fake, store=u.store, allow="major")
    assert v.list()[0]["version_policy"]["effective"] == "patch"
    assert v.set_version_policy("web", "inherit")["effective"] == "minor"
    with pytest.raises(ValueError):
        v.set_version_policy("web", "invalid")


def test_gui_policy_discovers_newer_major_tag(sfake):
    sfake.add(semver_ctr(level="patch"))
    u = Updater(sfake, registry=TagRegistry(), notifier=RecNotifier())
    u.set_version_policy("web", "patch")
    u.check_only("web")
    assert u.state["web"]["job"] == "approval"
    assert u.state["web"]["target_ref"] == "test/app:2.0.0"
