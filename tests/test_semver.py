import pytest

from app import semver
from app.registry import RateLimited, RegistryAuthError, RegistryError
from app.updater import Updater
from conftest import IMG_CFG, NEW_IMG, OLD_IMG, make_ctr

TAGS = ["latest", "2.0.0", "1.5.0", "1.4.10", "1.4.9-alpine", "1.4.3", "1.4.2", "1.4",
        "v1.9.9", "1.4.11-rc1", "1.3.0"]


@pytest.mark.parametrize("cur,level,want", [
    ("1.4.2", "patch", "1.4.10"),
    ("1.4.2", "minor", "1.5.0"),
    ("1.4.2", "major", "2.0.0"),
    ("1.4.10", "patch", None),
    ("1.4.2-alpine", "patch", "1.4.9-alpine"),
    ("1.4", "minor", None),
    ("1.4", "patch", None),
    ("v1.0.0", "major", "v1.9.9"),
    ("latest", "major", None),
    ("1.4.2", "bogus", None),
])
def test_best(cur, level, want):
    assert semver.best(cur, TAGS, level) == want


def test_parse():
    assert semver.parse("v1.2.3-alpine") == ("v", (1, 2, 3), "-alpine")
    assert semver.parse("1") == ("", (1,), "")
    assert semver.parse("1.2.3.4") is None and semver.parse(None) is None


class TagRegistry:
    def __init__(self, tags=TAGS, info=None, tags_fail=None, info_fail=None):
        self._tags, self.info, self.tags_fail, self.info_fail = tags, info, tags_fail, info_fail
        self.info_refs, self.platforms = [], []

    def tags(self, ref):
        if self.tags_fail:
            raise self.tags_fail
        return self._tags

    def digest(self, ref):
        return "sha256:local"

    def remote_info(self, ref, platform):
        self.info_refs.append(ref)
        self.platforms.append(platform)
        if self.info_fail:
            raise self.info_fail
        return dict(self.info or {"id": "new", "digest": "sha256:n", "tags": [],
                                  "version": None, "revision": None, "created": None})


def semver_ctr(level="patch", tag="1.4.2"):
    return make_ctr(image=f"test/app:{tag}",
                    Config={**make_ctr()["Config"], "Image": f"test/app:{tag}",
                            "Labels": {"updock.semver": level}})


@pytest.fixture
def sfake(fake):
    fake.images[OLD_IMG].update(RepoDigests=["test/app@sha256:local"], Os="linux",
                                Architecture="amd64")
    fake.images[NEW_IMG] = {"Id": NEW_IMG, "Config": IMG_CFG}
    fake.tags["test/app:1.4.10"] = NEW_IMG
    return fake


def test_update_moves_to_newer_patch_tag(sfake):
    sfake.add(semver_ctr())
    reg = TagRegistry()
    u = Updater(sfake, registry=reg)
    u.check_only("web")
    st = u.state["web"]
    assert st["job"] == "update-available" and st["target_ref"] == "test/app:1.4.10"
    assert st["latest"]["tag"] == "1.4.10" and reg.info_refs == ["test/app:1.4.10"]
    assert reg.platforms[0]["architecture"] == "amd64"
    assert u.list()[0]["target_ref"] == "test/app:1.4.10"
    u.update("web")
    assert sfake.pulled == "test/app:1.4.10"
    assert sfake.created_body["Image"] == "test/app:1.4.10"
    st = u.state["web"]
    assert st["job"] == "updated" and st["ref"] == "test/app:1.4.10" and st["target_ref"] is None
    assert any("1.4.2 -> 1.4.10" in e["msg"] for e in u.logs)


def test_change_text_names_target(sfake):
    sfake.add(semver_ctr())
    u = Updater(sfake, registry=TagRegistry())
    u.check("web")
    assert "-> test/app:1.4.10" in u._describe_change(sfake.inspect("web"))


def test_no_newer_tag_falls_back_to_digest_check(sfake):
    sfake.add(semver_ctr(tag="1.4.10"))
    sfake.tags["test/app:1.4.10"] = OLD_IMG
    u = Updater(sfake, registry=TagRegistry())
    assert u.check("web")[1] is False
    assert u.state["web"]["job"] == "up-to-date" and u.state["web"]["target_ref"] is None


def test_details_unavailable_and_no_platform(sfake, monkeypatch):
    sfake.add(semver_ctr())
    u = Updater(sfake, registry=TagRegistry(info_fail=RegistryError("boom")))
    real = sfake.image
    monkeypatch.setattr(sfake, "image", lambda ref: (_ for _ in ()).throw(KeyError(ref))
                        if ref == OLD_IMG else real(ref))
    u.check("web")
    latest = u.state["web"]["latest"]
    assert latest["tag"] == "1.4.10" and "details unavailable" in latest["note"]


def test_tag_listing_error_falls_back(sfake):
    sfake.add(semver_ctr())
    u = Updater(sfake, registry=TagRegistry(tags_fail=RegistryError("nope")))
    assert u.check("web")[1] is False
    assert any("couldn't list tags" in e["msg"] for e in u.logs)


def test_tag_listing_rate_limit_and_auth(sfake):
    sfake.add(semver_ctr())
    u = Updater(sfake, registry=TagRegistry(tags_fail=RateLimited("docker.io")))
    u.check_only("web")
    assert u.state["web"]["job"] == "rate-limited"
    u = Updater(sfake, registry=TagRegistry(tags_fail=RegistryAuthError("ghcr.io", "x")))
    u.check_only("web")
    assert u.state["web"]["job"] == "auth-required"


def test_invalid_label_warns_once(sfake):
    sfake.add(semver_ctr(level="huge"))
    u = Updater(sfake, registry=TagRegistry())
    u.check("web")
    u.check("web")
    warn = [e for e in u.logs if "invalid updock.semver" in e["msg"]]
    assert len(warn) == 1 and warn[0]["level"] == "WARNING"
    assert u.state["web"].get("target_ref") is None


def test_label_ignored_without_registry(sfake):
    sfake.add(semver_ctr())
    u = Updater(sfake)
    u.check("web")
    assert u.state["web"]["target_ref"] is None and sfake.pulled == "test/app:1.4.2"
