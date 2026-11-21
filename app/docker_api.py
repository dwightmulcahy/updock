import base64
import json
import os
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


def registry_credentials(repo):
    """(user, password, serveraddress) from a mounted docker config.json, or None."""
    path = os.path.join(os.getenv("DOCKER_CONFIG", "/config"), "config.json")
    try:
        with open(path) as f:
            auths = json.load(f).get("auths", {})
    except (OSError, ValueError):
        return None
    reg = registry_of(repo)
    entry = auths.get(reg) or auths.get(reg.replace("https://", "").rstrip("/"))
    if not entry or "auth" not in entry:
        return None
    user, _, pw = base64.b64decode(entry["auth"]).decode().partition(":")
    return user, pw, reg


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

    def pull(self, ref):
        repo, tag = split_ref(ref)
        auth = registry_auth(repo)
        headers = {"X-Registry-Auth": auth} if auth else {}
        with self.c.stream("POST", "/images/create", params={"fromImage": repo, "tag": tag},
                           headers=headers) as r:
            if r.status_code >= 400:
                raise DockerError(r.status_code, r.read().decode().strip())
            for line in r.iter_lines():
                if line and "error" in (msg := json.loads(line)):
                    raise DockerError(500, msg["error"])

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
