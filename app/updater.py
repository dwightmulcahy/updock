import fnmatch
import io
import json
import logging
import os
import tarfile
import threading
import time
from collections import Counter, defaultdict

import httpx

from . import cosign
from .changelog import TEMPLATE_LABEL, changelog_url, source_of
from .docker_api import DockerError, registry_credentials, split_ref
from .logs import LogBuffer
from .notify import EmailNotifier
from .registry import (REVISION_LABELS, VERSION_LABELS, RateLimited, RegistryAuthError,
                       RegistryError, parse_repo)
from .semver import LEVELS, SEMVER_LABEL, best as semver_best
from .schedule import format_duration, parse_duration, parse_timestamp
from .units import format_size

ENABLE_LABEL = "updock.enable"
COOLDOWN_LABEL = "updock.cooldown"
SCOPE_LABEL = "updock.scope"
MONITOR_ONLY_LABEL = "updock.monitor-only"
REVIVE_LABEL = "updock.revive"
HEALTH_TIMEOUT_LABEL = "updock.health-timeout"
HEALTH_URL_LABEL = "updock.health-url"
TRUE = {"1", "true", "yes", "on"}
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


def http_probe(url, timeout):
    """GET url and return the status code (redirects are not followed: 3xx counts as up)."""
    return httpx.get(url, timeout=timeout, follow_redirects=False, trust_env=False).status_code


def base_repo(ref):
    return split_ref(ref)[0].split("@", 1)[0]


