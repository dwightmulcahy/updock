"""The Settings tab's env file: read it, validate edits, write them back in place, and
apply them on the next start.

Compose reads `env_file` only when it creates a container, so a restart alone would keep
the old values. Updock therefore records which keys were saved from the UI
(<data dir>/settings-saved.json) and, at startup, takes those keys from the file. Keys
nobody edited in the UI keep Compose's normal precedence (`environment:` over the file)."""
import json
import logging
import os
import re
from zoneinfo import ZoneInfo

from .schedule import Cron, Window, parse_duration
from .settings_schema import SETTINGS
from .units import parse_size, parse_threshold

RECORD = "settings-saved.json"
EDITABLE = {s[0]: s for s in SETTINGS}
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_BOOLS = ("1", "0", "true", "false", "yes", "no")


def _unquote(raw):
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return re.sub(r"\s+#.*$", "", v)  # unquoted: an inline " # comment" isn't part of the value


def parse(text):
    """{KEY: value} of an env file (last assignment wins; comments and blanks skipped)."""
    out = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            out[m[1]] = _unquote(m[2])
    return out


def _quote(value):
    return f'"{value}"' if value != value.strip() or "#" in value or " " in value else value


def render(text, changes):
    """`text` with each KEY in `changes` set (value) or removed (None). Lines are edited in
    place, so comments, order and keys Updock doesn't own (the socket proxy's) survive;
    new keys are appended."""
    lines, done = text.splitlines(), set()
    out = []
    for line in lines:
        m = _LINE.match(line)
        key = m[1] if m and not line.lstrip().startswith("#") else None
        if key in changes:
            if key not in done and changes[key] is not None:
                out.append(f"{key}={_quote(changes[key])}")
            done.add(key)
            continue
        out.append(line)
    new = [k for k, v in changes.items() if k not in done and v is not None]
    if new:
        if out and out[-1].strip():
            out.append("")
        out.append("# set from the Updock Settings tab")
        out += [f"{k}={_quote(changes[k])}" for k in new]
    return "\n".join(out) + "\n"


def check(key, value):
    """None if `value` is acceptable for `key`, else why not."""
    if key not in EDITABLE:
        return "not editable here"
    if any(c in value for c in "\r\n\0"):
        return "must be a single line"
    if value == "":
        return None  # empty = the default
    kind = EDITABLE[key][2]
    try:
        if kind == "bool" and value.lower() not in _BOOLS:
            return "use true or false"
        if kind == "int":
            int(value)
        elif kind == "duration":
            parse_duration(value)
        elif kind == "size":
            parse_size(value)
        elif kind == "threshold":
            parse_threshold(value, 100)
        elif kind == "cron":
            Cron(value)
        elif kind == "window":
            Window(value)
        elif kind == "tz":
            ZoneInfo(value)
        elif kind.startswith("choice:") and value not in kind[7:].split(","):
            return f"use one of: {kind[7:].replace(',', ', ')}"
    except Exception as e:
        return f"invalid ({e})" if str(e) else "invalid"
    return None


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    """Rewrite in place: the env file is usually a single-file bind mount, which a
    rename-over (atomic replace) can't target."""
    with open(path, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write(text)
        f.truncate()


def recorded(data_dir):
    try:
        with open(os.path.join(data_dir, RECORD)) as f:
            return set(json.load(f))
    except (OSError, ValueError, TypeError):
        return set()


def record(data_dir, keys):
    keys = sorted(recorded(data_dir) | set(keys))
    with open(os.path.join(data_dir, RECORD), "w") as f:
        json.dump(keys, f)


def announce(keys, path, log=None):
    """Startup log line naming the settings taken from the env file."""
    if keys:
        (log or logging.getLogger("updock")).info(
            "settings saved in the Settings tab: %s (from %s)", ", ".join(keys), path, extra={"event": "config"})


def apply(path, data_dir, environ=None):
    """At startup: take every key saved from the UI from the env file (removed from the
    file = back to the default). Returns the keys applied. Missing file: nothing."""
    environ = os.environ if environ is None else environ
    keys = recorded(data_dir) & set(EDITABLE)
    if not path or not keys:
        return []
    try:
        values = parse(read(path))
    except OSError:
        return []
    for key in sorted(keys):
        if values.get(key, "") != "":
            environ[key] = values[key]
        else:
            environ.pop(key, None)
    return sorted(keys)
