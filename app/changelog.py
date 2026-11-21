"""Release-notes links built from an image's OCI source label (or a per-container template)."""
import re
from urllib.parse import quote, urlsplit

SOURCE_LABELS = ("org.opencontainers.image.source", "org.label-schema.vcs-url",
                 "org.opencontainers.image.url", "org.label-schema.url")
TEMPLATE_LABEL = "updock.changelog-url"


def normalize_source(url):
    """'git@github.com:o/r.git' / 'https://github.com/o/r.git/' -> 'https://github.com/o/r'.
    Returns None for anything that isn't a plain http(s) URL."""
    if not url:
        return None
    url = url.strip()
    m = re.match(r"^git@([^:]+):(.+)$", url)
    if m:
        url = f"https://{m[1]}/{m[2]}"
    url = re.sub(r"^git\+", "", url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = re.sub(r"(\.git)?/*$", "", parts.path)
    return f"{parts.scheme}://{parts.netloc}{path}"


def source_of(labels):
    for k in SOURCE_LABELS:
        src = normalize_source((labels or {}).get(k))
        if src:
            return src
    return None


def changelog_url(source, version, template=None):
    """Best link to the release notes for `version`.

    With a template (label updock.changelog-url), `{version}` and `{source}` are filled in.
    GitHub: the releases page searched for the version (works for 'v1.2' and '1.2' tags).
    GitLab: the releases page. Other hosts: the source URL."""
    if template:
        if "{version}" in template and not version:
            return None
        url = template.replace("{version}", quote(version or "", safe="")) \
                      .replace("{source}", source or "")
        return url if normalize_source(url) else None
    if not source:
        return None
    host = urlsplit(source).netloc.lower()
    if host == "github.com" and source.count("/") >= 4:
        return f"{source}/releases" + (f"?q={quote(version, safe='')}&expanded=true"
                                       if version else "")
    if host == "gitlab.com" or host.startswith("gitlab."):
        return f"{source}/-/releases"
    return source