class Updater:
    def __init__(self, docker, cleanup=True, label_enable=False, registry=None, logbuf=None,
                 health_timeout=120, startup_grace=0, sleep=time.sleep, clock=time.monotonic,
                 digest_check=True, notifier=None, store=None, include_stopped=False,
                 cooldown=0, hooks=False, disk_max=0, disk_warn=0, exclude_names=(),
                 include_images=(), exclude_images=(), scope=None, revive_stopped=False,
                 probe=None, cosign_data_dir=None):
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
        self.exclude_names = [p for p in exclude_names if p]
        self.include_images = [p for p in include_images if p]
        self.exclude_images = [p for p in exclude_images if p]
        self.scope = scope or None
        self.revive_stopped = revive_stopped
        self.probe = probe or http_probe
        self.cosign_data_dir = cosign_data_dir
        self._backoff = {}  # registry host -> {"until": ts, "hits": n}
        self.on_change = None  # callback(name, state) for live UI updates
        self.on_run = None     # callback(run status or None) for the UI's run banner
        self.run = None        # the scheduled/bulk run in progress, if any
        self.queue = set()     # containers still waiting in that run
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

    def exclusion(self, c):
        """Why Updock leaves this container alone, or None if it may manage it."""
        labels = c.get("Labels") or {}
        name = c["Names"][0].lstrip("/")
        if labels.get(SELF_LABEL) == "true" or (self.self_id and c["Id"].startswith(self.self_id)):
            return "this is Updock"
        scope = labels.get(SCOPE_LABEL) or None
        if scope != self.scope:
            return (f"scope '{scope}' belongs to another Updock" if scope else
                    f"not in scope '{self.scope}'")
        v = labels.get(ENABLE_LABEL)
        if self.label_enable and v != "true":
            return f"no {ENABLE_LABEL}=true label"
        if not self.label_enable and v == "false":
            return f"label {ENABLE_LABEL}=false"
        for pat in self.exclude_names:
            if fnmatch.fnmatchcase(name, pat):
                return f"name matches UPDOCK_EXCLUDE '{pat}'"
        image = c["Image"] if not c["Image"].startswith("sha256:") else \
            self.state.get(name, {}).get("ref", c["Image"])
        if self.include_images and not any(fnmatch.fnmatchcase(image, p)
                                           for p in self.include_images):
            return "image doesn't match UPDOCK_INCLUDE_IMAGES"
        for pat in self.exclude_images:
            if fnmatch.fnmatchcase(image, pat):
                return f"image matches UPDOCK_EXCLUDE_IMAGES '{pat}'"
        return None

    def eligible(self, c):
        return self.exclusion(c) is None

    def revives(self, ctr):
        """Start a stopped container after its image is updated? Label updock.revive
        (true/false) overrides UPDOCK_REVIVE_STOPPED."""
        v = (ctr["Config"].get("Labels") or {}).get(REVIVE_LABEL)
        if v is None:
            return self.revive_stopped
        return str(v).lower() in TRUE

    def monitor_only(self, name, labels):
        """Pinned in the UI or labelled updock.monitor-only: scheduled runs only check."""
        return bool(self.state.get(name, {}).get("pinned")) or \
            str((labels or {}).get(MONITOR_ONLY_LABEL, "")).lower() in TRUE

    def list(self):
        out = []
        for c in self.d.containers(all=True):
            name = c["Names"][0].lstrip("/")
            if name.endswith("-updock-old"):
                continue  # the previous container, kept only until the update succeeds
            st = dict(self.state.get(name, {}))
            ref = st.pop("ref", None) or c["Image"]
            why = self.exclusion(c)
            row = {"name": name, "image": ref, "image_id": c["ImageID"][7:19],
                   "state": c["State"], "status": c["Status"], "eligible": why is None,
                   "excluded": why,
                   "monitor_only": str((c.get("Labels") or {}).get(MONITOR_ONLY_LABEL, "")).lower()
                   in TRUE,
                   "current": self._image_info(c["ImageID"]), "queued": name in self.queue,
                   "project": (c.get("Labels") or {}).get(COMPOSE_PROJECT),
                   "service": (c.get("Labels") or {}).get(COMPOSE_SERVICE),
                   **st}
            template = (c.get("Labels") or {}).get(TEMPLATE_LABEL)
            for key in ("current", "latest"):
                if isinstance(row.get(key), dict):
                    row[key] = {**row[key], "changelog": self.changelog(row[key], template)}
            out.append(row)
        return sorted(out, key=lambda x: x["name"])

    def check(self, name):
        ctr = self.d.inspect(name)
        ref = ctr["Config"]["Image"]
        self._set(name, ref=ref, target_ref=None)
        if split_ref(ref)[1] is None:
            self._set(name, job="pinned-digest", message="Image is pinned by digest/ID", last_check=time.time())
            return ctr, False
        host = parse_repo(ref)[0]
        wait = self._backoff_left(host)
        if wait:
            raise RateLimited(host, wait, waiting=True)  # don't touch the registry at all
        level = self.semver_level(name, ctr)
        if self.registry and level:
            try:
                if self._check_semver(name, ctr, ref, level):
                    self._backoff.pop(host, None)
                    return ctr, True
            except RateLimited as e:
                raise self._rate_limited(e)
            except RegistryAuthError:
                raise
            except Exception as e:
                self._log(f"{SEMVER_LABEL}: couldn't list tags ({e}); checking {ref} only",
                          logging.WARNING, name, "check")
        if self.registry and self.digest_check:
            try:
                avail = self._check_remote(name, ctr, ref)
                self._backoff.pop(host, None)
                return ctr, avail
            except RateLimited as e:  # no pull fallback: that would spend pull quota too
                raise self._rate_limited(e)
            except RegistryAuthError:  # a pull would be refused the same way
                raise
            except Exception as e:
                self._log(f"registry digest check failed ({e}); pulling instead",
                          logging.DEBUG, name, "check")
        avail = self._pull_guarded(name, ctr, ref)
        return ctr, avail

    def semver_level(self, name, ctr):
        """Label updock.semver=patch|minor|major: follow newer version tags in that range."""
        raw = (ctr["Config"].get("Labels") or {}).get(SEMVER_LABEL)
        if not raw:
            return None
        level = raw.strip().lower()
        if level in LEVELS:
            return level
        if name not in self._bad_labels:
            self._bad_labels.add(name)
            self._log(f"invalid {SEMVER_LABEL}={raw!r} (use patch, minor or major); "
                      "ignoring it", logging.WARNING, name, "check")
        return None

    def _check_semver(self, name, ctr, ref, level):
        """Look for a newer version tag within `level`. Returns True (and records it as
        the update target) if one exists; False to fall back to the same-tag check."""
        tag = split_ref(ref)[1]
        self._set(name, job="checking", message=f"Looking for newer {level} versions of {ref}")
        newer = semver_best(tag, self.registry.tags(ref), level)
        if not newer:
            self._log(f"no newer {level} version than {tag}", logging.DEBUG, name, "check")
            return False
        target = f"{base_repo(ref)}:{newer}"
        try:
            local = self.d.image(ctr["Image"])
            platform = {"os": local.get("Os"), "architecture": local.get("Architecture"),
                        "variant": local.get("Variant")}
        except Exception:
            platform = None
        try:
            latest = self.registry.remote_info(target, platform)
        except Exception as e:
            latest = {"id": None, "digest": None, "tags": [], "version": None,
                      "revision": None, "created": None, "note": f"details unavailable: {e}"}
        latest["tag"] = newer
        self._unpulled.add(name)
        self._log(f"newer {level} version: {tag} -> {newer}", container=name, event="check")
        self._set(name, job="update-available", latest=latest, update_available=True,
                  target_ref=target, last_check=time.time(), message="")
        return True

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
        self._set(name, job="pulling", step="pull", message=f"Pulling {ref}")
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
        except DockerError as e:
            text = str(e).lower()
            if any(k in text for k in ("unauthorized", "denied", "authentication required",
                                       "no basic auth credentials")):
                host, repo = parse_repo(ref)
                raise RegistryAuthError(host, repo) from None
            raise
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
                self._log(f"newer image found, but its details couldn't be read: {e}",
                          logging.WARNING, name, "check")
                latest = {"id": None, "digest": remote, "tags": [], "version": None,
                          "revision": None, "created": None,
                          "note": f"details unavailable: {e}"}
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
        self.state[name].update(job_started=time.time(), step=None, health_until=None)
        try:
            fn()
        except RegistryAuthError as e:
            self.counters["result_auth_required"] += 1
            self._set(name, job="auth-required", message=str(e), auth_host=e.host)
            self._log(str(e), logging.WARNING, name, "auth")
            if notify_errors:
                self.notifier.notify("error", name, str(e))
        except RateLimited as e:
            self.counters["result_rate_limited"] += 1
            self._set(name, job="rate-limited", message=str(e))
            self._log(str(e), logging.INFO if e.waiting else logging.WARNING, name, "rate-limit")
        except HookSkip as e:
            self.counters["result_skipped"] += 1
            self._set(name, job="skipped", message=str(e))
            self._log(str(e), container=name, event="hook")
        except cosign.SignatureError as e:
            self.counters["result_unverified"] += 1
            self._set(name, job="unverified", message=str(e))
            self._log(str(e), logging.ERROR, name, "signature")
            self.notifier.notify("error", name, str(e))
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
            self._set(name, step=None, health_until=None)
            lock.release()

    def update(self, name, force=False, scheduled=False):
        def job():
            self.state[name]["action"] = "update"
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
            target_ref = self.state[name].get("target_ref") if avail else None
            new_digest = (self.state[name].get("latest") or {}).get("digest")
            if scheduled and avail and new_digest and \
                    new_digest == self.state[name].get("unverified_digest"):
                self._log(f"skipping {new_digest[7:19]}: its signature check failed before; "
                          "use Update now to retry", logging.WARNING, name, "signature")
                self._set(name, job="unverified")
                return
            if avail and name in self._unpulled:
                avail = self._pull_guarded(name, ctr, target_ref or ctr["Config"]["Image"])
            if not (avail or force):
                self._log("up to date", container=name, event="check")
                return
            if avail:
                self._verify_signature(name, ctr, target_ref or ctr["Config"]["Image"])
            target = (self.state[name].get("latest") or {}).get("id")
            failed = self.state[name].get("failed_image")
            if scheduled and avail and target and target == failed:
                self._log(f"skipping image {target}: it failed its health check before; "
                          "use Update now to retry", logging.WARNING, name, "update")
                self._set(name, job="rolled-back")
                return
            self._set(name, job="updating", step="pull" if name in self._unpulled else "stop",
                      message="Recreating")
            change = self._describe_change(ctr)  # before cleanup removes the old image
            prev = self._previous_of(ctr)
            revive = avail and not ctr["State"]["Running"] and self.revives(ctr)
            if revive:
                self._log("stopped, but will be started after the update (revive)",
                          container=name, event="update")
            try:
                self._recreate(ctr, image_ref=target_ref, cleanup=True,
                               start=True if revive else None)
                self._restart_compose_dependents(ctr)
            except UnhealthyError:
                if avail:
                    self._set(name, failed_image=target)
                raise
            self.counters["result_updated" if avail else "result_recreated"] += 1
            self._set(name, job="updated", update_available=False, last_update=time.time(),
                      message="", failed_image=None, target_ref=None, previous=prev,
                      **({"ref": target_ref} if target_ref else {}))
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
            self.state[name]["action"] = "check"
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
        target = self.state[name].get("target_ref")
        text = f"{ctr['Config']['Image']} {fmt(old)} -> {target + ' ' if target else ''}{fmt(new)}"
        notes = self.changelog(new, (ctr["Config"].get("Labels") or {}).get(TEMPLATE_LABEL))
        return text + (f"\nRelease notes: {notes}" if notes else "")

    @staticmethod
    def changelog(info, template=None):
        return changelog_url(info.get("source"), info.get("version"), template)

    def recheck_auth(self, key):
        """Re-check containers that failed with 'auth required' for registry `key`."""
        from .docker_api import normalize_registry
        names = [n for n, st in list(self.state.items())
                 if st.get("job") == "auth-required"
                 and normalize_registry(st.get("auth_host")) == key]
        for n in names:
            self.check_only(n)
        return names

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
            self.state[name]["action"] = "deploy"
            ctr = self.d.inspect(name)
            new_ref = f"{base_repo(ctr['Config']['Image'])}:{tag}"
            self._set(name, job="pulling", step="pull", message=f"Pulling {new_ref}")
            self._pull_image(name, new_ref)
            self._verify_signature(name, ctr, new_ref)
            self._set(name, job="updating", message=f"Deploying {new_ref}")
            prev = self._previous_of(ctr)
            # Keep the previous image so switching back is instant.
            self._recreate(ctr, image_ref=new_ref, cleanup=False)
            self._restart_compose_dependents(ctr)
            self._set(name, job="deployed", ref=new_ref, update_available=False, previous=prev,
                      latest=None, last_update=time.time(), message="")
            self._log(f"deployed {new_ref}", container=name, event="deploy")
            self.notifier.notify("deploy", name, f"deployed {new_ref}")
        self._locked(name, job)

    def _verify_signature(self, name, ctr, ref):
        """With updock.cosign.* labels: the pulled image must carry a valid signature,
        checked on the exact digest that was pulled, before anything is replaced."""
        args = cosign.policy_args(ctr["Config"].get("Labels"))
        if args is None:
            return
        want = parse_repo(ref)
        img = self.d.image(ref)
        digests = [d for d in img.get("RepoDigests") or [] if parse_repo(d) == want]
        if not digests:
            raise cosign.SignatureError(f"{ref} has no registry digest, so its signature "
                                        "can't be checked")
        target = f"{base_repo(ref)}@{digests[0].split('@', 1)[1]}"
        self._set(name, message=f"Checking the signature of {target}")
        try:
            cosign.run(args, target, credentials=registry_credentials(ref),
                       data_dir=self.cosign_data_dir)
        except cosign.SignatureError:
            self._set(name, unverified_digest=digests[0].split("@", 1)[1])
            raise
        self._set(name, unverified_digest=None)
        self._log(f"signature verified for {target}", container=name, event="signature")

    def _previous_of(self, ctr):
        """What the container runs now, so a later rollback can restore it."""
        info = self._image_info(ctr["Image"]) or {}
        return {"ref": ctr["Config"]["Image"], "image": ctr["Image"],
                "digest": info.get("digest"), "version": info.get("version"),
                "id": info.get("id"), "t": time.time()}

    def rollback(self, name):
        """Go back to the image the container ran before its last update or deploy, and
        pin it so scheduled runs don't apply the newer image again. Rolling back twice
        returns to the newer image."""
        def job():
            self.state[name]["action"] = "rollback"
            prev = self.state[name].get("previous")
            if not prev:
                raise RuntimeError("no earlier version recorded for this container")
            ctr = self.d.inspect(name)
            back = self._previous_of(ctr)
            ref = prev["ref"]
            label = prev.get("version") or (prev.get("id") or prev["image"][7:19])
            self._set(name, job="pulling", step="pull", message=f"Getting {ref} ({label})")
            image = self._restore_image(name, prev)
            if split_ref(ref)[1] is not None:
                self.d.tag_image(image, ref)  # the tag points at the earlier image again
            else:
                ref = image
            self._set(name, job="updating", message=f"Rolling back to {label}")
            self._recreate(ctr, image_ref=ref, cleanup=False)  # keep the newer one too
            self._restart_compose_dependents(ctr)
            self._set(name, job="reverted", ref=ref, previous=back, pinned=True,
                      update_available=False, latest=None, last_update=time.time(),
                      message=f"rolled back to {label}; pinned so scheduled runs don't "
                              "re-apply the newer image (unpin to resume)")
            self._log(f"rolled back to {ref} ({label}) and pinned", logging.WARNING, name,
                      "rollback")
            self.notifier.notify("rollback", name, f"manually rolled back to {ref} ({label})")
        self._locked(name, job)

    def _restore_image(self, name, prev):
        """The earlier image: still local, or pulled back from the registry by digest."""
        try:
            self.d.image(prev["image"])
            return prev["image"]
        except Exception:
            pass
        if not prev.get("digest"):
            raise RuntimeError("the earlier image was deleted and has no registry digest "
                               "to pull it back from")
        pinned = f"{base_repo(prev['ref'])}@{prev['digest']}"
        self._log(f"earlier image no longer local; pulling {pinned}", container=name,
                  event="rollback")
        self._pull_image(name, pinned)
        return pinned

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
        orig_running = ctr["State"]["Running"]
        was_running = orig_running if start is None else start  # start the new one?
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
        if orig_running:
            self._log(f"stopping (timeout {timeout}s)", logging.DEBUG, name, "recreate")
            if not network_parent:
                self._set(name, step="stop", message=f"Stopping (up to {timeout}s)")
            self.d.stop(old_id, timeout)
        self.d.rename(old_id, f"{name}-updock-old")
        new_id = None
        try:
            if not network_parent:
                self._set(name, step="start", message="Starting the new container")
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
                self._verify(new_id, name, was_unhealthy=(
                    ((ctr["State"].get("Health") or {}).get("Status") == "unhealthy")))
                if not network_parent:
                    self._verify_url(new_id, name)
        except Exception as e:
            self._log(f"{e}; rolling back to previous container", logging.ERROR, name, "rollback")
            if new_id:
                self.d.remove(new_id)
            self.d.rename(old_id, name)
            if orig_running:  # restore exactly what was there (a revived one stays stopped)
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
            self._set(name, step="cleanup", message="Removing the old image")
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

    def _verify(self, cid, name, was_unhealthy=False):
        """Wait for the new container to prove itself; raise UnhealthyError to roll back.

        With a healthcheck: wait up to the health timeout for `healthy`. Docker may report
        `unhealthy` while a slow app is still starting (its start period/retries ran out)
        and flip to `healthy` later, so `unhealthy` alone is not final: only the timeout,
        an exit or a restart loop is. Without a healthcheck: it must still be running
        after startup_grace seconds."""
        info = self.d.inspect(cid)
        test = (info["Config"].get("Healthcheck") or {}).get("Test") or []
        has_hc = bool(test) and test[0] != "NONE"
        if has_hc and was_unhealthy:
            # Rolling back to a container that fails its healthcheck too gains nothing:
            # judge the new one like a container without a healthcheck instead.
            has_hc = False
            self._log("its healthcheck was already failing before the update, so only "
                      "checking that the new container stays up", logging.WARNING, name,
                      "health")
        limit = self.health_timeout_for(name, info) if has_hc else self.startup_grace
        deadline = self.clock() + limit
        self._set(name, step="health", health_until=time.time() + limit,
                  message=f"Waiting up to {limit}s for the healthcheck" if has_hc else
                  f"Making sure it stays up for {limit}s")
        self._log(f"waiting up to {limit}s for the healthcheck" if has_hc else
                  f"checking it stays up for {limit}s", logging.DEBUG, name, "health")
        restarts = info.get("RestartCount", 0)
        last_out, warned = "", False
        while True:
            st = info["State"]
            if not st.get("Running") or st.get("Restarting"):
                raise UnhealthyError(f"new container exited (code {st.get('ExitCode')})")
            if info.get("RestartCount", 0) > restarts:
                raise UnhealthyError(f"new container crashed and was restarted "
                                     f"(exit code {st.get('ExitCode')})")
            health = st.get("Health") or {}
            status = health.get("Status")
            if has_hc and status == "healthy":
                self._log("new container is healthy", container=name, event="health")
                return
            if has_hc and status == "unhealthy":
                last_out = ((health.get("Log") or [{}])[-1].get("Output") or "").strip()[:300]
                if not warned:
                    warned = True
                    left = max(0, int(deadline - self.clock()))
                    self._log(f"healthcheck failing so far; waiting up to {left}s more"
                              + (f" ({last_out})" if last_out else ""),
                              logging.INFO, name, "health")
            if self.clock() >= deadline:
                if has_hc:
                    raise UnhealthyError(
                        f"new container not healthy after {limit}s"
                        + (f"; last check: {last_out}" if last_out else "")
                        + f" (raise {HEALTH_TIMEOUT_LABEL} if it just starts slowly)")
                return
            self.sleep(min(2, max(0.1, deadline - self.clock())))
            info = self.d.inspect(cid)

    def _verify_url(self, cid, name):
        """Label updock.health-url: the app must answer that URL with a 2xx/3xx status
        within the health timeout, or the update is rolled back. Requested by Updock
        itself, so use an address Updock can reach (a published port, or the
        container's name on a shared network)."""
        info = self.d.inspect(cid)
        url = ((info["Config"].get("Labels") or {}).get(HEALTH_URL_LABEL) or "").strip()
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            self._log(f"invalid {HEALTH_URL_LABEL}={url!r} (must be http:// or https://); "
                      "not probing", logging.WARNING, name, "health")
            return
        limit = self.health_timeout_for(name, info)
        deadline = self.clock() + limit
        self._set(name, step="health", health_until=time.time() + limit,
                  message=f"Waiting up to {limit}s for {url} to answer")
        last = None
        while True:
            try:
                code = self.probe(url, min(10, max(1, limit)))
                if 200 <= code < 400:
                    self._log(f"{url} answered {code}", container=name, event="health")
                    return
                last = f"HTTP {code}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            st = self.d.inspect(cid)["State"]
            if not st.get("Running") or st.get("Restarting"):
                raise UnhealthyError(f"new container exited (code {st.get('ExitCode')}) "
                                     f"while waiting for {url}")
            if self.clock() >= deadline:
                raise UnhealthyError(f"{url} did not answer with 2xx/3xx within {limit}s "
                                     f"(last: {last})")
            self.sleep(min(2, max(0.1, deadline - self.clock())))

    def health_timeout_for(self, name, info):
        """Per-container label updock.health-timeout (e.g. 5m) overrides UPDOCK_HEALTH_TIMEOUT."""
        raw = (info["Config"].get("Labels") or {}).get(HEALTH_TIMEOUT_LABEL)
        if raw is None:
            return self.health_timeout
        try:
            return parse_duration(raw)
        except ValueError:
            self._log(f"invalid {HEALTH_TIMEOUT_LABEL}={raw!r}; using {self.health_timeout}s",
                      logging.WARNING, name, "health")
            return self.health_timeout

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

    def run_status(self):
        return dict(self.run, results=dict(self.run["results"])) if self.run else None

    def _publish_run(self):
        if self.on_run:
            try:
                self.on_run(self.run_status())
            except Exception:  # the banner must never break a run
                pass

    def projects(self):
        """Compose project names of the containers Updock can see."""
        return sorted({(c.get("Labels") or {}).get(COMPOSE_PROJECT)
                       for c in self.d.containers(all=True)} - {None, ""})

    def run_all(self, update=True, project=None):
        """Check (or update) every eligible container, Compose dependencies first.
        `project` limits the run to one Compose project."""
        if self.run:
            self._log("a run is already in progress; request ignored", logging.WARNING,
                      event="run")
            return False
        t0 = time.monotonic()
        self.counters["runs"] += 1
        self._log(f"run started ({'update' if update else 'check only'}"
                  f"{f', project {project}' if project else ''})", event="run")
        self.run = {"kind": "update" if update else "check", "project": project,
                    "total": 0, "done": 0,
                    "current": None, "started": time.time(), "results": Counter()}
        self._publish_run()
        try:
            with self.notifier.batch():
                if update and not self._disk_gate():
                    update = False
                    self.run["kind"] = "check"
                targets = [c for c in compose_order(self.d.containers(all=self.include_stopped))
                           if self.eligible(c) and (project is None or
                                                    (c.get("Labels") or {}).get(COMPOSE_PROJECT)
                                                    == project)]
                self.queue = {c["Names"][0].lstrip("/") for c in targets}
                self.run["total"] = len(targets)
                self._publish_run()
                for c in targets:
                    name = c["Names"][0].lstrip("/")
                    self.queue.discard(name)
                    self.run["current"] = name
                    self._publish_run()
                    if update and self.monitor_only(name, c.get("Labels")):
                        self._log("pinned / monitor-only: checking only", logging.DEBUG,
                                  name, "pin")
                        self.check_only(name, scheduled=True)
                    elif update:
                        self.update(name, scheduled=True)
                    else:
                        self.check_only(name, scheduled=True)
                    self.run["done"] += 1
                    self.run["results"][self.state.get(name, {}).get("job") or "unknown"] += 1
        except Exception:
            self.counters["run_failures"] += 1
            raise
        finally:
            summary = self.run_status()
            self.last_run = {"t": time.time(), "duration": time.monotonic() - t0,
                             "kind": summary["kind"], "project": project,
                             "total": summary["total"],
                             "results": summary["results"]}
            self.run, self.queue = None, set()
            self._publish_run()
        self._log(f"run finished in {self.last_run['duration']:.1f}s", event="run")
        return True
