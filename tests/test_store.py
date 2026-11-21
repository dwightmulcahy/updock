import json
import logging
import os

import pytest

from app.logs import LogBuffer
from app.store import Store


def test_open_creates_dir(tmp_path):
    s = Store.open(str(tmp_path / "data"))
    assert s and os.path.isdir(tmp_path / "data")
    assert not os.path.exists(tmp_path / "data" / ".write-test")


def test_open_unwritable_returns_none(tmp_path, caplog):
    f = tmp_path / "file"
    f.write_text("x")
    with caplog.at_level(logging.WARNING, logger="updock"):
        assert Store.open(str(f / "sub")) is None
    assert "will not survive restarts" in caplog.records[-1].getMessage()


def test_state_roundtrip_and_interrupted_jobs(tmp_path):
    s = Store(str(tmp_path))
    assert s.load_state() == {}
    s.save_state({"web": {"job": "updated", "last_check": 1.5},
                  "db": {"job": "pulling", "message": "Pulling x"},
                  "app": {"job": "checking"}, "z": {"job": "updating"}})
    st = Store(str(tmp_path)).load_state()
    assert st["web"] == {"job": "updated", "last_check": 1.5}
    for n in ("db", "app", "z"):
        assert st[n] == {"job": "error", "message": "interrupted by an Updock restart"}
    assert not os.path.exists(s.state_path + ".tmp")


@pytest.mark.parametrize("content", ["{broken", "[1, 2]"])
def test_corrupt_or_wrong_state(tmp_path, caplog, content):
    s = Store(str(tmp_path))
    with open(s.state_path, "w") as f:
        f.write(content)
    assert s.load_state() == {}
    if content.startswith("{"):
        assert os.path.exists(s.state_path + ".corrupt")
        assert "starting fresh" in caplog.records[-1].getMessage()


def test_corrupt_state_rename_failure_ignored(tmp_path, monkeypatch):
    s = Store(str(tmp_path))
    with open(s.state_path, "w") as f:
        f.write("{bad")
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("ro")))
    assert s.load_state() == {}


def test_save_state_error_logged(tmp_path, caplog):
    s = Store(str(tmp_path / "missing"))
    with caplog.at_level(logging.ERROR, logger="updock"):
        s.save_state({"a": {}})
    assert "could not save state" in caplog.records[-1].getMessage()


def test_save_state_non_json_values(tmp_path):
    s = Store(str(tmp_path))
    s.save_state({"a": {"x": object}})  # default=str fallback
    assert "class" in json.load(open(s.state_path))["containers"]["a"]["x"]


def test_logs_append_rotate_and_load(tmp_path):
    s = Store(str(tmp_path), max_log_bytes=200, log_backups=2)
    for i in range(30):
        s.append_log({"id": i, "msg": f"entry {i:02d}"})
    assert os.path.exists(s.log_path + ".1") and os.path.exists(s.log_path + ".2")
    assert not os.path.exists(s.log_path + ".3")
    ids = [e["id"] for e in s.load_logs(0)]
    assert ids == sorted(ids) and ids[-1] == 29 and len(ids) < 30  # oldest rotated away
    assert [e["id"] for e in s.load_logs(3)] == [27, 28, 29]


def test_rotate_without_backups(tmp_path):
    s = Store(str(tmp_path), max_log_bytes=50, log_backups=0)
    for i in range(10):
        s.append_log({"id": i})
    assert not os.path.exists(s.log_path + ".1")
    assert s.load_logs(0)[-1]["id"] == 9


def test_load_logs_skips_bad_lines_and_missing(tmp_path):
    s = Store(str(tmp_path))
    assert s.load_logs(10) == []
    with open(s.log_path, "w") as f:
        f.write('{"id": 1}\nnot json\n{"id": 2}\n')
    assert [e["id"] for e in s.load_logs(10)] == [1, 2]


def test_append_log_errors_swallowed(tmp_path):
    Store(str(tmp_path / "missing")).append_log({"id": 1})  # no exception


def test_buffer_persistence_end_to_end(tmp_path):
    from app import main
    b1 = LogBuffer()
    store = main.init_store(str(tmp_path), b1)
    lg = logging.getLogger("t.persist")
    lg.propagate, lg.level = False, logging.INFO
    lg.addHandler(b1)
    lg.info("before restart", extra={"container": "web"})
    lg.removeHandler(b1)
    b2 = LogBuffer()
    main.init_store(str(tmp_path), b2)
    assert [e["msg"] for e in b2.entries] == ["before restart"]
    lg.addHandler(b2)
    lg.info("after restart")
    lg.removeHandler(b2)
    assert [e["id"] for e in b2.entries] == [1, 2]
    assert [e["msg"] for e in store.load_logs(0)] == ["before restart", "after restart"]
    f = tmp_path / "f"
    f.write_text("")
    assert main.init_store(str(f / "x"), LogBuffer()) is None
