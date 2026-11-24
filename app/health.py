"""Health monitor: watches Docker healthchecks between updates.

Every UPDOCK_HEALTH_INTERVAL it reads each running container's health from the container
list (no per-container requests). Consecutive `unhealthy` observations are counted; after
UPDOCK_HEALTH_NOTIFY_AFTER of them an `unhealthy` notification goes out (once per episode),
and a `healthy` one when the container recovers. With UPDOCK_HEALTH_RESTART_AFTER (or the
label updock.health.restart-after) the container is restarted after that many failures,
at most UPDOCK_HEALTH_RESTART_ATTEMPTS times per episode. Status changes, restarts and
notifications are kept as a history (UPDOCK_HEALTH_HISTORY, default 7 days)."""
import json
import logging
import os
import re
import threading
import time
from collections import deque

from .schedule import format_duration
from .updater import is_protected

log = logging.getLogger("updock")

MONITOR_LABEL = "updock.health.monitor"
RESTART_LABEL = "updock.health.restart-after"
MAX_HISTORY = 5000
_STATUS = re.compile(r"\((healthy|unhealthy|health: starting)\)")


def health_of(status_text):
    """'Up 3 minutes (unhealthy)' -> 'unhealthy'; None when there is no healthcheck."""
    m = _STATUS.search(status_text or "")
    if not m:
        return None
    return "starting" if m[1] == "health: starting" else m[1]


