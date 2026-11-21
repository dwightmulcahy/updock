import logging
import os
import threading
import time
from collections import defaultdict, deque

from .docker_api import DockerError, split_ref

ENABLE_LABEL = "updock.enable"
SELF_LABEL = "updock.self"
IMAGE_DEFAULT_KEYS = ("Env", "WorkingDir", "User", "ExposedPorts", "Volumes",
                      "Healthcheck", "StopSignal", "OnBuild", "Shell")
EP_KEEP = ("IPAMConfig", "Links", "Aliases", "DriverOpts")
VERSION_LABELS = ("org.opencontainers.image.version", "org.label-schema.version",
                  "build_version", "version")
REVISION_LABELS = ("org.opencontainers.image.revision", "org.label-schema.vcs-ref")

log = logging.getLogger("updock")


def build_create_body(ctr, old_img_cfg, image_ref=None):
    """Turn `docker inspect` output into a /containers/create body, letting
    values that came from the old image fall back to the new image's defaults."""
    cid12 = ctr["Id"][:12]
    cfg = dict(ctr["Config"])
    img = old_img_cfg or {}
    if image_ref:
        cfg["Image"] = image_ref

    for k in IMAGE_DEFAULT_KEYS:
        if k in cfg and cfg[k] == img.get(k):
            cfg.pop(k)
    if cfg.get("Env") and img.get("Env"):
        cfg["Env"] = [e for e in cfg["Env"] if e not in img["Env"]]
    # Cmd resets when Entrypoint is overridden, so only drop it alongside Entrypoint.
    if cfg.get("Entrypoint") == img.get("Entrypoint"):
        cfg.pop("Entrypoint", None)
        if cfg.get("Cmd") == img.get("Cmd"):
            cfg.pop("Cmd", None)
    if cfg.get("Labels") and img.get("Labels"):
        cfg["Labels"] = {k: v for k, v in cfg["Labels"].items() if img["Labels"].get(k) != v}
    if cfg.get("Hostname") == cid12:
        cfg.pop("Hostname")
    cfg.pop("MacAddress", None)

    hc = dict(ctr["HostConfig"])
    if hc.get("Links"):  # inspect: "/db:/web/alias" -> create: "db:alias"
        hc["Links"] = [f"{s.lstrip('/')}:{d.rsplit('/', 1)[-1]}"
                       for s, d in (l.split(":", 1) for l in hc["Links"])]

    # Re-attach anonymous volumes so their data survives.
    targets = {b.split(":")[1] for b in hc.get("Binds") or [] if ":" in b}
    targets |= {m["Target"] for m in hc.get("Mounts") or []}
    anon = [{"Type": "volume", "Source": m["Name"], "Target": m["Destination"]}
            for m in ctr.get("Mounts") or []
            if m.get("Type") == "volume" and m["Destination"] not in targets]
    if anon:
        hc["Mounts"] = (hc.get("Mounts") or []) + anon

    mode = hc.get("NetworkMode", "default")
    extra = []
    body = {**cfg, "HostConfig": hc}
    if mode.startswith("container:"):
        for k in ("Hostname", "Domainname", "ExposedPorts"):
            cfg.pop(k, None)
            body.pop(k, None)
    elif mode not in ("host", "none"):
        nets = {}
        for name, ep in (ctr["NetworkSettings"].get("Networks") or {}).items():
            clean = {k: ep[k] for k in EP_KEEP if ep.get(k)}
            if clean.get("Aliases"):
                clean["Aliases"] = [a for a in clean["Aliases"] if a != cid12]
            nets[name] = clean
        primary = mode if mode in nets else ("bridge" if mode == "default" and "bridge" in nets
                                             else next(iter(nets), None))
        if primary:
            body["NetworkingConfig"] = {"EndpointsConfig": {primary: nets.pop(primary)}}
        extra = list(nets.items())
    return body, extra


