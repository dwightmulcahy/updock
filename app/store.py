"""Persists container state and the log history in a data directory (a Docker volume)."""
import json
import logging
import os
import threading

log = logging.getLogger("updock")

TRANSIENT_JOBS = {"pulling", "checking", "updating"}


class Store:
    def __init__(self, directory, max_log_bytes=5_000_000, log_backups=2):
        self.dir = directory
        self.state_path = os.path.join(directory, "state.json")
        self.log_path = os.path.join(directory, "updock.log.jsonl")
        self.max_log_bytes, self.log_backups = max_log_bytes, log_backups
        self._state_lock, self._log_lock = threading.Lock(), threading.Lock()

    @classmethod
    def open(cls, directory, **kw):
        """Return a Store if `directory` is usable, else None (with a warning)."""
        try:
            os.makedirs(directory, exist_ok=True)
            probe = os.path.join(directory, ".write-test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            log.warning("state and logs will not survive restarts: %s is not writable (%s); "
                        "mount a volume there", directory, e, extra={"event": "store"})
            return None
        return cls(directory, **kw)

    # ---- state ----
    def load_state(self):
        try:
            with open(self.state_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            bad = self.state_path + ".corrupt"
            log.warning("could not read %s (%s); starting fresh, kept as %s", self.state_path, e,
                        bad, extra={"event": "store"})
            try:
                os.replace(self.state_path, bad)
            except OSError:
                pass
            return {}
        state = data.get("containers", {}) if isinstance(data, dict) else {}
        for st in state.values():
            if st.get("job") in TRANSIENT_JOBS:  # Updock stopped mid-job
                st["job"], st["message"] = "error", "interrupted by an Updock restart"
        return state

    def save_state(self, state):
        tmp = self.state_path + ".tmp"
        with self._state_lock:
            try:
                with open(tmp, "w") as f:
                    json.dump({"version": 1, "containers": state}, f, default=str)
                os.replace(tmp, self.state_path)
            except OSError as e:
                log.error("could not save state: %s", e, extra={"event": "store"})

    # ---- logs ----
    def _rotate(self):
        for i in range(self.log_backups - 1, 0, -1):
            src = f"{self.log_path}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{self.log_path}.{i + 1}")
        if self.log_backups:
            os.replace(self.log_path, f"{self.log_path}.1")
        else:
            os.remove(self.log_path)

    def append_log(self, entry):
        line = json.dumps(entry, default=str) + "\n"
        with self._log_lock:
            try:
                if os.path.exists(self.log_path) and \
                        os.path.getsize(self.log_path) + len(line) > self.max_log_bytes:
                    self._rotate()
                with open(self.log_path, "a") as f:
                    f.write(line)
            except OSError:
                pass  # never let logging fail; the stream handler still has it

    def load_logs(self, limit):
        """Most recent `limit` entries, oldest first, across rotated files."""
        files = [f"{self.log_path}.{i}" for i in range(self.log_backups, 0, -1)] + [self.log_path]
        out = []
        for path in files:
            try:
                with open(path) as f:
                    for line in f:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                continue
        return out[-limit:] if limit else out
