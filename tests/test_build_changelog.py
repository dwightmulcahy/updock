import json
from unittest.mock import patch

import pytest

from scripts import build_changelog as bc

COMMITS = {
    "v1.1.0": [("abc12345", "feat(ui): add tabs (#12)", ""), ("def67890", "fix: crash on start"),
               ("0001111", "ci: faster builds", ""), ("0002222", "feat!: new config format", ""),
               ("0003333", "fix: old flag", "BREAKING CHANGE: removed --x"), ("0004444", "Merge stuff", "")],
    "v1.0.0": [("1112222", "docs: readme", "")],
}


def fake_git(*args):
    if args[:2] == ("tag", "--merged"):
        return "v1.0.0\nv1.1.0\nv1.2.0-rc1\nnot-a-version\nv0.9.0"
    if args[:2] == ("log", "-1"):
        return {"v1.1.0": "2026-11-20", "v1.0.0": "2026-11-01", "v0.9.0": "2026-10-01"}[args[-1]]
    raise AssertionError(args)


@pytest.fixture
def repo():
    prev = {"v1.1.0": "v1.0.0", "v1.0.0": "v0.9.0", "v0.9.0": None}
    with patch.object(bc, "git", fake_git), \
            patch.object(bc, "previous_tag", lambda t: prev[t]), \
            patch.object(bc, "commit_subjects", lambda t, p: COMMITS.get(t, [])):
        yield


def test_build_groups_user_facing_changes(repo):
    releases = bc.build("v1.1.0")
    assert [r["version"] for r in releases] == ["1.1.0", "1.0.0", "0.9.0"]
    r = releases[0]
    assert r["date"] == "2026-11-20" and r["previous"] == "v1.0.0"
    sections = {s["type"]: [i["text"] for i in s["items"]] for s in r["sections"]}
    assert sections == {"breaking": ["new config format", "old flag"],
                        "feat": ["ui: add tabs (#12)"], "fix": ["crash on start"]}
    assert [s["type"] for s in r["sections"]][0] == "breaking"
    assert r["internal"] == 2  # ci + a non-conventional subject
    assert releases[1]["sections"] == [] and releases[1]["internal"] == 1


def test_prerelease_only_lists_earlier_stable_releases(repo):
    assert [r["version"] for r in bc.build("v1.1.0-rc1")] == ["1.0.0", "0.9.0"]
    with pytest.raises(ValueError):
        bc.build("latest")


def test_main_writes_json(repo, tmp_path, monkeypatch):
    out = tmp_path / "changelog.json"
    monkeypatch.setattr("sys.argv", ["x", "--upto", "v1.0.0", "--repository", "me/app",
                                     "--output", str(out)])
    bc.main()
    data = json.loads(out.read_text())
    assert data["repository"] == "me/app" and [r["tag"] for r in data["releases"]] == ["v1.0.0", "v0.9.0"]
