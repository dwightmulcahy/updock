"""Minimal OCI distribution (registry v2) client: list tags with token auth."""
import re

import httpx

from .docker_api import registry_credentials, split_ref

HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io"}
MAX_PAGES = 5
MAX_TAGS = 300


class RegistryError(Exception):
    pass


def parse_repo(ref):
    """'nginx:1' -> ('registry-1.docker.io', 'library/nginx')."""
    repo = split_ref(ref)[0].split("@", 1)[0]
    first, _, rest = repo.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, name = first, rest
    else:
        host, name = "docker.io", repo
    if host in HUB_ALIASES:
        host = "registry-1.docker.io"
        if "/" not in name:
            name = f"library/{name}"
    return host, name


def _challenge(header):
    scheme, _, params = header.partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', params))


_VER = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$")


def tag_sort_key(tag):
    """Version-like tags first, newest first; plain releases before suffixed ones."""
    m = _VER.match(tag)
    if not m:
        return (1, (), 1, tag)
    nums = tuple(-int(x) for x in m[1].split("."))
    return (0, nums, 0 if not m[2] else 1, m[2])


def sort_tags(tags):
    return sorted(set(tags), key=tag_sort_key)[:MAX_TAGS]


class Registry:
    def __init__(self, client=None):
        self.c = client or httpx.Client(timeout=20, follow_redirects=True)

    def _token(self, header, name, repo_ref):
        scheme, p = _challenge(header)
        creds = registry_credentials(split_ref(repo_ref)[0])
        basic = (creds[0], creds[1]) if creds else None
        if scheme == "basic":
            return {"auth": basic} if basic else None
        if scheme != "bearer" or "realm" not in p:
            raise RegistryError(f"unsupported auth challenge: {header}")
        params = {"scope": f"repository:{name}:pull"}
        if "service" in p:
            params["service"] = p["service"]
        r = self.c.get(p["realm"], params=params, auth=basic)
        if r.status_code >= 400:
            raise RegistryError(f"token request failed: {r.status_code}")
        data = r.json()
        tok = data.get("token") or data.get("access_token")
        return {"headers": {"Authorization": f"Bearer {tok}"}}

    def tags(self, ref):
        host, name = parse_repo(ref)
        url = f"https://{host}/v2/{name}/tags/list"
        params, auth, out = {"n": 1000}, {}, []
        for _ in range(MAX_PAGES):
            r = self.c.get(url, params=params, **auth)
            if r.status_code == 401 and not auth:
                auth = self._token(r.headers.get("www-authenticate", ""), name, ref) or {}
                r = self.c.get(url, params=params, **auth)
            if r.status_code >= 400:
                raise RegistryError(f"{host}: {r.status_code} listing tags for {name}")
            out += r.json().get("tags") or []
            nxt = re.search(r"<([^>]+)>;\s*rel=\"?next", r.headers.get("link", ""))
            if not nxt:
                break
            link = nxt[1]
            url = link if link.startswith("http") else f"https://{host}{link}"
            params = None
        return sort_tags(out)
