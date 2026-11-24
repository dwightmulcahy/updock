"""Self-update: replace Updock's own container with a newer image.

A process can't recreate the container it runs in, so Updock starts a short-lived helper
container from the *new* image (`<name>-self-update`, same Docker access, data volume and
networks, no published ports) that runs `python -m app.selfupdate <container> <ref>`. The
helper uses the normal update path: stop, rename, create the replacement, wait for its
healthcheck, roll back to the old container if it fails, remove the old one on success.
It writes the outcome to <data dir>/self-update.json; the Updock that comes up (new, or the
old one after a rollback) reads it, logs it and sends a `self-update` notification.

Off unless UPDOCK_ALLOW_SELF_UPDATE=true."""
import json
import logging
import os
import sys
import threading
import time

from .updater import SELF_LABEL, build_create_body

log = logging.getLogger("updock")

HELPER_SUFFIX = "-self-update"
HELPER_LABEL = "updock.self-helper"
RESULT_FILE = "self-update.json"
STALE_AFTER = 15 * 60  # a "running" result older than this: the helper died


class SelfUpdateError(RuntimeError):
    pass


def _read(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write(path, data):
    if not path:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not write %s: %s", path, e, extra={"event": "self-update"})


class SelfUpdater:
    def __init__(self, docker, updater, notifier, data_dir=None, version="dev", allowed=False,
                 self_ref=None, clock=time.time, sleep=time.sleep):
        self.d, self.up, self.notifier = docker, updater, notifier
        self.version, self.allowed = version, allowed
        self.self_ref = self_ref or os.getenv("UPDOCK_SELF") or os.getenv("HOSTNAME", "")
        self.path = os.path.join(data_dir, RESULT_FILE) if data_dir else None
        self.clock, self.sleep = clock, sleep
        self.in_progress = False
        self.error = None

    def container(self):
        """Updock's own container (inspect), or None if it can't be identified."""
        if not self.self_ref:
            return None
        try:
            ctr = self.d.inspect(self.self_ref)
        except Exception:
            return None
        if (ctr["Config"].get("Labels") or {}).get(SELF_LABEL) != "true":
            return None
        return ctr

    def result(self):
        return _read(self.path) if self.path else None

    def status(self):
        ctr = self.container()
        out = {"allowed": self.allowed, "version": self.version, "in_progress": self.in_progress,
               "error": self.error, "result": self.result(), "found": ctr is not None}
        if ctr is None:
            out["reason"] = ("can't find Updock's own container; set UPDOCK_SELF to its name "
                             "if you changed its hostname")
            return out
        name = ctr["Name"].lstrip("/")
        st = self.up.state.get(name, {})
        out.update(name=name, image=ctr["Config"]["Image"],
                   update_available=bool(st.get("update_available")),
                   latest=st.get("latest"), last_check=st.get("last_check"),
                   job=st.get("job"), message=st.get("message"))
        return out

    def check(self):
        ctr = self.container()
        if ctr is None:
            raise SelfUpdateError("can't find Updock's own container")
        name = ctr["Name"].lstrip("/")
        with self.up.locks[name]:
            _, avail = self.up.check(name)
        return avail

    def _busy(self):
        if self.in_progress:
            return "a self-update is already in progress"
        if self.up.run:
            return "a run is in progress; try again when it has finished"
        busy = [n for n, lock in self.up.locks.items() if lock.locked()]
        if busy:
            return f"jobs are running ({', '.join(sorted(busy)[:5])}); try again shortly"
        return None

    def preflight(self):
        """Updock's container if a self-update may start now; else SelfUpdateError."""
        if not self.allowed:
            raise SelfUpdateError("self-update is disabled (set UPDOCK_ALLOW_SELF_UPDATE=true)")
        why = self._busy()
        if why:
            raise SelfUpdateError(why)
        ctr = self.container()
        if ctr is None:
            raise SelfUpdateError("can't find Updock's own container; set UPDOCK_SELF")
        return ctr

    def start(self):
        """Pull the new image and start the helper. Returns the helper's name."""
        ctr = self.preflight()
        self.in_progress, self.error = True, None
        try:
            return self._start(ctr)
        except Exception:
            self.in_progress = False
            raise

    def start_safely(self):
        """start() for a background thread: errors are logged and shown in status()."""
        try:
            return self.start()
        except Exception as e:
            self.error = str(e)
            log.error("self-update not started: %s", e, extra={"event": "self-update"})
            return None

    def _start(self, ctr):
        name = ctr["Name"].lstrip("/")
        with self.up.locks[name]:
            ctr, avail = self.up.check(name)
            target = self.up.state[name].get("target_ref") or ctr["Config"]["Image"]
            if avail and name in self.up._unpulled:
                self.up._pull_guarded(name, ctr, target)
        new_id = self.d.image(target)["Id"]
        if new_id == ctr["Image"]:
            raise SelfUpdateError("Updock is already running the newest image")
        helper = name + HELPER_SUFFIX
        try:
            self.d.remove(helper)  # a leftover from an earlier attempt
        except Exception:
            pass
        body, extra = self.helper_body(ctr, new_id, target)
        _write(self.path, {"status": "running", "from_version": self.version,
                           "from_image": ctr["Image"], "to_image": new_id, "ref": target,
                           "name": name, "started": self.clock(), "reported": False})
        hid = self.d.create(helper, body)
        for net, cfg in extra:
            self.d.connect(net, hid, cfg)
        self.d.start(hid)
        log.warning("self-update started: %s -> %s (helper %s)", ctr["Image"][7:19],
                    new_id[7:19], helper, extra={"container": name, "event": "self-update"})
        return helper

    def helper_body(self, ctr, new_id, ref):
        """Create body for the helper: Updock's own config (Docker access, data volume,
        secrets, env, networks) from the new image, minus ports, restart policy, healthcheck
        and network aliases, running the self-update entry point."""
        try:
            img_cfg = self.d.image(ctr["Image"]).get("Config")
        except Exception:
            img_cfg = None
        body, extra = build_create_body(ctr, img_cfg, new_id)
        labels = {k: v for k, v in (body.get("Labels") or {}).items()
                  if not k.startswith("com.docker.compose.")}
        body["Labels"] = {**labels, SELF_LABEL: "true", HELPER_LABEL: "true",
                          "updock.enable": "false"}
        body["Entrypoint"] = None
        body["Cmd"] = ["python", "-m", "app.selfupdate", ctr["Id"], ref]
        body["Healthcheck"] = {"Test": ["NONE"]}
        body.pop("ExposedPorts", None)
        hc = body["HostConfig"]
        for k in ("PortBindings", "PublishAllPorts"):
            hc.pop(k, None)
        hc["RestartPolicy"] = {"Name": "no"}
        hc["AutoRemove"] = True
        for ep in ((body.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}).values():
            ep.pop("Aliases", None)
        extra = [(net, {k: v for k, v in cfg.items() if k != "Aliases"}) for net, cfg in extra]
        return body, extra

    # ---- after a restart: report what the helper did ----
    def report(self, wait=0, poll=5):
        """Log and notify the outcome of a finished self-update (once). Waits up to `wait`
        seconds for a helper that is still verifying the new container."""
        deadline = self.clock() + wait
        while True:
            res = self.result()
            if not res or res.get("reported"):
                return None
            if res.get("status") != "running":
                break
            if self.clock() - res.get("started", 0) > STALE_AFTER:
                res.update(status="failed", error="the self-update helper stopped without "
                                                  "reporting a result")
                break
            if self.clock() >= deadline:
                return None
            self.sleep(poll)
        status = res["status"]
        if status == "ok":
            msg = (f"Updock updated {res.get('from_version') or '?'} -> "
                   f"{res.get('to_version') or self.version}")
            level = logging.WARNING
        elif status == "rolled-back":
            msg = f"Updock self-update rolled back: {res.get('error') or 'unknown error'}"
            level = logging.ERROR
        else:
            msg = f"Updock self-update failed: {res.get('error') or 'unknown error'}"
            level = logging.ERROR
        log.log(level, msg, extra={"container": res.get("name"), "event": "self-update"})
        for line in res.get("log") or []:
            log.info("helper: %s", line, extra={"container": res.get("name"),
                                                 "event": "self-update"})
        self.notifier.notify("self-update", res.get("name"), msg,
                             {"reason": res.get("error")} if status != "ok" else {})
        res["reported"] = True
        _write(self.path, res)
        return status

    def pending(self):
        """A self-update result that hasn't been reported yet?"""
        res = self.result()
        return bool(res and not res.get("reported"))

    def report_in_background(self, wait):
        threading.Thread(target=self._report_safely, args=(wait,), daemon=True).start()

    def _report_safely(self, wait):
        try:
            self.report(wait)
        except Exception as e:
            log.error("could not report the self-update result: %s", e,
                      extra={"event": "self-update"})


# ---- the helper's entry point: python -m app.selfupdate <container id> <image ref> ----

class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage()[:300])
        self.lines[:] = self.lines[-40:]


