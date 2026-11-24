"""Pick the newest tag within a semver range (label updock.semver=patch|minor|major)."""
import re

SEMVER_LABEL = "updock.semver"
LEVELS = ("patch", "minor", "major")
_TAG = re.compile(r"^(v?)(\d+(?:\.\d+){0,2})((?:-[0-9A-Za-z][0-9A-Za-z.-]*)?)$")


def parse(tag):
    """'v1.2.3-alpine' -> ('v', (1, 2, 3), '-alpine'); None if not version-like."""
    m = _TAG.match(tag or "")
    if not m:
        return None
    return m[1], tuple(int(x) for x in m[2].split(".")), m[3]


def best(current, tags, level):
    """Newest tag newer than `current` that keeps its prefix, precision and suffix
    ('1.2.3-alpine' only moves to 'x.y.z-alpine') and stays within `level`:
    patch keeps major.minor, minor keeps major, major allows anything."""
    cur = parse(current)
    if not cur or level not in LEVELS:
        return None
    prefix, nums, suffix = cur
    fixed = {"patch": 2, "minor": 1, "major": 0}[level]
    found = None
    for tag in tags:
        p = parse(tag)
        if not p or p[0] != prefix or p[2] != suffix or len(p[1]) != len(nums):
            continue
        if p[1][:fixed] != nums[:fixed] or p[1] <= nums:
            continue
        if found is None or p[1] > found[1]:
            found = (tag, p[1])
    return found[0] if found else None


def jump(old, new):
    """How far old -> new moves: 'patch', 'minor' or 'major' (the first part that grows);
    None when either isn't version-like or new isn't newer. Prefix and suffix are ignored."""
    a, b = parse(str(old or "").strip()), parse(str(new or "").strip())
    if not a or not b:
        return None
    x, y = (a[1] + (0, 0))[:3], (b[1] + (0, 0))[:3]
    if y <= x:
        return None
    return LEVELS[2 - next(i for i in range(3) if x[i] != y[i])]


def exceeds(level, allow):
    """True when a `level` move is beyond what `allow` permits."""
    return LEVELS.index(level) > LEVELS.index(allow)
