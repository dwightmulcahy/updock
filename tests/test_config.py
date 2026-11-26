import os
import subprocess

import pytest

from app import config
from app.config import ConfigError, load_env, resolve_secrets, secret

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_plain_env_and_default():
    assert secret("X", {"X": "v"}) == "v"
    assert secret("X", {}) == ""
    assert secret("X", {}, default="d") == "d"


def test_file_wins_and_is_stripped(tmp_path):
    f = tmp_path / "pw"
    f.write_text("  s3cret\n\n")
    assert secret("X", {"X": "plain", "X_FILE": str(f)}) == "s3cret"


def test_empty_file_means_unset(tmp_path):
    f = tmp_path / "empty"
    f.write_text("")
    assert secret("X", {"X_FILE": str(f)}) == ""


def test_blank_file_var_ignored():
    assert secret("X", {"X": "v", "X_FILE": "  "}) == "v"


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="X_FILE=.*No such file"):
        secret("X", {"X_FILE": str(tmp_path / "nope")})


def test_resolve_secrets_only_touches_known_names(tmp_path):
    f = tmp_path / "t"
    f.write_text("tok")
    env = resolve_secrets({"UPDOCK_API_TOKEN_FILE": str(f), "OTHER_FILE": "/x", "OTHER": "y"})
    assert env["UPDOCK_API_TOKEN"] == "tok" and env["UPDOCK_PASSWORD"] == ""
    assert env["UPDOCK_SMTP_PASSWORD"] == "" and env["OTHER"] == "y"


def test_resolve_secrets_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("UPDOCK_SMTP_PASSWORD", "fromenv")
    monkeypatch.delenv("UPDOCK_SMTP_PASSWORD_FILE", raising=False)
    assert resolve_secrets()["UPDOCK_SMTP_PASSWORD"] == "fromenv"
    assert config.secret("UPDOCK_SMTP_PASSWORD") == "fromenv"


def test_load_env_exits_cleanly(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDOCK_PASSWORD_FILE", str(tmp_path / "typo"))
    with pytest.raises(SystemExit, match="updock: cannot start: UPDOCK_PASSWORD_FILE="):
        load_env()
    monkeypatch.delenv("UPDOCK_PASSWORD_FILE")
    assert "UPDOCK_PASSWORD" in load_env()


def test_init_secrets_script(tmp_path):
    script = os.path.join(ROOT, "scripts", "init-secrets.sh")
    d = tmp_path / "secrets"
    out = subprocess.run([script, str(d)], capture_output=True, text=True, check=True).stdout
    pw = (d / "updock_password").read_text()
    assert len(pw) >= 24 and (d / "updock_api_token").read_text() != pw
    assert (d / "updock_smtp_password").read_text() == ""
    assert f"UI login: admin / {pw}" in out
    assert oct((d / "updock_password").stat().st_mode & 0o777) == "0o600"
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    (d / "updock_password").write_text("mine")
    out2 = subprocess.run([script, str(d)], capture_output=True, text=True, check=True).stdout
    assert "kept" in out2 and (d / "updock_password").read_text() == "mine"


@pytest.mark.parametrize("compose", ["compose.yaml", "compose.qnap.yaml"])
def test_compose_wires_secret_files(compose):
    import yaml
    doc = yaml.safe_load(open(os.path.join(ROOT, compose)))
    svc = doc["services"]["updock"]
    names = ["updock_password", "updock_api_token", "updock_smtp_password"]
    assert svc["secrets"] == names and "DAC_READ_SEARCH" in svc["cap_add"]
    assert {n: doc["secrets"][n]["file"] for n in names} == {n: f"./secrets/{n}" for n in names}


def test_env_example_references_secret_files():
    text = open(os.path.join(ROOT, ".env.example")).read()
    for n in ("PASSWORD", "API_TOKEN", "SMTP_PASSWORD"):
        assert f"UPDOCK_{n}_FILE=/run/secrets/updock_{n.lower()}" in text
    assert "UPDOCK_PASSWORD=" not in text.replace("UPDOCK_PASSWORD=...", "")


def test_socket_proxy_override_removes_socket_from_updock():
    import yaml
    doc = yaml.safe_load(open(os.path.join(ROOT, "examples/compose.socket-proxy.yaml")).read()
                         .replace("!override", ""))
    proxy, updock = doc["services"]["socket-proxy"], doc["services"]["updock"]
    assert proxy["labels"]["updock.enable"] == "false"
    assert proxy["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    assert not any("docker.sock" in v for v in updock["volumes"])
    assert updock["environment"]["DOCKER_HOST"] == "tcp://socket-proxy:2375"
    assert doc["networks"]["docker-api"]["internal"] is True
    for k in ("CONTAINERS", "IMAGES", "NETWORKS", "POST"):
        assert proxy["environment"][k] == "1"


@pytest.mark.parametrize("path,key", [
    (".github/workflows/release.yml", "platforms: linux/amd64,linux/arm64,linux/arm/v7"),
])
def test_images_published_for_arm_v7(path, key):
    assert key in open(os.path.join(ROOT, path)).read()


def test_known_covers_every_setting_the_code_reads():
    import re
    from pathlib import Path

    used = set()
    for py in (Path(__file__).parents[1] / "app").glob("*.py"):
        used |= set(re.findall(r'"(UPDOCK_[A-Z0-9_]+)"', py.read_text()))
    assert used - config.KNOWN == set()


def test_clean_env_strips_values_and_flags_typos():
    env = {"UPDOCK_ALLOW_SELF_UPDATE": "true\r", "UPDOCK_SELF_UPDATE": "true", "UPDOCK_CLEANUP": " yes ",
           "UPDOCK_ZZZQQQ": "1", "UPDOCK_PASSWORD_FILE": "/run/x", "PATH": " /bin "}
    unknown = config.clean_env(env)
    assert env["UPDOCK_ALLOW_SELF_UPDATE"] == "true" and env["UPDOCK_CLEANUP"] == "yes"
    assert env["PATH"] == " /bin "  # only UPDOCK_* values are touched
    assert unknown == [("UPDOCK_SELF_UPDATE", "UPDOCK_ALLOW_SELF_UPDATE"), ("UPDOCK_ZZZQQQ", None)]


def test_warn_unknown_logs_one_line_each(caplog):
    with caplog.at_level("WARNING", logger="updock"):
        config.warn_unknown([("UPDOCK_SELF_UPDATE", "UPDOCK_ALLOW_SELF_UPDATE"), ("UPDOCK_X", None)])
    assert [r.getMessage() for r in caplog.records] == [
        "unknown setting UPDOCK_SELF_UPDATE is ignored; did you mean UPDOCK_ALLOW_SELF_UPDATE?",
        "unknown setting UPDOCK_X is ignored"]
