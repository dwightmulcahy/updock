import json

import pytest

from app import envfile
from app.config import KNOWN
from app.settings_schema import SETTINGS

TEXT = """# Updock settings
export TZ=America/Costa_Rica
UPDOCK_SCHEDULE="0 4 * * *"
UPDOCK_CLEANUP=yes   # inline comment
EXEC=0
# UPDOCK_COOLDOWN=1d  (commented out)
UPDOCK_COOLDOWN=1d
UPDOCK_COOLDOWN=2d
"""


def test_schema_covers_known_settings_except_secrets_and_wiring():
    keys = [s[0] for s in SETTINGS]
    assert len(keys) == len(set(keys))
    assert set(keys) - {"TZ"} <= KNOWN
    assert KNOWN - set(keys) == {
        "UPDOCK_PASSWORD", "UPDOCK_API_TOKEN", "UPDOCK_SMTP_PASSWORD", "UPDOCK_PASSWORD_FILE",
        "UPDOCK_API_TOKEN_FILE", "UPDOCK_SMTP_PASSWORD_FILE", "UPDOCK_DATA_DIR", "UPDOCK_SELF",
        "UPDOCK_ENV_FILE"}
    for key, _group, kind, default, _help in SETTINGS:  # every default passes its own check
        assert envfile.check(key, default) is None, key


def test_parse():
    assert envfile.parse(TEXT) == {"TZ": "America/Costa_Rica", "UPDOCK_SCHEDULE": "0 4 * * *",
                                   "UPDOCK_CLEANUP": "yes", "EXEC": "0", "UPDOCK_COOLDOWN": "2d"}


def test_render_edits_in_place_keeps_the_rest_and_appends_new_keys():
    out = envfile.render(TEXT, {"UPDOCK_COOLDOWN": "3d", "UPDOCK_CLEANUP": None,
                                "UPDOCK_ALLOW": "minor", "UPDOCK_URL": "a b", "UPDOCK_JITTER": None})
    assert out.splitlines() == [
        "# Updock settings", "export TZ=America/Costa_Rica", 'UPDOCK_SCHEDULE="0 4 * * *"', "EXEC=0",
        "# UPDOCK_COOLDOWN=1d  (commented out)", "UPDOCK_COOLDOWN=3d", "",
        "# set from the Updock Settings tab", "UPDOCK_ALLOW=minor", 'UPDOCK_URL="a b"']
    assert envfile.parse(out)["UPDOCK_URL"] == "a b"
    assert envfile.render("", {"UPDOCK_ALLOW": "patch"}) == "# set from the Updock Settings tab\nUPDOCK_ALLOW=patch\n"
    assert envfile.render("A=1\n", {"A": None}) == "\n"  # nothing new to append


@pytest.mark.parametrize("key,value,why", [
    ("UPDOCK_PASSWORD", "x", "not editable here"), ("UPDOCK_URL", "a\nb", "must be a single line"),
    ("UPDOCK_CLEANUP", "maybe", "use true or false"), ("UPDOCK_INTERVAL", "1h", "invalid"),
    ("UPDOCK_JITTER", "soon", "invalid"), ("UPDOCK_DISK_MAX", "lots", "invalid"),
    ("UPDOCK_DISK_WARN", "x%", "invalid"), ("UPDOCK_SCHEDULE", "61 * * * *", "invalid"),
    ("UPDOCK_WINDOW", "late", "invalid"), ("TZ", "Mars/Olympus", "invalid"),
    ("UPDOCK_ALLOW", "huge", "use one of: patch, minor, major"),
])
def test_check_rejects(key, value, why):
    assert envfile.check(key, value).startswith(why)


@pytest.mark.parametrize("key,value", [
    ("UPDOCK_CLEANUP", "FALSE"), ("UPDOCK_INTERVAL", "3600"), ("UPDOCK_JITTER", "5m"), ("UPDOCK_DISK_MAX", "40GB"),
    ("UPDOCK_DISK_WARN", "80%"), ("UPDOCK_SCHEDULE", "@daily"), ("UPDOCK_WINDOW", "02:00-05:00"),
    ("TZ", "Europe/Berlin"), ("UPDOCK_LOG_LEVEL", "DEBUG"), ("UPDOCK_URL", ""), ("UPDOCK_EXCLUDE", "db,cache*"),
])
def test_check_accepts(key, value):
    assert envfile.check(key, value) is None


def test_check_message_without_detail(monkeypatch):
    def boom(_):
        raise ValueError()
    monkeypatch.setattr(envfile, "parse_duration", boom)
    assert envfile.check("UPDOCK_JITTER", "1m") == "invalid"


def test_write_in_place_and_record(tmp_path):
    f = tmp_path / "updock.env"
    f.write_text("A=1\nLONGER=line\n")
    envfile.write(str(f), "A=2\n")
    assert envfile.read(str(f)) == "A=2\n"
    assert envfile.recorded(str(tmp_path)) == set()
    envfile.record(str(tmp_path), ["UPDOCK_ALLOW"])
    envfile.record(str(tmp_path), {"UPDOCK_CLEANUP": None})
    assert json.loads((tmp_path / envfile.RECORD).read_text()) == ["UPDOCK_ALLOW", "UPDOCK_CLEANUP"]
    (tmp_path / envfile.RECORD).write_text("{bad")
    assert envfile.recorded(str(tmp_path)) == set()


def test_apply_takes_only_ui_saved_keys_from_the_file(tmp_path):
    f = tmp_path / "updock.env"
    f.write_text("UPDOCK_ALLOW=minor\nUPDOCK_CLEANUP=false\nUPDOCK_URL=\n")
    env = {"UPDOCK_ALLOW": "major", "UPDOCK_CLEANUP": "true", "UPDOCK_URL": "http://x", "UPDOCK_JITTER": "2m"}
    assert envfile.apply(str(f), str(tmp_path), env) == []  # nothing saved from the UI yet
    envfile.record(str(tmp_path), ["UPDOCK_ALLOW", "UPDOCK_URL", "UPDOCK_JITTER", "UPDOCK_PASSWORD"])
    assert envfile.apply(str(f), str(tmp_path), env) == ["UPDOCK_ALLOW", "UPDOCK_JITTER", "UPDOCK_URL"]
    assert env == {"UPDOCK_ALLOW": "minor", "UPDOCK_CLEANUP": "true"}  # empty/missing = default
    assert envfile.apply("", str(tmp_path), env) == []
    assert envfile.apply(str(tmp_path / "gone.env"), str(tmp_path), env) == []


def test_announce(caplog):
    with caplog.at_level("INFO", logger="updock"):
        envfile.announce([], "/config/updock.env")
        envfile.announce(["UPDOCK_ALLOW", "TZ"], "/config/updock.env")
    assert [r.getMessage() for r in caplog.records] == [
        "settings saved in the Settings tab: UPDOCK_ALLOW, TZ (from /config/updock.env)"]
