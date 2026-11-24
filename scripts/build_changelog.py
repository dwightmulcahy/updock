#!/usr/bin/env python3
"""Write app/changelog.json: the release notes of every stable version up to --upto, built
from Conventional Commit subjects between tags. The release workflow runs this before the
image build so the UI can show "What's new" after an upgrade, without network access."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_release_notes import (CONVENTIONAL_COMMIT, SECTIONS, commit_subjects,  # noqa: E402
                                    git, previous_tag, version_tuple)

# What a user notices; the rest is counted as "internal changes"
USER_FACING = ("feat", "fix", "perf", "security", "revert")


def entry(tag: str) -> dict:
    prev = previous_tag(tag)
    groups: dict[str, list[dict]] = {}
    breaking, internal = [], 0
    for sha, subject, *rest in commit_subjects(tag, prev):
        body = rest[0] if rest else ""
        m = CONVENTIONAL_COMMIT.match(subject)
        kind = m.group("type") if m else "other"
        text = m.group("text") if m else subject
        if m and m.group("scope"):
            text = f"{m.group('scope')}: {text}"
        item = {"text": text, "sha": sha[:7]}
        if (m and m.group("breaking")) or "BREAKING CHANGE:" in body or "BREAKING-CHANGE:" in body:
            breaking.append(item)
        elif kind in USER_FACING:
            groups.setdefault(kind, []).append(item)
        else:
            internal += 1
    sections = [{"type": "breaking", "title": "Breaking changes", "items": breaking}] if breaking else []
    sections += [{"type": k, "title": SECTIONS[k][1], "items": groups[k]}
                 for k in SECTIONS if k in groups]
    return {"version": tag.removeprefix("v"), "tag": tag, "previous": prev,
            "date": git("log", "-1", "--format=%cs", tag), "sections": sections,
            "internal": internal}


def build(upto: str) -> list[dict]:
    top, stable = version_tuple(upto), True
    if top is None:  # a pre-release (v1.2.0-rc1): only the stable releases before it
        top, stable = version_tuple(upto.split("-", 1)[0]), False
        if top is None:
            raise ValueError(f"Not a SemVer tag: {upto}")
    keep = (lambda v: v <= top) if stable else (lambda v: v < top)
    tags = [t for t in git("tag", "--merged", upto).splitlines()
            if version_tuple(t) is not None and keep(version_tuple(t))]
    return [entry(t) for t in sorted(tags, key=version_tuple, reverse=True)]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--upto", required=True, help="release tag being built, e.g. v0.8.0")
    p.add_argument("--repository", default="", help="owner/name, for links")
    p.add_argument("--output", type=Path, default=Path("app/changelog.json"))
    a = p.parse_args()
    a.output.write_text(json.dumps({"repository": a.repository, "releases": build(a.upto)},
                                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
