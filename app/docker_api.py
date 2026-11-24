import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
from urllib.parse import quote

import httpx


class DockerError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"{status}: {msg}")
        self.status = status


# Docker socket proxies (Tecnativa / LinuxServer docker-socket-proxy) gate API sections by env var
PROXY_SECTIONS = [(r"^/containers/[^/]+/exec", ("CONTAINERS", "EXEC")), (r"^/exec/", ("EXEC",)),
                  (r"^/containers", ("CONTAINERS",)), (r"^/images", ("IMAGES",)),
                  (r"^/networks", ("NETWORKS",)), (r"^/system", ("SYSTEM",)), (r"^/_ping", ("PING",))]


def proxy_hint(method, path):
    path = re.sub(r"^/v[\d.]+", "", path)
    sections = next((s for rx, s in PROXY_SECTIONS if re.match(rx, path)), ())
    needs = [f"{s}=1" for s in sections] + (["POST=1"] if method != "GET" else [])
    return "blocked by the Docker socket proxy" + (f"; enable {', '.join(needs)}" if needs else "")


def http_error(r, text):
    """DockerError for a failed Engine API response, explaining socket-proxy denials (the
    proxy answers 403 with an HTML page; the Engine itself answers with JSON)."""
    text = (text or "").strip()
    if r.status_code == 403 and not text.startswith("{"):
        text = f"{proxy_hint(r.request.method, r.request.url.path)} " \
               f"({r.request.method} {r.request.url.path})"
    return DockerError(r.status_code, text)


def split_ref(ref):
    """Return (repo, tag); tag None if pinned by digest/ID."""
    if "@" in ref or ref.startswith("sha256:"):
        return ref, None
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, "latest"


HUB_KEY = "https://index.docker.io/v1/"
HUB_HOSTS = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com",
             "hub.docker.com"}
# Extra credential sources (e.g. logins saved in the UI): callables(registry_key) -> (user, pw)
CREDENTIAL_PROVIDERS = []


def normalize_registry(host):
    """'ghcr.io', 'https://ghcr.io/', 'docker.io' ... -> the key Docker uses for logins."""
    h = (host or "").strip().lower()
    h = h.split("://", 1)[-1].split("/", 1)[0]
    return HUB_KEY if h in HUB_HOSTS or not h else h


def registry_of(repo):
    first = repo.split("/", 1)[0]
    if "/" in repo and ("." in first or ":" in first or first == "localhost"):
        return normalize_registry(first)
    return HUB_KEY


def _cred_helper(helper, server):
    """Ask a docker-credential-<helper> binary (must be on PATH, e.g. mounted) for creds."""
    exe = shutil.which(f"docker-credential-{helper}")
    if not exe:
        logging.getLogger("updock").warning(
            "credential helper docker-credential-%s not found on PATH", helper,
            extra={"event": "auth"})
        return None
    try:
        out = subprocess.run([exe, "get"], input=server, capture_output=True, text=True,
                             timeout=10, check=True).stdout
        data = json.loads(out)
        return data["Username"], data["Secret"]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
        logging.getLogger("updock").warning("credential helper %s failed for %s: %s", helper,
                                            server, e, extra={"event": "auth"})
        return None


