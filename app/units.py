"""Byte sizes: '40GB', '20GiB', '512M', '1048576' <-> bytes."""
import re

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?)(i?)b?\s*$", re.I)
_POW = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}


def parse_size(text):
    """Decimal units (KB, MB, GB, TB = 1000^n) or binary (KiB, MiB, GiB = 1024^n)."""
    m = _SIZE.match(str(text))
    if not m:
        raise ValueError(f"invalid size {text!r} (use e.g. 40GB, 20GiB, 512MB)")
    base = 1024 if m[3] else 1000
    return int(float(m[1]) * base ** _POW[m[2].lower()])


def parse_threshold(text, maximum=None):
    """A size, or a percentage of `maximum` (e.g. '80%')."""
    t = str(text).strip()
    if t.endswith("%"):
        if not maximum:
            raise ValueError(f"{text!r} is a percentage but no maximum is set")
        pct = float(t[:-1])
        if not 0 < pct <= 100:
            raise ValueError(f"invalid percentage {text!r}")
        return int(maximum * pct / 100)
    return parse_size(t)


def format_size(n):
    n = float(n or 0)
    if abs(n) < 1000:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1000
        if abs(n) < 1000:
            return f"{n:.1f} {unit}"
    return f"{n / 1000:.1f} TB"
