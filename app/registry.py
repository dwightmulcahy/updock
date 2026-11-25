"""Minimal OCI distribution (registry v2) client: tags, manifest digests, image config."""
import hashlib
import logging
import re
import ssl
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .changelog import source_of
from .docker_api import registry_credentials, registry_identity_token, split_ref
from .schedule import format_duration

log = logging.getLogger("updock")
HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io"}
HUB_REGISTRY = "registry-1.docker.io"
HUB_API = "https://hub.docker.com/v2/repositories"
MAX_PAGES = 5
MAX_TAGS = 300
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json")
ACCEPT = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
VERSION_LABELS = ("org.opencontainers.image.version", "org.label-schema.version",
                  "build_version", "version")
REVISION_LABELS = ("org.opencontainers.image.revision", "org.label-schema.vcs-ref")


class RegistryError(Exception):
    pass


class RegistryAuthError(RegistryError):
    """The registry wants credentials Updock doesn't have (private image, or wrong login)."""

    def __init__(self, host, name, status=401):
        self.host, self.name, self.status = host, name, status
        super().__init__(auth_help(host, name))


def auth_help(host, name):
    """Actionable explanation for a 401/403 from `host` for repository `name`."""
    hint = {
        "ghcr.io": "a GitHub token with the read:packages scope",
        "registry-1.docker.io": "your Docker Hub username and an access token",
        "docker.io": "your Docker Hub username and an access token",
    }.get(host, "a username and password/token for it")
    shown = "docker.io" if host in HUB_ALIASES else host
    return (f"{shown} refused access to {name} (private image or missing/expired login). "
            f"Add a login for {shown} with {hint} under Registries in the Updock header.")


class RateLimited(RegistryError):
    """The registry answered 429 (or Updock is backing off from an earlier 429)."""

    def __init__(self, host, retry_after=None, waiting=False):
        self.host, self.retry_after, self.waiting = host, retry_after, waiting
        when = f"; next try in {format_duration(retry_after)}" if retry_after else ""
        what = "backing off from" if waiting else "rate-limited by"
        super().__init__(f"{what} {host}{when}")