def _config():
    path = os.path.join(os.getenv("DOCKER_CONFIG", "/config"), "config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _config_entry(cfg, reg):
    keys = (reg, reg.replace("https://", "").rstrip("/"))
    auths = cfg.get("auths") or {}
    return auths.get(keys[0]) or auths.get(keys[1]) or {}, keys


def _ui_login(reg):
    for provider in CREDENTIAL_PROVIDERS:
        creds = provider(reg)
        if creds:
            return creds
    return None


def registry_identity_token(repo):
    """OAuth refresh token (`identitytoken`) that `docker login` stores for some registries,
    e.g. Azure ACR via `az acr login`. A login saved in the UI takes precedence."""
    reg = registry_of(repo)
    if _ui_login(reg):
        return None
    entry, _ = _config_entry(_config(), reg)
    return (entry.get("identitytoken") or "").strip() or None


def registry_credentials(repo):
    """(user, password, serveraddress) from logins saved in the UI, then a mounted docker
    config.json (inline `auths` entries, `credHelpers` / `credsStore` helpers), or None.
    An entry with an `identitytoken` has no usable password: see registry_identity_token."""
    reg = registry_of(repo)
    creds = _ui_login(reg)
    if creds:
        return creds[0], creds[1], reg
    cfg = _config()
    if not cfg:
        return None
    entry, keys = _config_entry(cfg, reg)
    if entry.get("identitytoken"):
        return None
    if "auth" in entry:
        user, _, pw = base64.b64decode(entry["auth"]).decode().partition(":")
        return user, pw, reg
    if entry.get("username"):
        return entry["username"], entry.get("password", ""), reg
    helpers = cfg.get("credHelpers", {})
    helper = helpers.get(keys[0]) or helpers.get(keys[1]) or cfg.get("credsStore")
    if helper:
        creds = _cred_helper(helper, reg)
        if creds:
            return creds[0], creds[1], reg
    return None


def registry_auth(repo):
    """Build X-Registry-Auth header value for the Engine API."""
    token = registry_identity_token(repo)
    if token:
        payload = {"identitytoken": token, "serveraddress": registry_of(repo)}
    else:
        creds = registry_credentials(repo)
        if not creds:
            return None
        payload = {"username": creds[0], "password": creds[1], "serveraddress": creds[2]}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def demux(raw):
    """Decode Docker's multiplexed stdout/stderr stream (8-byte frame headers)."""
    out, i = [], 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1:i + 4] == b"\0\0\0":
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    if i < len(raw):  # not multiplexed (TTY) or truncated: keep the rest as-is
        out.append(raw[i:])
    return b"".join(out).decode(errors="replace")


