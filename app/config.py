"""Settings that may come from files (Docker/Compose secrets) instead of plain env vars."""
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