def image_info(img):
    """Summarise an image inspect result for display."""
    labels = (img.get("Config") or {}).get("Labels") or {}
    digests = img.get("RepoDigests") or []
    return {
        "id": img["Id"].split(":")[-1][:12],
        "digest": digests[0].split("@", 1)[1] if digests else None,
        "tags": img.get("RepoTags") or [],
        "version": next((labels[k] for k in VERSION_LABELS if labels.get(k)), None),
        "revision": next((labels[k][:12] for k in REVISION_LABELS if labels.get(k)), None),
        "created": img.get("Created"),
    }


def base_repo(ref):
    return split_ref(ref)[0].split("@", 1)[0]


class Updater:
    def __init__(self, docker, cleanup=True, label_enable=False, registry=None):
        self.d = docker
        self.registry = registry
        self.cleanup = cleanup
        self.label_enable = label_enable
        self.state = defaultdict(dict)
        self.locks = defaultdict(threading.Lock)
        self.logs = deque(maxlen=500)
        self.self_id = os.getenv("HOSTNAME", "")
        self._img_cache = {}

    def _log(self, msg, level=logging.INFO):
        log.log(level, msg)
        self.logs.append({"t": time.time(), "level": logging.getLevelName(level), "msg": msg})

    def _set(self, name, **kw):
        self.state[name].update(kw)

    def _image_info(self, iid):
        if iid not in self._img_cache:
            try:
                self._img_cache[iid] = image_info(self.d.image(iid))
            except Exception:
                return None
        return self._img_cache[iid]

    def eligible(self, c):
        labels = c.get("Labels") or {}
        if labels.get(SELF_LABEL) == "true" or (self.self_id and c["Id"].startswith(self.self_id)):
            return False
        v = labels.get(ENABLE_LABEL)
        return v == "true" if self.label_enable else v != "false"

    def list(self):
        out = []
        for c in self.d.containers():
            name = c["Names"][0].lstrip("/")
            st = dict(self.state.get(name, {}))
            ref = st.pop("ref", None) or c["Image"]
            out.append({"name": name, "image": ref, "image_id": c["ImageID"][7:19],
                        "state": c["State"], "status": c["Status"],
                        "eligible": self.eligible(c), "current": self._image_info(c["ImageID"]),
                        **st})
        return sorted(out, key=lambda x: x["name"])

    def check(self, name):
        ctr = self.d.inspect(name)
        ref = ctr["Config"]["Image"]
        self._set(name, ref=ref)
        if split_ref(ref)[1] is None:
            self._set(name, job="pinned", message="Pinned by digest/ID", last_check=time.time())
            return ctr, False
        self._set(name, job="pulling", message=f"Pulling {ref}")
        self.d.pull(ref)
        new = self.d.image(ref)
        self._img_cache[new["Id"]] = latest = image_info(new)
        avail = new["Id"] != ctr["Image"]
        self._set(name, job="update-available" if avail else "up-to-date", latest=latest,
                  update_available=avail, last_check=time.time(), message="")
        return ctr, avail

    def _locked(self, name, fn):
        lock = self.locks[name]
        if not lock.acquire(blocking=False):
            self._log(f"{name}: job already running", logging.WARNING)
            return
        try:
            fn()
        except Exception as e:
            self._set(name, job="error", message=str(e))
            self._log(f"{name}: {e}", logging.ERROR)
        finally:
            lock.release()

    def update(self, name, force=False):
        def job():
            try:
                ctr, avail = self.check(name)
            except Exception as e:
                if not force:
                    raise
                self._log(f"{name}: pull failed ({e}); recreating from local image",
                          logging.WARNING)
                ctr, avail = self.d.inspect(name), False
            if not (avail or force):
                self._log(f"{name}: up to date")
                return
            self._set(name, job="updating", message="Recreating")
            self._recreate(ctr, cleanup=True)
            self._set(name, job="updated", update_available=False, last_update=time.time(),
                      message="")
            self._log(f"{name}: {'updated' if avail else 'recreated'}")
        self._locked(name, job)

    def check_only(self, name):
        def job():
            _, avail = self.check(name)
            self._log(f"{name}: {'update available' if avail else 'up to date'}")
        self._locked(name, job)

    def tags(self, name):
        if not self.registry:
            raise RuntimeError("registry lookups disabled")
        ref = self.d.inspect(name)["Config"]["Image"]
        return {"image": base_repo(ref), "current": split_ref(ref)[1],
                "tags": self.registry.tags(ref)}

    def deploy(self, name, tag):
        """Recreate the container from repo:<tag> (e.g. roll back to an older version)."""
        def job():
            ctr = self.d.inspect(name)
            new_ref = f"{base_repo(ctr['Config']['Image'])}:{tag}"
            self._set(name, job="pulling", message=f"Pulling {new_ref}")
            self.d.pull(new_ref)
            self._set(name, job="updating", message=f"Deploying {new_ref}")
            # Keep the previous image so switching back is instant.
            self._recreate(ctr, image_ref=new_ref, cleanup=False)
            self._set(name, job="deployed", ref=new_ref, update_available=False,
                      latest=None, last_update=time.time(), message="")
            self._log(f"{name}: deployed {new_ref}")
        self._locked(name, job)

    def _recreate(self, ctr, image_ref=None, cleanup=True):
        name = ctr["Name"].lstrip("/")
        old_id, old_img = ctr["Id"], ctr["Image"]
        ref = image_ref or ctr["Config"]["Image"]
        try:
            img_cfg = self.d.image(old_img).get("Config")
        except Exception:
            img_cfg = None
        body, extra = build_create_body(ctr, img_cfg, image_ref)
        was_running = ctr["State"]["Running"]
        timeout = ctr["Config"].get("StopTimeout") or 10

        if was_running:
            self.d.stop(old_id, timeout)
        self.d.rename(old_id, f"{name}-updock-old")
        new_id = None
        try:
            new_id = self.d.create(name, body)
            for net, cfg in extra:
                self.d.connect(net, new_id, cfg)
            if was_running:
                self.d.start(new_id)
        except Exception:
            self._log(f"{name}: recreate failed, rolling back", logging.ERROR)
            if new_id:
                self.d.remove(new_id)
            self.d.rename(old_id, name)
            if was_running:
                self.d.start(old_id)
            raise
        self.d.remove(old_id)
        if cleanup and self.cleanup and self.d.inspect(new_id)["Image"] != old_img:
            self.remove_old_image(old_img, ref)

    def remove_old_image(self, old_img, ref):
        """Delete the superseded image: untag same-repo tags still pointing at it, then
        remove it. Skipped if another container uses it or a foreign repo tags it."""
        short = old_img.split(":")[-1][:12]
        try:
            users = [c["Names"][0].lstrip("/") for c in self.d.containers(all=True)
                     if c["ImageID"] == old_img]
            if users:
                self._log(f"kept old image {short}: used by {', '.join(users)}")
                return False
            tags = self.d.image(old_img).get("RepoTags") or []
            repo = base_repo(ref)
            foreign = [t for t in tags if base_repo(t) != repo]
            if foreign:
                self._log(f"kept old image {short}: also tagged {', '.join(foreign)}")
                return False
            for t in tags:
                self.d.remove_image(t)
            try:
                self.d.remove_image(old_img)
            except DockerError as e:
                if e.status != 404:  # 404: untagging the last tag already removed it
                    raise
            self._img_cache.pop(old_img, None)
            self._log(f"removed old image {short}")
            return True
        except Exception as e:
            self._log(f"could not remove old image {short}: {e}", logging.WARNING)
            return False

    def run_all(self, update=True):
        self._log("Starting run")
        for c in self.d.containers():
            if self.eligible(c):
                name = c["Names"][0].lstrip("/")
                (self.update if update else self.check_only)(name)
        self._log("Run finished")