def retry_after_seconds(value, now=None):
    """Parse a Retry-After header (seconds or HTTP date) into seconds, or None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = now if now is not None else time.time()
    return max(0, int(when.timestamp() - now))


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


def tls_context(ca_file=None):
    """Default trust store, plus the PEM bundle `ca_file` (private CA or self-signed cert)."""
    ctx = ssl.create_default_context()
    if ca_file:
        ctx.load_verify_locations(cafile=ca_file)
    return ctx


def normalize_hosts(hosts):
    """'registry.lan:5000, http://10.0.0.5:5000/' -> {'registry.lan:5000', '10.0.0.5:5000'}"""
    out = set()
    for h in hosts or ():
        h = h.strip().lower().split("://", 1)[-1].split("/", 1)[0]
        if h:
            out.add(HUB_REGISTRY if h in HUB_ALIASES else h)
    return out


class Registry:
    def __init__(self, client=None, insecure=(), ca_file=None):
        self.c = client or httpx.Client(timeout=20, follow_redirects=True,
                                        verify=tls_context(ca_file))
        self.insecure = normalize_hosts(insecure)  # plain-HTTP registries
        self._auth = {}  # (host, name) -> request kwargs, refreshed on 401

    def base(self, host):
        return f"{'http' if host in self.insecure else 'https'}://{host}"

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
        refresh = registry_identity_token(split_ref(repo_ref)[0])
        if refresh:  # `docker login` OAuth refresh token (e.g. az acr login)
            r = self.c.post(p["realm"], data={**params, "grant_type": "refresh_token",
                                              "refresh_token": refresh, "client_id": "docker"})
        else:
            r = self.c.get(p["realm"], params=params, auth=basic)
        if r.status_code == 429:
            raise RateLimited(parse_repo(repo_ref)[0],
                              retry_after_seconds(r.headers.get("retry-after")))
        if r.status_code in (401, 403):
            raise RegistryAuthError(parse_repo(repo_ref)[0], name, r.status_code)
        if r.status_code >= 400:
            raise RegistryError(f"token request failed: {r.status_code}")
        data = r.json()
        tok = data.get("token") or data.get("access_token")
        return {"headers": {"Authorization": f"Bearer {tok}"}}

    def _do(self, method, url, auth, headers=None, **kw):
        h = {**(headers or {}), **auth.get("headers", {})}
        return self.c.request(method, url, headers=h, auth=auth.get("auth"),
                              follow_redirects=False, **kw)

    def _send(self, method, url, host, name, ref, _hops=0, **kw):
        auth = self._auth.get((host, name), {})
        r = self._do(method, url, auth, **kw)
        if r.status_code == 401:
            auth = self._token(r.headers.get("www-authenticate", ""), name, ref) or {}
            self._auth[(host, name)] = auth
            r = self._do(method, url, auth, **kw)
        if r.is_redirect:
            if _hops >= 5:
                raise RegistryError(f"{host}: too many redirects for {name}")
            return self._redirect(r, method, url, host, name, ref, _hops, **kw)
        if r.status_code == 429:
            raise RateLimited(host, retry_after_seconds(r.headers.get("retry-after")))
        return r

    def _redirect(self, r, method, url, host, name, ref, hops, **kw):
        """Follow a redirect without leaking or losing credentials.

        Same host: repeat with the same auth. Another registry (e.g. lscr.io sending blobs
        to ghcr.io): authenticate there through its own challenge, since a plain redirect
        drops the Authorization header and the target answers 401. Anything else (a CDN
        or signed storage URL): fetch it without credentials."""
        target = str(r.url.join(r.headers["location"]))
        new = urlsplit(target)
        m = re.match(r"^/v2/(.+)/(manifests|blobs|tags)/", new.path)
        if new.netloc == host or m:
            new_host = new.netloc
            new_name = m[1] if m else name
            return self._send(method, target, new_host, new_name, ref, _hops=hops + 1, **kw)
        kw.pop("params", None)
        return self.c.request(method, target, headers={k: v for k, v in (kw.get("headers") or {}).items()
                                                        if k.lower() != "authorization"},
                              follow_redirects=True)

    def forget(self, key):
        """Drop cached tokens for a registry (after its login changed)."""
        host = "registry-1.docker.io" if key.startswith("https://index.docker.io") else key
        self._auth = {k: v for k, v in self._auth.items() if k[0] != host}

    def verify_login(self, key, username, password):
        """Check credentials against the registry's auth endpoint before saving them.
        Raises RegistryAuthError if they're rejected."""
        host = "registry-1.docker.io" if key.startswith("https://index.docker.io") else key
        r = self.c.get(f"{self.base(host)}/v2/", follow_redirects=False)
        if r.status_code == 200:
            return  # registry without auth: nothing to verify
        if r.status_code != 401:
            raise RegistryError(f"{host}: unexpected status {r.status_code} from /v2/")
        scheme, p = _challenge(r.headers.get("www-authenticate", ""))
        if scheme == "bearer" and "realm" in p:
            params = {"service": p["service"]} if "service" in p else {}
            r = self.c.get(p["realm"], params=params, auth=(username, password))
        else:
            r = self.c.get(f"{self.base(host)}/v2/", auth=(username, password),
                           follow_redirects=False)
        if r.status_code in (401, 403):
            raise RegistryAuthError(host, "the login check", r.status_code)
        if r.status_code >= 400:
            raise RegistryError(f"{host}: login check failed with status {r.status_code}")

    def tags(self, ref):
        """Tags of the image's repository, version-like ones newest first. Docker Hub is
        asked for its most recently pushed tags first (its registry API lists tags
        alphabetically, so a repo with thousands of tags could hide the newest ones);
        if that fails (private repo, Hub API down), the registry API is used."""
        host, name = parse_repo(ref)
        if host == HUB_REGISTRY:
            try:
                return sort_tags(self._hub_tags(name))
            except (httpx.HTTPError, RegistryError, ValueError, KeyError, TypeError) as e:
                log.debug("Docker Hub tag API for %s failed (%s); using the registry", name, e,
                          extra={"event": "check"})
        url = f"{self.base(host)}/v2/{name}/tags/list"
        params, out = {"n": 1000}, []
        for _ in range(MAX_PAGES):
            r = self._send("GET", url, host, name, ref, params=params)
            if r.status_code in (401, 403):
                raise RegistryAuthError(host, name, r.status_code)
            if r.status_code >= 400:
                raise RegistryError(f"{host}: {r.status_code} listing tags for {name}")
            out += r.json().get("tags") or []
            nxt = re.search(r"<([^>]+)>;\s*rel=\"?next", r.headers.get("link", ""))
            if not nxt:
                break
            link = nxt[1]
            url = link if link.startswith("http") else f"{self.base(host)}{link}"
            params = None
        return sort_tags(out)

    def _hub_tags(self, name):
        """Docker Hub tags ordered by last push, newest first (public repos only)."""
        url, params, out = f"{HUB_API}/{name}/tags", {"page_size": 100,
                                                      "ordering": "last_updated"}, []
        for _ in range(MAX_PAGES):
            r = self.c.get(url, params=params, follow_redirects=True)
            if r.status_code >= 400:
                raise RegistryError(f"hub.docker.com: {r.status_code} listing tags for {name}")
            data = r.json()
            out += [t["name"] for t in data.get("results") or [] if t.get("name")]
            url, params = data.get("next"), None
            if not url or not url.startswith(HUB_API):
                break
        if not out:
            raise RegistryError(f"hub.docker.com: no tags listed for {name}")
        return out

    def _manifest(self, ref, reference, method="GET"):
        host, name = parse_repo(ref)
        r = self._send(method, f"{self.base(host)}/v2/{name}/manifests/{reference}", host, name,
                       ref, headers={"Accept": ACCEPT})
        if r.status_code in (401, 403):
            raise RegistryAuthError(host, name, r.status_code)
        if r.status_code >= 400:
            raise RegistryError(f"{host}: {r.status_code} for manifest {name}:{reference}")
        return r

    def digest(self, ref):
        """Digest the tag currently points to, via HEAD (doesn't count as a pull)."""
        tag = split_ref(ref)[1]
        r = self._manifest(ref, tag, "HEAD")
        dg = r.headers.get("docker-content-digest")
        if not dg:  # some registries omit it on HEAD
            r = self._manifest(ref, tag)
            dg = r.headers.get("docker-content-digest") or \
                "sha256:" + hashlib.sha256(r.content).hexdigest()
        return dg

    def remote_info(self, ref, platform=None):
        """Version/revision/created/ID of the image a tag points to, without pulling.
        `platform` = {"os", "architecture", "variant"} selects from a multi-arch index."""
        host, name = parse_repo(ref)
        r = self._manifest(ref, split_ref(ref)[1])
        digest = r.headers.get("docker-content-digest")
        man = r.json()
        if man.get("mediaType") in INDEX_TYPES or "manifests" in man:
            want = platform or {"os": "linux", "architecture": "amd64"}
            entries = [m for m in man["manifests"]
                       if (m.get("platform") or {}).get("os") == want.get("os")
                       and (m.get("platform") or {}).get("architecture") == want.get("architecture")]
            if want.get("variant"):
                entries = [m for m in entries
                           if (m.get("platform") or {}).get("variant") == want["variant"]] or entries
            if not entries:
                raise RegistryError(f"no {want.get('os')}/{want.get('architecture')} image in {ref}")
            man = self._manifest(ref, entries[0]["digest"]).json()
        cfg_digest = man["config"]["digest"]
        b = self._send("GET", f"{self.base(host)}/v2/{name}/blobs/{cfg_digest}", host, name, ref)
        if b.status_code in (401, 403):
            raise RegistryAuthError(host, name, b.status_code)
        if b.status_code >= 400:
            raise RegistryError(f"{host}: {b.status_code} fetching image config for {name}")
        cfg = b.json()
        labels = (cfg.get("config") or {}).get("Labels") or {}
        return {
            "id": cfg_digest.split(":")[-1][:12],
            "digest": digest,
            "tags": [],
            "version": next((labels[k] for k in VERSION_LABELS if labels.get(k)), None),
            "revision": next((labels[k][:12] for k in REVISION_LABELS if labels.get(k)), None),
            "created": cfg.get("created"),
            "source": source_of(labels),
        }
