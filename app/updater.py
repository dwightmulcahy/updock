import logging
import os
import threading
import time
from collections import defaultdict

from .docker_api import DockerError, split_ref
from .logs import LogBuffer
from .notify import EmailNotifier
from .registry import REVISION_LABELS, VERSION_LABELS, RegistryError

ENABLE_LABEL = "updock.enable"
SELF_LABEL = "updock.self"
IMAGE_DEFAULT_KEYS = ("Env", "WorkingDir", "User", "ExposedPorts", "Volumes",
                      "Healthcheck", "StopSignal", "OnBuild", "Shell")
EP_KEEP = ("IPAMConfig", "Links", "Aliases", "DriverOpts")

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
COMPOSE_DEPENDS = "com.docker.compose.depends_on"
COMPOSE_IMAGE = "com.docker.compose.image"

log = logging.getLogger("updock")


def compose_depends(labels):
    """Parse 'db:service_healthy:true,cache:service_started' -> {service: restart?}."""
    out = {}
    for item in (labels or {}).get(COMPOSE_DEPENDS, "").split(","):
        parts = item.strip().split(":")
        if parts[0]:
            out[parts[0]] = len(parts) > 2 and parts[2].lower() == "true"
    return out


def compose_order(containers):
    """Order container list entries so Compose dependencies come before dependents
    (stable otherwise; cycles and unknown services keep their original order)."""
    by_key = {}
    for c in containers:
        labels = c.get("Labels") or {}
        if labels.get(COMPOSE_PROJECT) and labels.get(COMPOSE_SERVICE):
            by_key.setdefault((labels[COMPOSE_PROJECT], labels[COMPOSE_SERVICE]), []).append(c)
    out, done, visiting = [], set(), set()

    def visit(c):
        cid = c["Id"]
        if cid in done or cid in visiting:
            return
        visiting.add(cid)
        labels = c.get("Labels") or {}
        for svc in compose_depends(labels):
            for dep in by_key.get((labels.get(COMPOSE_PROJECT), svc), []):
                visit(dep)
        visiting.discard(cid)
        done.add(cid)
        out.append(c)

    for c in containers:
        visit(c)
    return out