def run_helper(argv, docker=None, updater_cls=None, env=None):
    from .docker_api import Docker
    from .updater import Updater, UnhealthyError, image_info
    env = os.environ if env is None else env
    if len(argv) != 2:
        print("usage: python -m app.selfupdate <container> <image ref>", file=sys.stderr)
        return 2
    target, ref = argv
    collect = _Collect()
    log.addHandler(collect)
    log.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, _Collect)
               for h in log.handlers):
        log.addHandler(logging.StreamHandler(sys.stdout))
    d = docker or Docker()
    path = os.path.join(env.get("UPDOCK_DATA_DIR", "/data"), RESULT_FILE)
    res = _read(path) or {}
    as_bool = lambda k, dflt: str(env.get(k, dflt)).lower() in ("1", "true", "yes")
    up = (updater_cls or Updater)(
        d, cleanup=as_bool("UPDOCK_CLEANUP", True),
        health_timeout=int(env.get("UPDOCK_HEALTH_TIMEOUT", "120")),
        startup_grace=int(env.get("UPDOCK_STARTUP_GRACE", "10")))
    up.self_id = ""
    try:
        ctr = d.inspect(target)
        new_id = up._recreate(ctr, image_ref=ref, cleanup=True)
        info = image_info(d.image(d.inspect(new_id)["Image"]))
        res.update(status="ok", to_version=info.get("version"), error=None)
        code = 0
    except UnhealthyError as e:
        res.update(status="rolled-back", error=str(e))
        code = 1
    except Exception as e:
        res.update(status="failed", error=f"{type(e).__name__}: {e}")
        code = 1
    res.update(finished=time.time(), log=collect.lines, reported=False)
    _write(path, res)
    log.removeHandler(collect)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised through run_helper
    sys.exit(run_helper(sys.argv[1:]))
