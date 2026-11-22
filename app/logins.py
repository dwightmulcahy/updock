"""Registry logins entered in the UI, kept in the data directory (mode 600)."""
import json
import logging
import os
import threading

from .docker_api import HUB_KEY, normalize_registry

log = logging.getLogger("updock")


def display_host(key):
    return "docker.io" if key == HUB_KEY else key


class RegistryLogins:
    def __init__(self, path=None):
        self.path = path  # None = keep in memory only (no writable data dir)
        self._lock = threading.Lock()
        self._logins = self._load()

    def _load(self):
        if not self.path:
            return {}
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.error("could not read registry logins from %s: %s", self.path, e,
                      extra={"event": "auth"})
            return {}
        return {normalize_registry(k): v for k, v in data.items()
                if isinstance(v, dict) and v.get("username") is not None}

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._logins, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, key):
        """Credential provider for docker_api: registry key -> (user, password) or None."""
        entry = self._logins.get(normalize_registry(key))
        return (entry["username"], entry.get("password", "")) if entry else None

    def list(self):
        return [{"host": display_host(k), "username": v["username"],
                 "persisted": bool(self.path)} for k, v in sorted(self._logins.items())]

    def set(self, host, username, password):
        key = normalize_registry(host)
        with self._lock:
            self._logins[key] = {"username": username, "password": password}
            self._save()
        log.info("saved registry login for %s (%s)", display_host(key), username,
                 extra={"event": "auth"})
        return key

    def remove(self, host):
        key = normalize_registry(host)
        with self._lock:
            found = self._logins.pop(key, None) is not None
            if found:
                self._save()
        if found:
            log.info("removed registry login for %s", display_host(key), extra={"event": "auth"})
        return found
