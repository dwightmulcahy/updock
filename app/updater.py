import io
import json
import logging
import os
import tarfile
import threading
import time
from collections import Counter, defaultdict

from .changelog import TEMPLATE_LABEL, changelog_url, source_of
from .docker_api import DockerError, split_ref
from .logs import LogBuffer
from .notify import EmailNotifier
from .registry import REVISION_LABELS, VERSION_LABELS, RateLimited, RegistryError, parse_repo
from .schedule import format_duration, parse_duration, parse_timestamp
from .units import format_size

ENABLE_LABEL = "updock.enable"
COOLDOWN_LABEL = "updock.cooldown"
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


RATE_BASE = 60          # first backoff after a 429, doubled per consecutive 429
RATE_MAX = 6 * 3600     # longest backoff
COPY_LABEL = "updock.copy-file"
COPY_MAX_FILE = 1024 * 1024        # per file
COPY_MAX_TOTAL = 16 * 1024 * 1024  # all files of one container, held in memory
HOOK_PREFIX = "updock.hook."
HOOK_SKIP = 75  # EX_TEMPFAIL from a pre-update hook: skip this update, try again next run


class CopyFileError(Exception):
    """A updock.copy-file entry can't be carried over safely: the update is aborted."""


class HookError(Exception):
    """A pre-update hook failed: the update is aborted."""


class HookSkip(Exception):
    """A pre-update hook asked to skip this update (exit code 75)."""


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
        "source": source_of(labels),
    }


def base_repo(ref):
    return split_ref(ref)[0].split("@", 1)[0]


