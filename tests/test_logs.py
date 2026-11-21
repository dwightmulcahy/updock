import json
import logging

import pytest

from app import logs
from app.logs import JsonFormatter, LogBuffer, TextFormatter, format_text, setup


@pytest.fixture
def lg():
    lgr = logging.getLogger("test.logs")
    lgr.setLevel(logging.DEBUG)
    lgr.propagate = False
    yield lgr
    lgr.handlers.clear()


def test_buffer_captures_fields_and_exceptions(lg):
    b = LogBuffer(size=3)
    lg.addHandler(b)
    lg.info("one", extra={"container": "web", "event": "check"})
    try:
        raise ValueError("bad")
    except ValueError:
        lg.exception("two")
    e1, e2 = b.entries
    assert e1["container"] == "web" and e1["event"] == "check" and e1["level"] == "INFO"
    assert e1["id"] == 1 and e2["id"] == 2
    assert "ValueError: bad" in e2["exc"] and e2["container"] is None
    for i in range(5):
        lg.info(f"x{i}")
    assert len(b.entries) == 3 and b.entries[-1]["msg"] == "x4"  # ring buffer


def test_buffer_ignores_access_log():
    b = LogBuffer()
    acc = logging.getLogger("uvicorn.access")
    acc.addHandler(b)
    try:
        acc.warning("GET /api/containers 200")
    finally:
        acc.removeHandler(b)
    assert not b.entries


def test_listeners_called_and_isolated(lg):
    b = LogBuffer()
    lg.addHandler(b)
    seen = []
    b.listeners.append(lambda e: seen.append(e["msg"]))
    b.listeners.append(lambda e: 1 / 0)
    lg.info("hi")
    assert seen == ["hi"] and len(b.entries) == 1


def test_query_since_and_unknown_level(lg):
    b = LogBuffer()
    lg.addHandler(b)
    lg.debug("d")
    lg.info("i")
    t = b.entries[-1]["t"]
    assert [e["msg"] for e in b.query(level="nope")] == ["d", "i"]
    assert [e["msg"] for e in b.query(since=t)] == ["i"]
    assert b.query(limit=0) == list(b.entries)


def test_facets_empty():
    assert LogBuffer().facets() == {"containers": [], "events": [], "last_id": 0}


def test_load_continues_ids(lg):
    b = LogBuffer()
    b.load([{"id": 7, "t": 1, "level": "INFO", "msg": "old", "container": "a", "event": None}])
    lg.addHandler(b)
    lg.info("new")
    assert [e["id"] for e in b.entries] == [7, 8]
    b2 = LogBuffer()
    b2.load([])
    assert b2.facets()["last_id"] == 0


def test_format_text_variants():
    e = {"t": 0, "level": "ERROR", "msg": "m", "container": "c", "event": "ev", "exc": "TB"}
    out = format_text(e)
    assert "ERROR   [c] (ev) m\nTB" in out
    assert format_text({"t": 0, "level": "INFO", "msg": "plain"}).endswith("INFO    plain")


def _record(**extra):
    r = logging.LogRecord("updock", logging.WARNING, __file__, 1, "msg %s", ("x",), None)
    r.__dict__.update(extra)
    return r


def test_json_formatter():
    d = json.loads(JsonFormatter().format(_record(container="web")))
    assert d["msg"] == "msg x" and d["container"] == "web" and d["level"] == "WARNING"
    assert "event" not in d and "time" in d and "id" not in d


def test_text_formatter():
    assert TextFormatter().format(_record(event="pull")).endswith("WARNING (pull) msg x")


@pytest.mark.parametrize("fmt,cls", [("json", JsonFormatter), ("text", TextFormatter)])
def test_setup_idempotent(fmt, cls):
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    try:
        b1 = setup("debug", fmt, 10)
        b2 = setup("WARNING", fmt, 10)
        ours = [h for h in root.handlers if h not in before]
        assert b2 in ours and b1 not in ours and len(ours) == 2
        stream = next(h for h in ours if h is not b2)
        assert isinstance(stream.formatter, cls)
        assert not stream.filter(logging.LogRecord("uvicorn.access", 20, "", 0, "", (), None))
        assert root.level == logging.WARNING and b2.entries.maxlen == 10
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
        root.setLevel(level)


def test_setup_unknown_level_defaults_info():
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    try:
        setup("loud")
        assert root.level == logging.INFO
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
        root.setLevel(level)


def test_levels_cover_std():
    assert logs.LEVELS["ERROR"] == logging.ERROR
