"""Structured, queryable in-memory log shared by the UI, stdout and later sinks."""
import itertools
import json
import logging
import threading
import time
from collections import deque

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
QUIET_LOGGERS = {"uvicorn.access"}  # UI polling would drown everything else


def record_to_entry(record, entry_id):
    e = {
        "id": entry_id,
        "t": record.created,
        "level": record.levelname,
        "logger": record.name,
        "container": getattr(record, "container", None),
        "event": getattr(record, "event", None),
        "msg": record.getMessage(),
    }
    if record.exc_info:
        e["exc"] = logging.Formatter().formatException(record.exc_info)
    return e


class LogBuffer(logging.Handler):
    def __init__(self, size=2000):
        super().__init__()
        self.entries = deque(maxlen=size)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.listeners = []  # callables(entry), e.g. persistence or notifications

    def emit(self, record):
        if record.name in QUIET_LOGGERS:
            return
        with self._lock:
            entry = record_to_entry(record, next(self._ids))
            self.entries.append(entry)
        for fn in list(self.listeners):
            try:
                fn(entry)
            except Exception:  # a broken sink must never break logging
                pass

    def load(self, entries):
        """Seed with previously persisted entries (oldest first)."""
        with self._lock:
            for e in entries:
                self.entries.append(e)
            last = max((e.get("id", 0) for e in self.entries), default=0)
            self._ids = itertools.count(last + 1)

    def query(self, level=None, container=None, event=None, q=None, after=None,
              since=None, limit=500):
        min_level = LEVELS.get((level or "").upper(), 0)
        needle = (q or "").lower()
        with self._lock:
            items = list(self.entries)
        out = [
            e for e in items
            if LEVELS.get(e["level"], 0) >= min_level
            and (not container or e.get("container") == container)
            and (not event or e.get("event") == event)
            and (after is None or e["id"] > after)
            and (since is None or e["t"] >= since)
            and (not needle or needle in e["msg"].lower()
                 or needle in (e.get("container") or "").lower())
        ]
        return out[-limit:] if limit else out

    def facets(self):
        with self._lock:
            items = list(self.entries)
        return {
            "containers": sorted({e["container"] for e in items if e.get("container")}),
            "events": sorted({e["event"] for e in items if e.get("event")}),
            "last_id": items[-1]["id"] if items else 0,
        }


def format_text(e):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["t"]))
    ctr = f" [{e['container']}]" if e.get("container") else ""
    ev = f" ({e['event']})" if e.get("event") else ""
    line = f"{ts} {e['level']:<7}{ctr}{ev} {e['msg']}"
    return line + ("\n" + e["exc"] if e.get("exc") else "")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        e = record_to_entry(record, None)
        e.pop("id")
        e["time"] = self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z")
        return json.dumps({k: v for k, v in e.items() if v is not None})


class TextFormatter(logging.Formatter):
    def format(self, record):
        return format_text(record_to_entry(record, None))


def setup(level="INFO", fmt="text", size=2000):
    """Configure root logging: stdout (text or JSON) + the queryable buffer."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, LogBuffer) or getattr(h, "_updock", False):
            root.removeHandler(h)
    stream = logging.StreamHandler()
    stream._updock = True
    stream.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    stream.addFilter(lambda r: r.name not in QUIET_LOGGERS)
    buf = LogBuffer(size)
    root.addHandler(stream)
    root.addHandler(buf)
    root.setLevel(LEVELS.get(level.upper(), logging.INFO))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return buf
