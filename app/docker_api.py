import base64
import json
import logging
import os
import shutil
import subprocess
from urllib.parse import quote

import httpx


class DockerError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"{status}: {msg}")
        self.status = status


def split_ref(ref):
    """Return (repo, tag); tag None if pinned by digest/ID."""
    if "@" in ref or ref.startswith("sha256:"):
        return ref, None
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, "latest"


def registry_of(repo):
    first = repo.split("/", 1)[0]
    if "/" in repo and ("." in first or ":" in first or first == "localhost"):
        return first
    return "https://index.docker.io/v1/"


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


def registry_credentials(repo):
    """(user, password, serveraddress) from a mounted docker config.json, or None.
    Supports inline `auths` entries and `credHelpers` / `credsStore` helpers."""
    path = os.path.join(os.getenv("DOCKER_CONFIG", "/config"), "config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    reg = registry_of(repo)
    keys = (reg, reg.replace("https://", "").rstrip("/"))
    auths = cfg.get("auths", {})
    entry = auths.get(keys[0]) or auths.get(keys[1])
    if entry and "auth" in entry:
        user, _, pw = base64.b64decode(entry["auth"]).decode().partition(":")
        return user, pw, reg
    helpers = cfg.get("credHelpers", {})
    helper = helpers.get(keys[0]) or helpers.get(keys[1]) or cfg.get("credsStore")
    if helper:
        creds = _cred_helper(helper, reg)
        if creds:
            return creds[0], creds[1], reg
    return None


def registry_auth(repo):
    """Build X-Registry-Auth header value for the Engine API."""
    creds = registry_credentials(repo)
    if not creds:
        return None
    payload = {"username": creds[0], "password": creds[1], "serveraddress": creds[2]}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


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
            raise DockerError(r.status_code, r.text.strip())
        if r.content and r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return None

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
                raise DockerError(r.status_code, r.read().decode().strip())
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
            raise DockerError(r.status_code, r.text)

    def start(self, cid):
        r = self.c.post(f"/containers/{cid}/start")
        if r.status_code not in (204, 304):
            raise DockerError(r.status_code, r.text)

    def rename(self, cid, name):
        self._req("POST", f"/containers/{cid}/rename", params={"name": name})

    def create(self, name, body):
        return self._req("POST", "/containers/create", params={"name": name}, json=body)["Id"]

    def connect(self, net, cid, cfg):
        self._req("POST", f"/networks/{quote(net)}/connect",
                  json={"Container": cid, "EndpointConfig": cfg})

    def remove(self, cid):
        self._req("DELETE", f"/containers/{cid}", params={"force": 1})

    def restart(self, cid, t=10):
        r = self.c.post(f"/containers/{cid}/restart", params={"t": t})
        if r.status_code != 204:
            raise DockerError(r.status_code, r.text)

    def remove_image(self, ref):
        """Delete an image by ID, or untag it when given a tag."""
        self._req("DELETE", f"/images/{quote(ref, safe='')}")
