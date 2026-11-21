import pytest

from app.changelog import changelog_url, normalize_source, source_of


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r", "https://github.com/o/r"),
    ("https://github.com/o/r.git", "https://github.com/o/r"),
    ("https://github.com/o/r/", "https://github.com/o/r"),
    ("git@github.com:o/r.git", "https://github.com/o/r"),
    ("git+https://gitlab.com/g/p.git", "https://gitlab.com/g/p"),
    ("  http://gitea.local:3000/o/r  ", "http://gitea.local:3000/o/r"),
    ("javascript:alert(1)", None), ("ftp://x/y", None), ("https://", None),
    ("", None), (None, None),
])
def test_normalize_source(url, expected):
    assert normalize_source(url) == expected


def test_source_of_label_priority():
    assert source_of({"org.label-schema.vcs-url": "https://gitlab.com/a/b",
                      "org.opencontainers.image.url": "https://example.com"}) == "https://gitlab.com/a/b"
    assert source_of({"org.opencontainers.image.source": "not a url",
                      "org.opencontainers.image.url": "https://example.com/app"}) == "https://example.com/app"
    assert source_of({}) is None and source_of(None) is None


@pytest.mark.parametrize("source,version,template,expected", [
    ("https://github.com/o/r", "1.2.3", None, "https://github.com/o/r/releases?q=1.2.3&expanded=true"),
    ("https://github.com/o/r", "v2 beta", None, "https://github.com/o/r/releases?q=v2%20beta&expanded=true"),
    ("https://github.com/o/r", None, None, "https://github.com/o/r/releases"),
    ("https://github.com/o", "1", None, "https://github.com/o"),  # not a repo URL
    ("https://gitlab.com/g/p", "1", None, "https://gitlab.com/g/p/-/releases"),
    ("https://gitlab.example.org/g/p", None, None, "https://gitlab.example.org/g/p/-/releases"),
    ("https://codeberg.org/o/r", "1", None, "https://codeberg.org/o/r"),
    (None, "1", None, None),
    (None, "1.0", "https://ex.com/changes/{version}", "https://ex.com/changes/1.0"),
    ("https://github.com/o/r", "1.0", "{source}/blob/main/CHANGELOG.md",
     "https://github.com/o/r/blob/main/CHANGELOG.md"),
    (None, None, "https://ex.com/changes/{version}", None),   # needs a version
    (None, "1", "javascript:alert({version})", None),          # unsafe template
])
def test_changelog_url(source, version, template, expected):
    assert changelog_url(source, version, template) == expected