class Updater:
    def __init__(self, docker, cleanup=True, label_enable=False, registry=None, logbuf=None,
                 health_timeout=120, startup_grace=0, sleep=time.sleep, clock=time.monotonic,
                 digest_check=True, notifier=None, store=None, include_stopped=False,
                 cooldown=0, hooks=False, disk_max=0, disk_warn=0):
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
        self.cooldown = cooldown
        self.hooks = hooks
        self.disk_max, self.disk_warn = disk_max, disk_warn
        self._disk, self._disk_alerted = None, "ok"
        self._backoff = {}  # registry host -> {"until": ts, "hits": n}
        self.on_change = None  # callback(name, state) for live UI updates
        self.counters = Counter()  # Prometheus counters: outcomes and runs since start
        self.last_run = {}
        self._bad_labels = set()
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
        if self.on_change:
            try:
                self.on_change(name, dict(self.state[name]))
            except Exception:  # live updates must never break a job
                pass
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
            row = {"name": name, "image": ref, "image_id": c["ImageID"][7:19],
                   "state": c["State"], "status": c["Status"],
                   "eligible": self.eligible(c), "current": self._image_info(c["ImageID"]), **st}
            template = (c.get("Labels") or {}).get(TEMPLATE_LABEL)
            for key in ("current", "latest"):
                if isinstance(row.get(key), dict):
                    row[key] = {**row[key], "changelog": self.changelog(row[key], template)}
            out.append(row)
        return sorted(out, key=lambda x: x["name"])

    def check(self, name):
        ctr = self.d.inspect(name)
        ref = ctr["Config"]["Image"]
        self._set(name, ref=ref)
        if split_ref(ref)[1] is None:
            self._set(name, job="pinned-digest", message="Image is pinned by digest/ID", last_check=time.time())
            return ctr, False
        host = parse_repo(ref)[0]
        wait = self._backoff_left(host)
        if wait:
            raise RateLimited(host, wait, waiting=True)  # don't touch the registry at all
        if self.registry and self.digest_check:
            try:
                avail = self._check_remote(name, ctr, ref)
                self._backoff.pop(host, None)
                return ctr, avail
            except RateLimited as e:  # no pull fallback: that would spend pull quota too
                raise self._rate_limited(e)
            except Exception as e:
                self._log(f"registry digest check failed ({e}); pulling instead",
                          logging.DEBUG, name, "check")
        avail = self._pull_guarded(name, ctr, ref)
        return ctr, avail

    def _backoff_left(self, host):
        b = self._backoff.get(host)
        left = (b["until"] - time.time()) if b and "until" in b else 0
        return int(left) + 1 if left > 0 else 0

    def _rate_limited(self, e):
        """Record a 429 for e.host and return an exception describing the backoff."""
        b = self._backoff.setdefault(e.host, {"hits": 0})
        b["hits"] += 1
        delay = max(e.retry_after or 0, min(RATE_MAX, RATE_BASE * 2 ** (b["hits"] - 1)))
        b["until"] = time.time() + delay
        self._log(f"{e.host} answered 429 Too Many Requests; pausing requests to it for "
                  f"{format_duration(delay)}", logging.WARNING, event="rate-limit")
        return RateLimited(e.host, delay)

    def _pull_guarded(self, name, ctr, ref):
        """_pull, turning Docker's 'toomanyrequests' pull errors into a registry backoff."""
        host = parse_repo(ref)[0]
        try:
            avail = self._pull(name, ctr, ref)
        except DockerError as e:
            text = str(e).lower()
            if "toomanyrequests" in text or "429 too many requests" in text:
                raise self._rate_limited(RateLimited(host)) from None
            raise
        self._backoff.pop(host, None)
        return avail

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
        except RateLimited as e:
            self.counters["result_rate_limited"] += 1
            self._set(name, job="rate-limited", message=str(e))
            self._log(str(e), logging.INFO if e.waiting else logging.WARNING, name, "rate-limit")
        except HookSkip as e:
            self.counters["result_skipped"] += 1
            self._set(name, job="skipped", message=str(e))
            self._log(str(e), container=name, event="hook")
        except UnhealthyError as e:
            self.counters["result_rolled_back"] += 1
            self._set(name, job="rolled-back", message=str(e))
            self._log(f"rolled back: {e}", logging.ERROR, name, "rollback")
            self.notifier.notify("rollback", name, f"update rolled back: {e}")
        except Exception as e:
            self.counters["result_failed" if notify_errors else "result_check_failed"] += 1
            self._set(name, job="error", message=str(e))
            if notify_errors:
                self.notifier.notify("error", name, f"{type(e).__name__}: {e}")
            expected = isinstance(e, (DockerError, RegistryError, CopyFileError))
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
            if avail and scheduled:
                blocked = self._cooldown_block(name, ctr)
                if blocked:  # before pulling: a too-new image is never even downloaded
                    self.counters["result_cooldown"] += 1
                    self._set(name, job="cooldown", message=blocked)
                    self._log(blocked, container=name, event="cooldown")
                    return
            if avail and name in self._unpulled:
                avail = self._pull_guarded(name, ctr, ctr["Config"]["Image"])
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
            self.counters["result_updated" if avail else "result_recreated"] += 1
            self._set(name, job="updated", update_available=False, last_update=time.time(),
                      message="", failed_image=None)
            self._log("updated" if avail else "recreated", container=name,
                      event="update" if avail else "recreate")
            if avail:
                self.notifier.notify("update", name, change)
        self._locked(name, self._with_check_hooks(name, job))

    def _with_check_hooks(self, name, job):
        """Run pre-check / post-check hooks around a check or update job."""
        def wrapped():
            ctr = self.d.inspect(name) if self.hooks else None
            if ctr:
                self.run_hook(ctr, "pre-check")
            try:
                job()
            finally:
                if ctr:
                    try:
                        self.run_hook(self.d.inspect(name), "post-check")
                    except Exception as e:
                        self._log(f"post-check hook skipped: {e}", logging.DEBUG, name, "hook")
        return wrapped

    def run_hook(self, ctr, hook):
        """Run a lifecycle hook from the container's labels. Returns its exit code, or None
        if hooks are off, no hook is set, or the container isn't running."""
        if not self.hooks:
            return None
        name = ctr["Name"].lstrip("/")
        labels = ctr["Config"].get("Labels") or {}
        cmd = labels.get(HOOK_PREFIX + hook)
        if not cmd:
            return None
        if not ctr["State"].get("Running"):
            self._log(f"{hook} hook not run: container is not running", logging.DEBUG, name,
                      "hook")
            return None
        raw_t = labels.get(f"{HOOK_PREFIX}{hook}-timeout", "60")
        try:
            timeout = parse_duration(raw_t)
        except ValueError:
            self._log(f"invalid {HOOK_PREFIX}{hook}-timeout={raw_t!r}; using 60s",
                      logging.WARNING, name, "hook")
            timeout = 60
        argv = ["sh", "-c", cmd]
        if cmd.lstrip().startswith("["):  # exec form, for images without a shell
            try:
                argv = [str(x) for x in json.loads(cmd)]
            except ValueError:
                self._log(f"{hook} hook is not a valid JSON array; running it with sh",
                          logging.WARNING, name, "hook")
        self._log(f"running {hook} hook: {cmd}", logging.DEBUG, name, "hook")
        try:
            code, out = self.d.exec_run(ctr["Id"], argv, timeout)
        except Exception as e:
            self._log(f"{hook} hook failed: {e}", logging.ERROR, name, "hook")
            return -1
        out = out.strip()
        tail = f": {out[-500:]}" if out else ""
        level = logging.INFO if code == 0 else logging.WARNING
        self._log(f"{hook} hook exited {code}{tail}", level, name, "hook")
        return code

    def cooldown_for(self, name, ctr):
        """Seconds a new image must age before scheduled runs apply it (label overrides)."""
        raw = (ctr["Config"].get("Labels") or {}).get(COOLDOWN_LABEL)
        if raw is None:
            return self.cooldown
        try:
            return parse_duration(raw)
        except ValueError:
            if name not in self._bad_labels:
                self._bad_labels.add(name)
                self._log(f"invalid {COOLDOWN_LABEL}={raw!r}; using the global cooldown",
                          logging.WARNING, name, "cooldown")
            return self.cooldown

    def _cooldown_block(self, name, ctr):
        """Why the new image may not be applied yet, or None if it may."""
        delay = self.cooldown_for(name, ctr)
        if not delay:
            return None
        latest = self.state[name].get("latest") or {}
        label = latest.get("version") or latest.get("id") or "new image"
        created = parse_timestamp(latest.get("created"))
        if created is None:
            return f"cooldown: {label} has no build date; not applying it automatically"
        age = time.time() - created.timestamp()
        if age < 0:
            return f"cooldown: {label} has a build date in the future; not applying it"
        if age < delay:
            return (f"cooldown: {label} is {format_duration(age)} old; "
                    f"applying after {format_duration(delay)} (in {format_duration(delay - age)})")
        return None

    def check_only(self, name, scheduled=False):
        def job():
            ctr, avail = self.check(name)
            self._log("update available" if avail else "up to date", container=name, event="check")
            latest = (self.state[name].get("latest") or {})
            key = latest.get("id") or latest.get("digest")
            if scheduled and avail and key != self.state[name].get("notified_available"):
                self.notifier.notify("available", name, self._describe_change(ctr))
                self._set(name, notified_available=key)
        self._locked(name, self._with_check_hooks(name, job), notify_errors=False)

    def _describe_change(self, ctr):
        """'test/app:latest 1.0.0 (8b0806bb9026) -> 2.0.0 (4b92afd055fd)'"""
        name = ctr["Name"].lstrip("/")
        old = self._image_info(ctr["Image"]) or {}
        new = self.state[name].get("latest") or {}
        fmt = lambda i: " ".join(x for x in (i.get("version") or "",
                                              f"({i['id']})" if i.get("id") else "") if x) or "?"
        text = f"{ctr['Config']['Image']} {fmt(old)} -> {fmt(new)}"
        notes = self.changelog(new, (ctr["Config"].get("Labels") or {}).get(TEMPLATE_LABEL))
        return text + (f"\nRelease notes: {notes}" if notes else "")

    @staticmethod
    def changelog(info, template=None):
        return changelog_url(info.get("source"), info.get("version"), template)

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
        info = {"ref": ref, **self.registry.remote_info(ref, platform)}
        info["changelog"] = self.changelog(info, (ctr["Config"].get("Labels") or {}).get(TEMPLATE_LABEL))
        return info

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

        if not network_parent:
            code = self.run_hook(ctr, "pre-update")
            if code == HOOK_SKIP:
                raise HookSkip("pre-update hook asked to skip this update (exit 75)")
            if code not in (None, 0):
                raise HookError(f"pre-update hook exited {code}; update aborted")

        carried = self._collect_copy_files(ctr)  # before stopping: current contents

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
            if carried:
                self._write_copy_files(new_id, name, carried)
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
        if not network_parent and was_running:
            self.run_hook(self.d.inspect(new_id), "post-update")
        for d in deps:
            self._recreate_dependent(d, new_id, dep_running[d["Id"]], name)
        if cleanup and self.cleanup and self.d.inspect(new_id)["Image"] != old_img:
            self.remove_old_image(old_img, ref)
        return new_id

    def _collect_copy_files(self, ctr):
        """Read files listed in the updock.copy-file label (files that live only in the
        container's writable layer, e.g. Compose `configs: content:`) so the replacement
        gets them too. Returns [(path, TarInfo, bytes)]."""
        name = ctr["Name"].lstrip("/")
        raw = (ctr["Config"].get("Labels") or {}).get(COPY_LABEL, "")
        paths = [p.strip() for p in raw.split(",") if p.strip()]
        if not paths:
            return []
        if ctr["HostConfig"].get("ReadonlyRootfs"):
            self._log(f"{COPY_LABEL} ignored: read-only root filesystem", logging.WARNING, name,
                      "copy-file")
            return []
        mounts = [m["Destination"].rstrip("/") for m in ctr.get("Mounts") or []]
        out, total = [], 0
        for path in paths:
            if not path.startswith("/") or ".." in path.split("/") or path.endswith("/"):
                raise CopyFileError(f"{COPY_LABEL}: invalid path {path!r} (must be an "
                                    "absolute file path without '..')")
            if any(path == m or path.startswith(m + "/") for m in mounts):
                self._log(f"{path} is on a volume/bind mount and survives anyway; not copied",
                          logging.INFO, name, "copy-file")
                continue
            try:
                data = self.d.get_archive(ctr["Id"], path, COPY_MAX_FILE + 64 * 1024)
            except DockerError as e:
                if e.status == 404:
                    self._log(f"{path} doesn't exist; not copied", logging.WARNING, name,
                              "copy-file")
                    continue
                if e.status == 413:
                    raise CopyFileError(f"{COPY_LABEL}: {path} is over 1 MiB") from None
                raise
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                members = tf.getmembers()
                if len(members) != 1 or not members[0].isreg():
                    raise CopyFileError(f"{COPY_LABEL}: {path} is not a regular file")
                info = members[0]
                if info.size > COPY_MAX_FILE:
                    raise CopyFileError(f"{COPY_LABEL}: {path} is over 1 MiB")
                total += info.size
                if total > COPY_MAX_TOTAL:
                    raise CopyFileError(f"{COPY_LABEL}: files total over 16 MiB")
                out.append((path, info, tf.extractfile(info).read()))
        return out

    def _write_copy_files(self, cid, name, files):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for path, info, data in files:
                ti = tarfile.TarInfo(path.lstrip("/"))
                ti.size, ti.mode, ti.uid, ti.gid, ti.mtime = (len(data), info.mode, info.uid,
                                                              info.gid, info.mtime)
                tf.addfile(ti, io.BytesIO(data))
        self.d.put_archive(cid, "/", buf.getvalue())
        self._log(f"copied {', '.join(p for p, _, _ in files)} into the new container",
                  container=name, event="copy-file")

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

    def disk_status(self, max_age=0):
        """Measure Docker image storage against the configured limits (cached `max_age` s)."""
        if not (self.disk_max or self.disk_warn):
            return {"enabled": False}
        if self._disk and max_age and time.time() - self._disk["t"] < max_age:
            return self._disk
        u = self.d.disk_usage()
        used = u["used"]
        status = ("blocked" if self.disk_max and used >= self.disk_max else
                  "warning" if self.disk_warn and used >= self.disk_warn else "ok")
        self._disk = {"enabled": True, **u, "max": self.disk_max or None,
                      "warn": self.disk_warn or None, "status": status, "t": time.time()}
        return self._disk

    def _disk_gate(self):
        """Return True if updates may proceed. Fails closed if usage can't be read."""
        if not (self.disk_max or self.disk_warn):
            return True
        try:
            d = self.disk_status()
        except Exception as e:
            status, ok = "unknown", False
            msg = f"could not read Docker disk usage ({e}); skipping updates this run"
        else:
            status, ok = d["status"], d["status"] != "blocked"
            used = format_size(d["used"])
            msg = {"blocked": f"image storage {used} is at/over the limit of "
                              f"{format_size(self.disk_max)}; this run only checks "
                              f"({format_size(d['reclaimable'])} reclaimable)",
                   "warning": f"image storage {used} is over the warning level of "
                              f"{format_size(self.disk_warn)}",
                   "ok": f"image storage {used}"}[status]
        level = {"ok": logging.DEBUG, "warning": logging.WARNING}.get(status, logging.ERROR)
        self._log(msg, level, event="disk")
        if status != self._disk_alerted:
            if status != "ok":
                self.notifier.notify("disk", None, msg)
            self._disk_alerted = status
        return ok

    def run_all(self, update=True):
        t0 = time.monotonic()
        self.counters["runs"] += 1
        self._log(f"run started ({'update' if update else 'check only'})", event="run")
        try:
            with self.notifier.batch():
                if update and not self._disk_gate():
                    update = False
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
        except Exception:
            self.counters["run_failures"] += 1
            raise
        finally:
            self.last_run = {"t": time.time(), "duration": time.monotonic() - t0}
        self._log(f"run finished in {self.last_run['duration']:.1f}s", event="run")
