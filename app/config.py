"""Settings that may come from files (Docker/Compose secrets) instead of plain env vars."""
import difflib
import logging
import os

SECRET_NAMES = ("UPDOCK_PASSWORD", "UPDOCK_API_TOKEN", "UPDOCK_SMTP_PASSWORD")


class ConfigError(RuntimeError):
    pass


def secret(name, env=None, default=""):
    """`NAME_FILE=/run/secrets/x` wins over `NAME=value`. The file's contents are used with
    surrounding whitespace removed; an empty file means "not set". A `_FILE` that points to
    a missing or unreadable file is an error, so a typo can't silently disable a password."""
    env = os.environ if env is None else env
    path = (env.get(f"{name}_FILE") or "").strip()
    if path:
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError as e:
            raise ConfigError(f"{name}_FILE={path}: {e.strerror or e}") from None
    return env.get(name, default)


def resolve_secrets(env=None):
    """Copy of `env` with every supported secret resolved from its `_FILE` (if any)."""
    env = dict(os.environ if env is None else env)
    for name in SECRET_NAMES:
        env[name] = secret(name, env)
    return env


def load_env():
    """resolve_secrets() for startup: a bad secret file stops Updock with a clear message."""
    try:
        return resolve_secrets()
    except ConfigError as e:
        raise SystemExit(f"updock: cannot start: {e}") from None


# Every UPDOCK_* setting Updock reads (tests keep this in sync with the code). Anything else
# with the prefix is almost certainly a typo, which would otherwise be ignored silently.
KNOWN = frozenset((
    "UPDOCK_ALLOW", "UPDOCK_ALLOWED_ORIGINS", "UPDOCK_ALLOW_DELETE",
    "UPDOCK_ALLOW_SELF_UPDATE", "UPDOCK_API_TOKEN", "UPDOCK_AUTH_HEADER",
    "UPDOCK_AUTH_USERS", "UPDOCK_AUTO_UPDATE", "UPDOCK_CLEANUP", "UPDOCK_COOLDOWN",
    "UPDOCK_COSIGN", "UPDOCK_DATA_DIR", "UPDOCK_DIGEST_CHECK", "UPDOCK_DISK_MAX",
    "UPDOCK_DISK_WARN", "UPDOCK_EXCLUDE", "UPDOCK_EXCLUDE_IMAGES", "UPDOCK_HEALTH_HISTORY",
    "UPDOCK_HEALTH_INTERVAL", "UPDOCK_HEALTH_NOTIFY_AFTER", "UPDOCK_HEALTH_RESTART_AFTER",
    "UPDOCK_HEALTH_RESTART_ATTEMPTS", "UPDOCK_HEALTH_TIMEOUT", "UPDOCK_HOOKS",
    "UPDOCK_INCLUDE_IMAGES", "UPDOCK_INCLUDE_STOPPED", "UPDOCK_INSECURE_REGISTRIES",
    "UPDOCK_INSTANCE", "UPDOCK_INTERVAL", "UPDOCK_JITTER", "UPDOCK_LABEL_ENABLE",
    "UPDOCK_LOG_BUFFER", "UPDOCK_LOG_FORMAT", "UPDOCK_LOG_LEVEL", "UPDOCK_METRICS_PUBLIC",
    "UPDOCK_NOTIFY_EVENTS", "UPDOCK_PASSWORD", "UPDOCK_PUBLIC_API", "UPDOCK_REGISTRY_CA",
    "UPDOCK_REVIVE_STOPPED", "UPDOCK_RUN_ON_START", "UPDOCK_SCHEDULE", "UPDOCK_SCOPE",
    "UPDOCK_SELF", "UPDOCK_SMTP_FROM", "UPDOCK_SMTP_HOST", "UPDOCK_SMTP_PASSWORD",
    "UPDOCK_SMTP_PORT", "UPDOCK_SMTP_SECURITY", "UPDOCK_SMTP_TO", "UPDOCK_SMTP_USER",
    "UPDOCK_STARTUP_GRACE", "UPDOCK_TRUSTED_PROXIES", "UPDOCK_URL", "UPDOCK_USER",
    "UPDOCK_WINDOW",
)) | {f"{n}_FILE" for n in SECRET_NAMES}


def clean_env(env=None):
    """Strip stray whitespace (a Windows line ending, trailing spaces) from UPDOCK_* values
    in place, and return the unknown UPDOCK_* names as [(name, closest known or None)]."""
    env = os.environ if env is None else env
    unknown = []
    for name in sorted(k for k in env if k.startswith("UPDOCK_")):
        value = env[name].strip()
        if value != env[name]:
            env[name] = value
        if name not in KNOWN:
            # compare without the shared prefix, which would make every name look close
            close = difflib.get_close_matches(name[7:], [k[7:] for k in KNOWN], n=1, cutoff=0.6)
            unknown.append((name, f"UPDOCK_{close[0]}" if close else None))
    return unknown


def warn_unknown(unknown, log=None):
    """One startup warning per unknown UPDOCK_* setting."""
    log = log or logging.getLogger("updock")
    for name, close in unknown:
        hint = f"; did you mean {close}?" if close else ""
        log.warning("unknown setting %s is ignored%s", name, hint, extra={"event": "config"})
