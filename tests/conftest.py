import copy
import logging

import pytest

from app.logs import LogBuffer

CID = "abcdef1234567890" + "0" * 48
OLD_IMG = "sha256:old"
NEW_IMG = "sha256:new"


def make_ctr(name="web", image="test/app:latest", image_id=OLD_IMG, cid=CID, **over):
    ctr = {
        "Id": cid,
        "Name": f"/{name}",
        "Image": image_id,
        "State": {"Running": True},
        "Config": {
            "Image": image,
            "Hostname": cid[:12],
            "Env": ["USER_VAR=x", "V=1"],
            "Cmd": ["/run"],
            "Entrypoint": None,
            "WorkingDir": "",
            "Labels": {"foo": "bar", "img.label": "1"},
            "MacAddress": "aa:bb",
        },
        "HostConfig": {"NetworkMode": "bridge", "Binds": ["named:/data"], "Mounts": None},
        "Mounts": [
            {"Type": "volume", "Name": "named", "Destination": "/data"},
            {"Type": "volume", "Name": "anonvol", "Destination": "/anon"},
            {"Type": "bind", "Source": "/x", "Destination": "/x"},
        ],
        "NetworkSettings": {"Networks": {
            "bridge": {"NetworkID": "n1", "IPAddress": "1.2.3.4", "Aliases": None},
            "net2": {"NetworkID": "n2", "Aliases": ["webby", cid[:12]], "DNSNames": ["x"]},
        }},
    }
    for k, v in over.items():
        ctr[k] = v
    return ctr


IMG_CFG = {"Env": ["V=1"], "Cmd": ["/run"], "Entrypoint": None, "WorkingDir": "",
           "Labels": {"img.label": "1"}}


class FakeDocker:
    """In-memory stand-in for app.docker_api.Docker."""

    def __init__(self):
        self.ctrs = {}
        self.images = {OLD_IMG: {"Id": OLD_IMG, "Config": IMG_CFG}}
        self.tags = {}
        self.calls = []
        self.fail = {}
        self.next_id = 0

    def add(self, ctr, list_entry_labels=None):
        name = ctr["Name"].lstrip("/")
        self.ctrs[name] = ctr
        self.tags.setdefault(ctr["Config"]["Image"], ctr["Image"])
        return ctr

    def _maybe_fail(self, op):
        self.calls.append(op)
        if op in self.fail:
            raise self.fail[op]

    def _find(self, cid):
        for n, c in self.ctrs.items():
            if c["Id"] == cid or n == cid:
                return n, c
        raise KeyError(cid)

    def containers(self, all=False):
        self.calls.append(f"containers:{all}")
        return [{"Id": c["Id"], "Names": [f"/{n}"], "Image": c["Config"]["Image"],
                 "ImageID": c["Image"], "State": "running", "Status": "Up",
                 "Labels": c["Config"].get("Labels")} for n, c in self.ctrs.items()]

    def inspect(self, cid):
        self._maybe_fail("inspect")
        return copy.deepcopy(self._find(cid)[1])

    def image(self, ref):
        self._maybe_fail("image")
        iid = self.tags.get(ref, ref)
        return {**self.images[iid], "RepoTags": [t for t, i in self.tags.items() if i == iid]}

    def pull(self, ref):
        self._maybe_fail("pull")
        self.pulled = ref

    def stop(self, cid, t):
        self._maybe_fail("stop")
        self.stop_timeout = t

    def start(self, cid):
        self._maybe_fail("start")

    def rename(self, cid, name):
        self._maybe_fail(f"rename:{name}")
        old, c = self._find(cid)
        del self.ctrs[old]
        c["Name"] = f"/{name}"
        self.ctrs[name] = c

    def create(self, name, body):
        self._maybe_fail("create")
        self.next_id += 1
        cid = f"new{self.next_id}".ljust(64, "0")
        self.created_body = body
        self.ctrs[name] = {"Id": cid, "Name": f"/{name}", "Image": self.tags[body["Image"]],
                           "Config": {"Image": body["Image"], "Labels": body.get("Labels")}}
        return cid

    def connect(self, net, cid, cfg):
        self._maybe_fail("connect")
        self.connected = getattr(self, "connected", []) + [(net, cfg)]

    def remove(self, cid):
        self._maybe_fail("remove")
        n, _ = self._find(cid)
        del self.ctrs[n]

    def remove_image(self, ref):
        self._maybe_fail("remove_image")
        self.removed = getattr(self, "removed", []) + [ref]
        if ref in self.tags:  # untag; deleting the last tag deletes the image
            iid = self.tags.pop(ref)
            if iid not in self.tags.values():
                self.images.pop(iid, None)
            return
        if ref not in self.images:
            from app.docker_api import DockerError
            raise DockerError(404, "no such image")
        self.images.pop(ref)


@pytest.fixture
def fake():
    return FakeDocker()


@pytest.fixture(autouse=True)
def _updock_logger():
    lg = logging.getLogger("updock")
    old = lg.level
    lg.setLevel(logging.DEBUG)
    yield
    lg.setLevel(old)
    for h in list(lg.handlers):
        if isinstance(h, LogBuffer):
            lg.removeHandler(h)
