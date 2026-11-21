"""Cron expressions, maintenance windows, durations and timestamps (no dependencies)."""
import re
from datetime import datetime, timedelta

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
           "@annually": "0 0 1 1 *"}
MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
# (min, max, names) for minute, hour, day-of-month, month, day-of-week
FIELDS = [(0, 59, {}), (0, 23, {}), (1, 31, {}), (1, 12, MONTHS), (0, 7, DAYS)]


def _value(tok, lo, hi, names):
    v = names.get(tok.lower()) if not tok.isdigit() else int(tok)
    if v is None or not lo <= v <= hi:
        raise ValueError(f"{tok!r} is not in {lo}-{hi}")
    return v


def parse_field(text, lo, hi, names):
    out = set()
    for part in text.split(","):
        rng, _, step = part.partition("/")
        step = int(step) if step else 1
        if step < 1:
            raise ValueError(f"bad step in {part!r}")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (_value(x, lo, hi, names) for x in rng.split("-", 1))
            if a > b:
                raise ValueError(f"bad range {rng!r}")
        else:
            a = _value(rng, lo, hi, names)
            b = hi if step > 1 or "/" in part else a
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr):
        self.expr = expr.strip()
        parts = ALIASES.get(self.expr.lower(), self.expr).split()
        if len(parts) != 5:
            raise ValueError(f"cron needs 5 fields (minute hour day month weekday): {expr!r}")
        try:
            sets = [parse_field(p, *f) for p, f in zip(parts, FIELDS)]
        except ValueError as e:
            raise ValueError(f"invalid cron {expr!r}: {e}") from None
        self.minutes, self.hours, self.days, self.months, dow = (sorted(x) for x in sets)
        self.weekdays = {d % 7 for d in dow}
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"

    def _day_ok(self, d):
        dom, dow = d.day in self.days, (d.isoweekday() % 7) in self.weekdays
        if self.dom_any or self.dow_any:  # standard cron: OR only when both are restricted
            return dom and dow
        return dom or dow

    def next(self, after):
        """First matching time strictly after `after` (keeps its tzinfo)."""
        start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.replace(hour=0, minute=0)
        for _ in range(366 * 5):
            if day.month in self.months and self._day_ok(day):
                for h in self.hours:
                    for m in self.minutes:
                        t = day.replace(hour=h, minute=m)
                        if t >= start:
                            return t
            day = (day + timedelta(days=1)).replace(hour=0, minute=0)
        raise ValueError(f"cron {self.expr!r} never matches")

    def __str__(self):
        return self.expr


class Window:
    """Daily maintenance window like '02:00-05:00' (may cross midnight)."""

    def __init__(self, text):
        self.text = text.strip()
        try:
            a, b = self.text.split("-")
            self.start, self.end = (self._mins(x) for x in (a, b))
        except ValueError:
            raise ValueError(f"invalid window {text!r}, expected HH:MM-HH:MM") from None

    @staticmethod
    def _mins(s):
        h, m = s.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 24 and 0 <= m < 60) or h * 60 + m > 1440:
            raise ValueError
        return h * 60 + m

    def contains(self, dt):
        x = dt.hour * 60 + dt.minute
        if self.start <= self.end:
            return self.start <= x < self.end
        return x >= self.start or x < self.end

    def __str__(self):
        return self.text


_DUR = re.compile(r"(\d+)\s*([smhdwM])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000}


def parse_duration(text):
    """'90' / '45s' / '24h' / '3d12h' / '2w' / '1M' (30 days) -> seconds."""
    t = str(text).strip()
    if t.isdigit():
        return int(t)
    if not t or _DUR.sub("", t).strip():
        raise ValueError(f"invalid duration {text!r} (use e.g. 90, 30m, 24h, 3d12h, 2w)")
    return sum(int(n) * _UNITS[u] for n, u in _DUR.findall(t))


def format_duration(seconds):
    seconds = int(max(0, seconds))
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(parts[:2]) or f"{seconds}s"


def parse_timestamp(value):
    """RFC 3339 timestamp (Docker/OCI 'created', possibly with nanoseconds) -> aware datetime."""
    if not value:
        return None
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$",
                 str(value).strip())
    if not m:
        return None
    frac = (m[2] or "")[:7]  # '.' + up to 6 digits
    tz = m[3] or "Z"
    tz = "+00:00" if tz == "Z" else (tz if ":" in tz else f"{tz[:3]}:{tz[3:]}")
    dt = datetime.fromisoformat(m[1] + frac + tz)
    return dt if dt.year > 1970 else None  # reproducible builds use the epoch: unknown