class HealthMonitor:
    def __init__(self, docker, updater, notifier, data_dir=None, interval=0, notify_after=3,
                 restart_after=0, restart_attempts=3, history_seconds=7 * 86400,
                 clock=time.time):
        self.d, self.up, self.notifier = docker, updater, notifier
        self.interval = interval
        self.notify_after, self.restart_after = notify_after, restart_after
        self.restart_attempts = restart_attempts
        self.history_seconds = history_seconds
        self.clock = clock
        self.state = {}  # name -> {status, failures, restarts, notified, since, gave_up}
        self.history = deque(maxlen=MAX_HISTORY)
        self.restarts_total = 0
        self.last_tick = None
        self._lock = threading.Lock()
        self.path = os.path.join(data_dir, "health.jsonl") if data_dir else None
        self._load()

    @property
    def enabled(self):
        return self.interval > 0

    # ---- history persistence ----
    def _load(self):
        if not self.path:
            return
        keep, cutoff = [], self.clock() - self.history_seconds
        try:
            with open(self.path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and e.get("t", 0) >= cutoff:
                        keep.append(e)
        except OSError:
            return
        self.history.extend(keep[-MAX_HISTORY:])
        self._rewrite()

    def _rewrite(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(json.dumps(e) + "\n" for e in self.history)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save health history: %s", e, extra={"event": "health"})

    def _record(self, name, status, **extra):
        e = {"t": self.clock(), "container": name, "status": status, **extra}
        self.history.append(e)
        if self.path:
            try:
                with open(self.path, "a") as f:
                    f.write(json.dumps(e) + "\n")
            except OSError:
                pass
        return e

    def _prune(self):
        cutoff, before = self.clock() - self.history_seconds, len(self.history)
        while self.history and self.history[0].get("t", 0) < cutoff:
            self.history.popleft()
        if self.path and len(self.history) != before:
            self._rewrite()

    # ---- monitoring ----
    def monitored(self, c):
        labels = c.get("Labels") or {}
        name = c["Names"][0].lstrip("/")
        if name.endswith(("-updock-old", "-self-update")):
            return False
        if str(labels.get(MONITOR_LABEL, "")).strip().lower() in ("0", "false", "no", "off"):
            return False
        return (labels.get("updock.scope") or None) == self.up.scope

    def restart_after_for(self, name, labels):
        raw = (labels or {}).get(RESTART_LABEL)
        if raw is None:
            return self.restart_after
        try:
            return max(0, int(str(raw).strip()))
        except ValueError:
            return self.restart_after

    def tick(self):
        """One pass over running containers. Returns the number of containers watched."""
        with self._lock:
            self.last_tick = self.clock()
            seen = set()
            with self.notifier.batch():
                for c in self.d.containers():
                    name = c["Names"][0].lstrip("/")
                    status = health_of(c.get("Status"))
                    if status is None or not self.monitored(c):
                        continue
                    seen.add(name)
                    if self.up.locks[name].locked():
                        continue  # an update is replacing it: its own checks apply
                    try:
                        self._observe(name, c, status)
                    except Exception as e:  # one container must not stop the pass
                        log.error("health check of %s failed: %s", name, e,
                                  extra={"container": name, "event": "health"})
            for gone in set(self.state) - seen:
                self.state.pop(gone, None)
            self._prune()
            return len(seen)

    def _observe(self, name, c, status):
        st = self.state.get(name)
        if st is None or st.get("id") != c["Id"]:  # new, or replaced under the same name
            st = self.state[name] = {"id": c["Id"], "status": None, "failures": 0,
                                     "restarts": 0, "notified": False, "since": self.clock(),
                                     "gave_up": False}
        prev = st["status"]
        if status != prev:
            st["status"], st["since"] = status, self.clock()
            if prev is not None or status != "healthy":
                self._record(name, status)
        if status == "starting":
            return
        if status == "healthy":
            if prev == "unhealthy" and st["notified"]:
                msg = (f"healthy again after {format_duration(self.clock() - st['began'])}"
                       + (f" and {st['restarts']} restart(s)" if st["restarts"] else ""))
                log.info(msg, extra={"container": name, "event": "health"})
                self.notifier.notify("healthy", name, msg, {"image": c.get("Image")})
                self._record(name, "healthy", notified=True, note=msg)
            st.update(failures=0, restarts=0, notified=False, gave_up=False)
            st.pop("began", None)
            return
        # unhealthy
        st["failures"] += 1
        st.setdefault("began", self.clock())
        if st["failures"] == 1:
            log.warning("healthcheck failing", extra={"container": name, "event": "health"})
        if self.notify_after > 0 and st["failures"] >= self.notify_after and not st["notified"]:
            st["notified"] = True
            msg = f"unhealthy for {st['failures']} checks in a row"
            self.notifier.notify("unhealthy", name, msg, {"image": c.get("Image")})
            self._record(name, "unhealthy", notified=True, failures=st["failures"], note=msg)
        self._maybe_restart(name, c, st)

    def _maybe_restart(self, name, c, st):
        labels = c.get("Labels") or {}
        every = self.restart_after_for(name, labels)
        if every <= 0 or st["failures"] < (st["restarts"] + 1) * every:
            return
        if st["restarts"] >= self.restart_attempts:
            if not st["gave_up"]:
                st["gave_up"] = True
                msg = f"still unhealthy after {st['restarts']} restart(s); not restarting again"
                log.error(msg, extra={"container": name, "event": "health"})
                self._record(name, "unhealthy", note=msg, failures=st["failures"])
            return
        if self.up.exclusion(c) == "this is Updock":
            return
        if is_protected(labels):
            if not st["gave_up"]:
                st["gave_up"] = True
                log.warning("not restarting: protected", extra={"container": name,
                                                                "event": "health"})
            return
        st["restarts"] += 1
        self.restarts_total += 1
        try:
            timeout = self.d.inspect(c["Id"])["Config"].get("StopTimeout") or 10
            self.d.restart(c["Id"], timeout)
            msg = (f"restarted after {st['failures']} failed healthchecks "
                   f"(attempt {st['restarts']} of {self.restart_attempts})")
            log.warning(msg, extra={"container": name, "event": "health"})
            self._record(name, "unhealthy", restart=True, failures=st["failures"], note=msg)
        except Exception as e:
            msg = f"restart failed: {e}"
            log.error(msg, extra={"container": name, "event": "health"})
            self._record(name, "unhealthy", restart=False, failures=st["failures"], note=msg)

    # ---- API ----
    def status(self):
        return {"enabled": self.enabled, "interval": self.interval,
                "notify_after": self.notify_after, "restart_after": self.restart_after,
                "restart_attempts": self.restart_attempts,
                "history_seconds": self.history_seconds, "last_tick": self.last_tick,
                "restarts": self.restarts_total,
                "containers": {n: dict(st) for n, st in sorted(self.state.items())}}

    def query(self, container=None, limit=200):
        out = [e for e in self.history if not container or e.get("container") == container]
        return list(reversed(out))[:max(0, limit)] if limit else list(reversed(out))

    def loop(self, sleep=time.sleep):
        while True:
            sleep(self.interval)
            try:
                self.tick()
            except Exception as e:
                log.exception("health monitor pass failed: %s", e, extra={"event": "health"})