class UnhealthyError(Exception):
    """The replacement container failed its healthcheck or exited during startup."""


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
    def __init__(self, docker, cleanup=True, label_enable=False, registry=None, logbuf=None,
                 health_timeout=120, startup_grace=0, sleep=time.sleep, clock=time.monotonic,
                 digest_check=True, notifier=None, store=None, include_stopped=False):
        self.d = docker
        self.registry = registry
        self.cleanup = cleanup
        self.label_enable = label_enable
        self.state = defaultdict(dict)
        self.locks = defaultdict(threading.Lock)
        if logbuf is None:
            logbuf = LogBuffer()
            log.addHandler(logbuf)
        self.logbuf = logbuf
        self.self_id = os.getenv("HOSTNAME", "")
        self._img_cache = {}
        self.health_timeout = health_timeout
        self.startup_grace = startup_grace
        self.sleep, self.clock = sleep, clock
        self.digest_check = digest_check
        self.notifier = notifier or EmailNotifier()  # disabled unless configured
        self.store = store
        self.include_stopped = include_stopped
        if store:
            for name, st in store.load_state().items():
                self.state[name].update(st)
        self._unpulled = set()  # containers whose newer image was found but not pulled yet

    @property
    def logs(self):
        return self.logbuf.entries

    def _log(self, msg, level=logging.INFO, container=None, event=None, exc_info=None):
        log.log(level, msg, extra={"container": container, "event": event}, exc_info=exc_info)

    def _set(self, name, **kw):
        self.state[name].update(kw)
        if self.store:
            self.store.save_state({n: dict(st) for n, st in self.state.items()})

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
        for c in self.d.containers(all=True):
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
            self._set(name, job="pinned-digest", message="Image is pinned by digest/ID", last_check=time.time())
            return ctr, False
        if self.registry and self.digest_check:
            try:
                return ctr, self._check_remote(name, ctr, ref)
            except Exception as e:
                self._log(f"registry digest check failed ({e}); pulling instead",
                          logging.DEBUG, name, "check")
        avail = self._pull(name, ctr, ref)
        return ctr, avail

    def _pull(self, name, ctr, ref):
        self._set(name, job="pulling", message=f"Pulling {ref}")
        self._log(f"pulling {ref}", logging.DEBUG, name, "pull")
        t0 = time.monotonic()
        self._pull_image(name, ref)
        new = self.d.image(ref)
        self._log(f"pulled {ref} in {time.monotonic() - t0:.1f}s -> {new['Id'][7:19]}",
                  logging.DEBUG, name, "pull")
        self._img_cache[new["Id"]] = latest = image_info(new)
        self._unpulled.discard(name)
        avail = new["Id"] != ctr["Image"]
        self._set(name, job="update-available" if avail else "up-to-date", latest=latest,
                  update_available=avail, last_check=time.time(), message="")
        return avail

    def _pull_image(self, name, ref):
        """Pull with progress shown in the UI (state saved at most every 5% / second)."""
        seen = {"pct": -100, "t": 0.0}

        def progress(pct):
            now = time.monotonic()
            if pct == 100 or pct - seen["pct"] >= 5 or now - seen["t"] >= 1:
                seen.update(pct=pct, t=now)
                self._set(name, progress=pct, message=f"Pulling {ref} {pct}%")
        try:
            self.d.pull(ref, progress=progress)
        finally:
            self._set(name, progress=None)

    def _check_remote(self, name, ctr, ref):
        """Compare the registry's digest for the tag with the running image's RepoDigests.
        Costs one HEAD request (not a pull, so no Docker Hub rate-limit hit)."""
        self._set(name, job="checking", message=f"Checking {ref}")
        remote = self.registry.digest(ref)
        local = self.d.image(ctr["Image"])
        local_digests = {d.split("@", 1)[1] for d in local.get("RepoDigests") or []}
        if remote in local_digests:
            latest, avail = image_info(local), False
        else:
            platform = {"os": local.get("Os"), "architecture": local.get("Architecture"),
                        "variant": local.get("Variant")}
            try:
                latest = self.registry.remote_info(ref, platform)
            except Exception as e:
                self._log(f"could not read remote image details: {e}", logging.DEBUG, name,
                          "check")
                latest = {"id": None, "digest": remote, "tags": [], "version": None,
                          "revision": None, "created": None}
            avail = True
            self._unpulled.add(name)
        self._log(f"registry digest {remote[7:19]} {'differs' if avail else 'matches'}",
                  logging.DEBUG, name, "check")
        self._set(name, job="update-available" if avail else "up-to-date", latest=latest,
                  update_available=avail, last_check=time.time(), message="")
        return avail

    def _locked(self, name, fn, notify_errors=True):
        lock = self.locks[name]
        if not lock.acquire(blocking=False):
            self._log("job already running; request ignored", logging.WARNING, name, "busy")
            return
        try:
            fn()
        except UnhealthyError as e:
            self._set(name, job="rolled-back", message=str(e))
            self._log(f"rolled back: {e}", logging.ERROR, name, "rollback")
            self.notifier.notify("rollback", name, f"update rolled back: {e}")
        except Exception as e:
            self._set(name, job="error", message=str(e))
            if notify_errors:
                self.notifier.notify("error", name, f"{type(e).__name__}: {e}")
            expected = isinstance(e, (DockerError, RegistryError))
            self._log(f"{type(e).__name__}: {e}", logging.ERROR, name, "error",
                      exc_info=None if expected else e)
        finally:
            lock.release()

    def update(self, name, force=False, scheduled=False):
        def job():
            try:
                ctr, avail = self.check(name)
            except Exception as e:
                if not force:
                    raise
                self._log(f"pull failed ({e}); recreating from local image",
                          logging.WARNING, name, "pull")
                ctr, avail = self.d.inspect(name), False
            if avail and name in self._unpulled:
                avail = self._pull(name, ctr, ctr["Config"]["Image"])
            if not (avail or force):
                self._log("up to date", container=name, event="check")
                return
            target = (self.state[name].get("latest") or {}).get("id")
            failed = self.state[name].get("failed_image")
            if scheduled and avail and target and target == failed:
                self._log(f"skipping image {target}: it failed its health check before; "
                          "use Update now to retry", logging.WARNING, name, "update")
                self._set(name, job="rolled-back")
                return
            self._set(name, job="updating", message="Recreating")
            change = self._describe_change(ctr)  # before cleanup removes the old image
            try:
                self._recreate(ctr, cleanup=True)
                self._restart_compose_dependents(ctr)
            except UnhealthyError:
                if avail:
                    self._set(name, failed_image=target)
                raise
            self._set(name, job="updated", update_available=False, last_update=time.time(),
                      message="", failed_image=None)
            self._log("updated" if avail else "recreated", container=name,
                      event="update" if avail else "recreate")
            if avail:
                self.notifier.notify("update", name, change)
        self._locked(name, job)

    def check_only(self, name, scheduled=False):
        def job():
            ctr, avail = self.check(name)
            self._log("update available" if avail else "up to date", container=name, event="check")
            latest = (self.state[name].get("latest") or {})
            key = latest.get("id") or latest.get("digest")
            if scheduled and avail and key != self.state[name].get("notified_available"):
                self.notifier.notify("available", name, self._describe_change(ctr))
                self._set(name, notified_available=key)
        self._locked(name, job, notify_errors=False)

    def _describe_change(self, ctr):
        """'test/app:latest 1.0.0 (8b0806bb9026) -> 2.0.0 (4b92afd055fd)'"""
        name = ctr["Name"].lstrip("/")
        old = self._image_info(ctr["Image"]) or {}
        new = self.state[name].get("latest") or {}
        fmt = lambda i: " ".join(x for x in (i.get("version") or "",
                                              f"({i['id']})" if i.get("id") else "") if x) or "?"
        return f"{ctr['Config']['Image']} {fmt(old)} -> {fmt(new)}"

    def pin(self, name, pinned):
        """Pinned containers are checked but never updated by scheduled runs."""
        self.d.inspect(name)  # 404s for unknown containers
        self._set(name, pinned=bool(pinned))
        self._log("pinned: scheduled runs will only check it" if pinned else
                  "unpinned: scheduled runs will update it again", container=name, event="pin")

    def tag_info(self, name, tag):
        """Registry details (digest, version, created, ID) for one tag of the image."""
        if not self.registry:
            raise RuntimeError("registry lookups disabled")
        ctr = self.d.inspect(name)
        try:
            cur = self.d.image(ctr["Image"])
            platform = {"os": cur.get("Os"), "architecture": cur.get("Architecture"),
                        "variant": cur.get("Variant")}
        except Exception:
            platform = None
        ref = f"{base_repo(ctr['Config']['Image'])}:{tag}"
        return {"ref": ref, **self.registry.remote_info(ref, platform)}

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
            self._pull_image(name, new_ref)
            self._set(name, job="updating", message=f"Deploying {new_ref}")
            # Keep the previous image so switching back is instant.
            self._recreate(ctr, image_ref=new_ref, cleanup=False)
            self._restart_compose_dependents(ctr)
            self._set(name, job="deployed", ref=new_ref, update_available=False,
                      latest=None, last_update=time.time(), message="")
            self._log(f"deployed {new_ref}", container=name, event="deploy")
            self.notifier.notify("deploy", name, f"deployed {new_ref}")
        self._locked(name, job)

    def dependents(self, cid, name):
        """Containers sharing this container's network namespace (network_mode: container:X,
        or Compose `network_mode: service:X`). They must be recreated to follow a new ID."""
        modes = {f"container:{cid}", f"container:{name}"}
        out = []
        for c in self.d.containers(all=True):
            if c["Id"] != cid and (c.get("HostConfig") or {}).get("NetworkMode") in modes:
                out.append(self.d.inspect(c["Id"]))
        return out

    def _recreate(self, ctr, image_ref=None, cleanup=True, network_parent=None, start=None):
        name = ctr["Name"].lstrip("/")
        old_id, old_img = ctr["Id"], ctr["Image"]
        ref = image_ref or ctr["Config"]["Image"]
        try:
            img_cfg = self.d.image(old_img).get("Config")
        except Exception:
            img_cfg = None
        body, extra = build_create_body(ctr, img_cfg, image_ref)
        if (body.get("Labels") or {}).get(COMPOSE_IMAGE) and not network_parent:
            # Compose compares this with the image to decide whether to recreate on `up`
            try:
                body["Labels"][COMPOSE_IMAGE] = self.d.image(body["Image"])["Id"]
            except Exception as e:
                self._log(f"could not refresh {COMPOSE_IMAGE} label: {e}", logging.DEBUG, name,
                          "compose")
        if network_parent:
            body["HostConfig"]["NetworkMode"] = f"container:{network_parent}"
        was_running = ctr["State"]["Running"] if start is None else start
        timeout = ctr["Config"].get("StopTimeout") or 10

        deps = self.dependents(old_id, name)
        dep_running = {d["Id"]: d["State"]["Running"] for d in deps}
        for d in deps:
            dname = d["Name"].lstrip("/")
            self._log(f"uses {name}'s network; stopping until {name} is back", container=dname,
                      event="dependent")
            if d["State"]["Running"]:
                self.d.stop(d["Id"], d["Config"].get("StopTimeout") or 10)

        self._log(f"recreating from {ref} (old image {old_img[7:19]})", container=name,
                  event="recreate")
        if was_running:
            self._log(f"stopping (timeout {timeout}s)", logging.DEBUG, name, "recreate")
            self.d.stop(old_id, timeout)
        self.d.rename(old_id, f"{name}-updock-old")
        new_id = None
        try:
            new_id = self.d.create(name, body)
            self._log(f"created {new_id[:12]}", logging.DEBUG, name, "recreate")
            for net, cfg in extra:
                self.d.connect(net, new_id, cfg)
                self._log(f"connected to network {net}", logging.DEBUG, name, "recreate")
            if was_running:
                self.d.start(new_id)
                self._log("started", logging.DEBUG, name, "recreate")
                self._verify(new_id, name)
        except Exception as e:
            self._log(f"{e}; rolling back to previous container", logging.ERROR, name, "rollback")
            if new_id:
                self.d.remove(new_id)
            self.d.rename(old_id, name)
            if was_running:
                self.d.start(old_id)
            self._restart_dependents(deps, dep_running)
            self._log("previous container restored", logging.WARNING, name, "rollback")
            raise
        self.d.remove(old_id)
        for d in deps:
            self._recreate_dependent(d, new_id, dep_running[d["Id"]], name)
        if cleanup and self.cleanup and self.d.inspect(new_id)["Image"] != old_img:
            self.remove_old_image(old_img, ref)
        return new_id

    def _verify(self, cid, name):
        """Wait for the new container to prove itself; raise UnhealthyError to roll back.

        With a healthcheck: wait up to health_timeout for `healthy`. Without one: it must
        still be running (not crash-looping) after startup_grace seconds."""
        info = self.d.inspect(cid)
        test = (info["Config"].get("Healthcheck") or {}).get("Test") or []
        has_hc = bool(test) and test[0] != "NONE"
        limit = self.health_timeout if has_hc else self.startup_grace
        deadline = self.clock() + limit
        self._log("waiting for healthcheck" if has_hc else f"checking it stays up for {limit}s",
                  logging.DEBUG, name, "health")
        restarts = info.get("RestartCount", 0)
        while True:
            st = info["State"]
            if not st.get("Running") or st.get("Restarting"):
                raise UnhealthyError(f"new container exited (code {st.get('ExitCode')})")
            if info.get("RestartCount", 0) > restarts:
                raise UnhealthyError(f"new container crashed and was restarted "
                                     f"(exit code {st.get('ExitCode')})")
            health = st.get("Health") or {}
            if has_hc and health.get("Status") == "healthy":
                self._log("new container is healthy", container=name, event="health")
                return
            if has_hc and health.get("Status") == "unhealthy":
                out = ((health.get("Log") or [{}])[-1].get("Output") or "").strip()[:300]
                raise UnhealthyError("new container is unhealthy" + (f": {out}" if out else ""))
            if self.clock() >= deadline:
                if has_hc:
                    raise UnhealthyError(f"new container not healthy after {limit}s")
                return
            self.sleep(min(2, max(0.1, deadline - self.clock())))
            info = self.d.inspect(cid)

    def _restart_compose_dependents(self, ctr):
        """Mimic Compose `depends_on: {restart: true}`: restart services that asked to be
        restarted when this one is recreated."""
        labels = ctr["Config"].get("Labels") or {}
        project, service = labels.get(COMPOSE_PROJECT), labels.get(COMPOSE_SERVICE)
        if not (project and service):
            return
        for c in self.d.containers():
            cl = c.get("Labels") or {}
            if cl.get(COMPOSE_PROJECT) == project and compose_depends(cl).get(service):
                cname = c["Names"][0].lstrip("/")
                try:
                    timeout = self.d.inspect(c["Id"])["Config"].get("StopTimeout") or 10
                    self.d.restart(c["Id"], timeout)
                    self._log(f"restarted because {service} was recreated (depends_on restart)",
                              container=cname, event="compose")
                except Exception as e:
                    self._log(f"restart after {service} was recreated failed: {e}",
                              logging.ERROR, cname, "compose")

    def _restart_dependents(self, deps, dep_running):
        for d in deps:
            if dep_running[d["Id"]]:
                try:
                    self.d.start(d["Id"])
                except Exception as e:
                    self._log(f"could not restart: {e}", logging.ERROR, d["Name"].lstrip("/"),
                              "dependent")

    def _recreate_dependent(self, dep, parent_id, was_running, parent_name):
        dname = dep["Name"].lstrip("/")
        try:
            self._recreate(dep, cleanup=False, network_parent=parent_id, start=was_running)
            self._set(dname, job="recreated", message=f"followed {parent_name}",
                      last_update=time.time())
            self._log(f"recreated to join {parent_name}'s new network", container=dname,
                      event="dependent")
        except Exception as e:
            self._set(dname, job="error", message=f"failed to follow {parent_name}: {e}")
            self._log(f"failed to follow {parent_name}: {e}", logging.ERROR, dname, "dependent")
            self.notifier.notify("error", dname, f"failed to follow {parent_name}'s new network: {e}")

    def remove_old_image(self, old_img, ref):
        """Delete the superseded image: untag same-repo tags still pointing at it, then
        remove it. Skipped if another container uses it or a foreign repo tags it."""
        short = old_img.split(":")[-1][:12]
        try:
            users = [c["Names"][0].lstrip("/") for c in self.d.containers(all=True)
                     if c["ImageID"] == old_img]
            if users:
                self._log(f"kept old image {short}: used by {', '.join(users)}", event="cleanup")
                return False
            tags = self.d.image(old_img).get("RepoTags") or []
            repo = base_repo(ref)
            foreign = [t for t in tags if base_repo(t) != repo]
            if foreign:
                self._log(f"kept old image {short}: also tagged {', '.join(foreign)}", event="cleanup")
                return False
            for t in tags:
                self.d.remove_image(t)
            try:
                self.d.remove_image(old_img)
            except DockerError as e:
                if e.status != 404:  # 404: untagging the last tag already removed it
                    raise
            self._img_cache.pop(old_img, None)
            self._log(f"removed old image {short}", event="cleanup")
            return True
        except Exception as e:
            self._log(f"could not remove old image {short}: {e}", logging.WARNING, event="cleanup")
            return False

    def run_all(self, update=True):
        t0 = time.monotonic()
        self._log(f"run started ({'update' if update else 'check only'})", event="run")
        with self.notifier.batch():
            for c in compose_order(self.d.containers(all=self.include_stopped)):
                if self.eligible(c):
                    name = c["Names"][0].lstrip("/")
                    if update and self.state.get(name, {}).get("pinned"):
                        self._log("pinned: checking only", logging.DEBUG, name, "pin")
                        self.check_only(name, scheduled=True)
                    elif update:
                        self.update(name, scheduled=True)
                    else:
                        self.check_only(name, scheduled=True)
        self._log(f"run finished in {time.monotonic() - t0:.1f}s", event="run")
