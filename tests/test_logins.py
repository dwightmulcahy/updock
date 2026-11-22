import json
import os
import stat

import pytest

from app import docker_api
from app.docker_api import HUB_KEY, normalize_registry, registry_credentials
from app.logins import RegistryLogins, display_host


@pytest.mark.parametrize("host,key", [
    ("ghcr.io", "ghcr.io"), ("https://GHCR.io/", "ghcr.io"), ("docker.io", HUB_KEY),
    ("registry-1.docker.io", HUB_KEY), ("https://index.docker.io/v1/", HUB_KEY), ("", HUB_KEY),
    ("reg.local:5000", "reg.local:5000"),
])
def test_normalize_registry(host, key):
    assert normalize_registry(host) == key


def test_display_host():
    assert display_host(HUB_KEY) == "docker.io" and display_host("ghcr.io") == "ghcr.io"


def test_persisted_with_0600_and_reload(tmp_path):
    path = str(tmp_path / "registries.json")
    lg = RegistryLogins(path)
    assert lg.set("https://ghcr.io", "me", "tok") == "ghcr.io"
    lg.set("docker.io", "hubber", "pw")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = RegistryLogins(path)
    assert again.get("ghcr.io") == ("me", "tok") and again.get(HUB_KEY) == ("hubber", "pw")
    assert again.list() == [{"host": "ghcr.io", "username": "me", "persisted": True},
                            {"host": "docker.io", "username": "hubber", "persisted": True}]
    assert "tok" not in json.dumps(again.list())
    assert again.remove("ghcr.io") is True and again.remove("ghcr.io") is False
    assert RegistryLogins(path).get("ghcr.io") is None


def test_memory_only_and_bad_files(tmp_path, caplog):
    mem = RegistryLogins(None)
    mem.set("quay.io", "u", "p")
    assert mem.get("quay.io") == ("u", "p") and mem.list()[0]["persisted"] is False
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert RegistryLogins(str(bad)).list() == []
    assert "could not read registry logins" in caplog.text
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"ghcr.io": "x", "quay.io": {"password": "p"}, "a.io": {"username": "u"}}))
    lg = RegistryLogins(str(odd))
    assert lg.get("a.io") == ("u", "") and lg.get("ghcr.io") is None and lg.get("quay.io") is None


def test_ui_logins_take_precedence_over_config(tmp_path, monkeypatch):
    import base64
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps(
        {"auths": {"ghcr.io": {"auth": base64.b64encode(b"cfg:cfgpw").decode()}}}))
    lg = RegistryLogins(None)
    monkeypatch.setattr(docker_api, "CREDENTIAL_PROVIDERS", [lg.get])
    assert registry_credentials("ghcr.io/me/app") == ("cfg", "cfgpw", "ghcr.io")
    lg.set("ghcr.io", "ui", "uipw")
    assert registry_credentials("ghcr.io/me/app") == ("ui", "uipw", "ghcr.io")
    lg.set("docker.io", "hub", "hubpw")
    assert registry_credentials("me/private") == ("hub", "hubpw", HUB_KEY)
    assert registry_credentials("docker.io/me/private") == ("hub", "hubpw", HUB_KEY)