class Docker:
    def __init__(self):
        host = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")
        if host.startswith("unix://"):
            self.c = httpx.Client(transport=httpx.HTTPTransport(uds=host[7:]),
                                  base_url="http://docker", timeout=None)
        else:
            self.c = httpx.Client(base_url=host.replace("tcp://", "http://"), timeout=None)

    def _req(self, method, path, **kw):
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise http_error(r, r.text)
        if r.content and r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return None

    def version(self):
        """Engine version info (GET /version)."""
        return self._req("GET", "/version")

    def ping(self):
        r = self.c.get("/_ping", timeout=5)
        return r.status_code == 200

    def containers(self, all=False):
        return self._req("GET", "/containers/json", params={"all": int(all)})

    def inspect(self, cid):
        return self._req("GET", f"/containers/{quote(cid)}/json")

    def image(self, ref):
        return self._req("GET", f"/images/{quote(ref, safe='')}/json")

    def pull(self, ref, progress=None):
        """Pull an image. `progress(percent)` is called as layers download (0-100)."""
        repo, tag = split_ref(ref)
        auth = registry_auth(repo)
        headers = {"X-Registry-Auth": auth} if auth else {}
        layers = {}  # id -> [current, total]
        last = -1
        with self.c.stream("POST", "/images/create", params={"fromImage": repo, "tag": tag},
                           headers=headers) as r:
            if r.status_code >= 400:
                raise http_error(r, r.read().decode(errors="replace"))
            for line in r.iter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if "error" in msg:
                    raise DockerError(500, msg["error"])
                if progress is None or "id" not in msg:
                    continue
                status, detail = msg.get("status", ""), msg.get("progressDetail") or {}
                if status == "Downloading" and detail.get("total"):
                    layers[msg["id"]] = [detail.get("current", 0), detail["total"]]
                elif status in ("Download complete", "Pull complete", "Already exists") \
                        and msg["id"] in layers:
                    layers[msg["id"]][0] = layers[msg["id"]][1]
                else:
                    continue
                total = sum(t for _, t in layers.values())
                pct = int(100 * sum(c for c, _ in layers.values()) / total) if total else 0
                if pct != last:
                    last = pct
                    progress(pct)

    def stop(self, cid, t):
        r = self.c.post(f"/containers/{cid}/stop", params={"t": t})
        if r.status_code not in (204, 304):
            raise http_error(r, r.text)

    def start(self, cid):
        r = self.c.post(f"/containers/{cid}/start")
        if r.status_code not in (204, 304):
            raise http_error(r, r.text)

    def rename(self, cid, name):
        self._req("POST", f"/containers/{cid}/rename", params={"name": name})

    def create(self, name, body):
        return self._req("POST", "/containers/create", params={"name": name}, json=body)["Id"]

    def connect(self, net, cid, cfg):
        self._req("POST", f"/networks/{quote(net)}/connect",
                  json={"Container": cid, "EndpointConfig": cfg})

    def remove(self, cid, volumes=False):
        """Force-remove a container; `volumes` also removes its anonymous volumes."""
        self._req("DELETE", f"/containers/{cid}", params={"force": 1, "v": int(volumes)})

    def exec_run(self, cid, cmd, timeout=60, max_output=65536):
        """Run `cmd` in a running container; return (exit_code, combined output).
        Raises TimeoutError if it runs longer than `timeout` seconds (0 = no limit)."""
        eid = self._req("POST", f"/containers/{cid}/exec",
                        json={"Cmd": cmd, "AttachStdout": True, "AttachStderr": True})["Id"]
        raw, start = b"", time.monotonic()
        try:
            with self.c.stream("POST", f"/exec/{eid}/start", json={"Detach": False, "Tty": False},
                               timeout=httpx.Timeout(10, read=timeout or None)) as r:
                if r.status_code >= 400:
                    raise http_error(r, r.read().decode(errors="replace"))
                for chunk in r.iter_bytes():
                    if len(raw) < max_output:
                        raw += chunk
                    if timeout and time.monotonic() - start > timeout:
                        raise TimeoutError(f"still running after {timeout}s")
        except httpx.ReadTimeout:
            raise TimeoutError(f"still running after {timeout}s") from None
        code = self._req("GET", f"/exec/{eid}/json").get("ExitCode")
        return code, demux(raw[:max_output])

    def disk_usage(self):
        """Docker image storage: {'used': bytes, 'reclaimable': bytes, 'images': n}."""
        df = self._req("GET", "/system/df", params={"type": "image"})
        images = df.get("Images") or []
        used = df.get("LayersSize") or sum(i.get("Size", 0) for i in images)
        reclaimable = sum(max(i.get("Size", 0) - max(i.get("SharedSize", 0), 0), 0)
                          for i in images if i.get("Containers", 0) <= 0)
        return {"used": used, "reclaimable": reclaimable, "images": len(images)}

    def get_archive(self, cid, path, max_bytes):
        """Tar of `path` in the container (works when stopped). Raises DockerError(413) if the
        archive would exceed `max_bytes`, DockerError(404) if the path doesn't exist."""
        data = b""
        with self.c.stream("GET", f"/containers/{cid}/archive", params={"path": path}) as r:
            if r.status_code >= 400:
                raise http_error(r, r.read().decode(errors="replace"))
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise DockerError(413, f"{path} is larger than {max_bytes} bytes")
        return data

    def put_archive(self, cid, path, data):
        """Extract a tar into the container at `path` (missing parent dirs are created)."""
        r = self.c.put(f"/containers/{cid}/archive", params={"path": path}, content=data,
                       headers={"Content-Type": "application/x-tar"})
        if r.status_code != 200:
            raise http_error(r, r.text)

    def restart(self, cid, t=10):
        r = self.c.post(f"/containers/{cid}/restart", params={"t": t})
        if r.status_code != 204:
            raise http_error(r, r.text)

    def tag_image(self, src, ref):
        """Point tag `ref` (repo:tag) at image `src`."""
        repo, tag = split_ref(ref)
        self._req("POST", f"/images/{quote(src, safe='')}/tag", params={"repo": repo, "tag": tag})

    def remove_image(self, ref):
        """Delete an image by ID, or untag it when given a tag."""
        self._req("DELETE", f"/images/{quote(ref, safe='')}")
