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
