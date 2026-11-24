import base64
from collections import defaultdict, deque
import inspect
import ipaddress
import json
import random
import re
import logging
import os
import platform
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, JSONResponse, PlainTextResponse, Response,
                                 StreamingResponse)
from starlette.routing import Route

from . import metrics as metricsmod
from .config import load_env
from . import docker_api
from .docker_api import Docker, normalize_registry
from .logins import RegistryLogins, display_host
from .registry import RegistryAuthError
from .events import EventHub
from . import events as eventsmod
from .notify import EmailNotifier
from .registry import Registry
from .schedule import Cron, Window, parse_duration
from .store import Store
from .units import parse_size, parse_threshold
from .updater import Updater, protected_message

from . import logs as logmod

LOGBUF = logmod.setup(os.getenv("UPDOCK_LOG_LEVEL", "INFO"), os.getenv("UPDOCK_LOG_FORMAT", "text"),
                      int(os.getenv("UPDOCK_LOG_BUFFER", "2000")))

env_bool = lambda k, d: os.getenv(k, str(d)).lower() in ("1", "true", "yes")
env_list = lambda k: [x.strip() for x in os.getenv(k, "").split(",") if x.strip()]
INTERVAL = int(os.getenv("UPDOCK_INTERVAL", "86400"))  # seconds, 0 = UI only
AUTO_UPDATE = env_bool("UPDOCK_AUTO_UPDATE", True)  # false = scheduled check only


def _tz(name):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(name) if name else timezone.utc
    except Exception:
        logging.getLogger("updock").warning("unknown TZ %r, using UTC", name)
        return timezone.utc


TZ = _tz(os.getenv("TZ", "").strip())
CRON = Cron(os.environ["UPDOCK_SCHEDULE"]) if os.getenv("UPDOCK_SCHEDULE", "").strip() else None
WINDOW = Window(os.environ["UPDOCK_WINDOW"]) if os.getenv("UPDOCK_WINDOW", "").strip() else None
JITTER = parse_duration(os.getenv("UPDOCK_JITTER", "1m"))  # spread runs off the exact minute
UI_USER = os.getenv("UPDOCK_USER", "admin")
ENV = load_env()  # UPDOCK_*_FILE -> value (Compose secrets in ./secrets)
UI_PASS = ENV["UPDOCK_PASSWORD"]
VERSION = os.getenv("APP_VERSION") or "dev"
COMMIT = os.getenv("GIT_SHA", "")[:7]
BUILD_DATE = os.getenv("BUILD_DATE", "") or None
STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def init_store(directory, buf):
    """Open the data dir; replay saved logs into the buffer and persist new ones."""
    store = Store.open(directory)
    if store:
        buf.load(store.load_logs(buf.entries.maxlen))
        buf.listeners.append(store.append_log)
    return store


STORE = init_store(os.getenv("UPDOCK_DATA_DIR", "/data"), LOGBUF)
LOGINS = RegistryLogins(os.path.join(STORE.dir, "registries.json") if STORE else None)
docker_api.CREDENTIAL_PROVIDERS.append(LOGINS.get)
LOG_COUNTS = {}
LOGBUF.listeners.append(lambda e: LOG_COUNTS.__setitem__(e["level"], LOG_COUNTS.get(e["level"], 0) + 1))
METRICS_PUBLIC = env_bool("UPDOCK_METRICS_PUBLIC", False)
API_TOKEN = ENV["UPDOCK_API_TOKEN"].strip()


def parse_networks(value):
    """'10.0.0.0/8, 172.18.0.5' -> [ip_network, ...]; bad entries are logged and skipped."""
    out = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logging.getLogger("updock").error("UPDOCK_TRUSTED_PROXIES: ignoring invalid "
                                              "entry %r", item, extra={"event": "security"})
    return out


AUTH_HEADER = os.getenv("UPDOCK_AUTH_HEADER", "").strip()
TRUSTED_PROXIES = parse_networks(os.getenv("UPDOCK_TRUSTED_PROXIES", ""))
AUTH_USERS = {u.strip() for u in os.getenv("UPDOCK_AUTH_USERS", "").split(",") if u.strip()}


def check_auth_config():
    if AUTH_HEADER and not TRUSTED_PROXIES:
        logging.getLogger("updock").error(
            "UPDOCK_AUTH_HEADER is set but UPDOCK_TRUSTED_PROXIES is empty: the header is "
            "ignored (anyone could send it); set the reverse proxy's address",
            extra={"event": "security"})


check_auth_config()


def from_trusted_proxy(request):
    try:
        ip = ipaddress.ip_address((request.client.host if request.client else "") or "")
    except ValueError:
        return False
    return any(ip in net for net in TRUSTED_PROXIES)


def proxy_user(request):
    """(handled, user): with header auth on, requests from a trusted proxy are decided by
    the header alone. handled=False means 'not from the proxy: use the other checks'."""
    if not (AUTH_HEADER and TRUSTED_PROXIES) or not from_trusted_proxy(request):
        return False, None
    user = request.headers.get(AUTH_HEADER, "").strip()
    if user and (not AUTH_USERS or user in AUTH_USERS):
        return True, user
    return True, None
HUB = EventHub()
LOGBUF.listeners.append(lambda e: HUB.publish("log", e))
PROBES = {"/health", "/livez", "/readyz", "/startupz"}
STARTED = {"done": False}
DISK_MAX = parse_size(os.environ["UPDOCK_DISK_MAX"]) if os.getenv("UPDOCK_DISK_MAX") else 0
DISK_WARN = parse_threshold(os.environ["UPDOCK_DISK_WARN"], DISK_MAX) \
    if os.getenv("UPDOCK_DISK_WARN") else 0
NOTIFIER = EmailNotifier.from_env(ENV, version=VERSION)
up = Updater(Docker(), cleanup=env_bool("UPDOCK_CLEANUP", True),
             label_enable=env_bool("UPDOCK_LABEL_ENABLE", False),
             registry=Registry(insecure=env_list("UPDOCK_INSECURE_REGISTRIES"),
                               ca_file=os.getenv("UPDOCK_REGISTRY_CA", "").strip() or None),
             logbuf=LOGBUF, health_timeout=int(os.getenv("UPDOCK_HEALTH_TIMEOUT", "120")),
             startup_grace=int(os.getenv("UPDOCK_STARTUP_GRACE", "10")),
             digest_check=env_bool("UPDOCK_DIGEST_CHECK", True), notifier=NOTIFIER, store=STORE,
             include_stopped=env_bool("UPDOCK_INCLUDE_STOPPED", False),
             cooldown=parse_duration(os.getenv("UPDOCK_COOLDOWN", "0")),
             hooks=env_bool("UPDOCK_HOOKS", False), disk_max=DISK_MAX, disk_warn=DISK_WARN,
             exclude_names=env_list("UPDOCK_EXCLUDE"),
             include_images=env_list("UPDOCK_INCLUDE_IMAGES"),
             exclude_images=env_list("UPDOCK_EXCLUDE_IMAGES"),
             scope=os.getenv("UPDOCK_SCOPE", "").strip() or None,
             revive_stopped=env_bool("UPDOCK_REVIVE_STOPPED", False),
             cosign_data_dir=STORE.dir if STORE else None,
             allow=os.getenv("UPDOCK_ALLOW"))
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in os.getenv("UPDOCK_ALLOWED_ORIGINS", "").split(",")
                   if o.strip()}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "x-updock-request"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"),
}
up.on_change = lambda name, st: HUB.publish("state", {"name": name, **st})
up.on_run = lambda run: HUB.publish("run", {"run": run, "last_run": up.last_run})
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
next_run = {"t": None}


def next_due(now_ts):
    jitter = random.uniform(0, JITTER) if JITTER else 0
    if CRON:
        return CRON.next(datetime.fromtimestamp(now_ts, TZ)).timestamp() + jitter
    return now_ts + INTERVAL + jitter


def scheduled_run():
    update = AUTO_UPDATE
    if update and WINDOW and not WINDOW.contains(datetime.now(TZ)):
        logging.getLogger("updock").info(
            "outside maintenance window %s: checking only", WINDOW, extra={"event": "run"})
        update = False
    up.run_all(update)


def _safe_run():
    try:
        scheduled_run()
    except Exception as e:
        logging.getLogger("updock").exception("scheduled run failed: %s", e,
                                              extra={"event": "run"})


def scheduler():
    if env_bool("UPDOCK_RUN_ON_START", False):
        _safe_run()
    while True:
        due = next_due(time.time())
        next_run["t"] = due
        while (remaining := due - time.time()) > 0:
            time.sleep(min(remaining, 60))  # short naps survive clock changes / suspend
        _safe_run()


def schedule_info():
    return {"cron": str(CRON) if CRON else None, "interval": None if CRON else INTERVAL or None,
            "jitter": JITTER,
            "window": str(WINDOW) if WINDOW else None, "tz": str(TZ)}


@asynccontextmanager
async def lifespan(_):
    if CRON or INTERVAL > 0:
        logging.getLogger("updock").info(
            "scheduler: %s, %s%s", f"cron '{CRON}' ({TZ})" if CRON else f"every {INTERVAL}s",
            "auto-update" if AUTO_UPDATE else "check only",
            f", maintenance window {WINDOW}" if WINDOW else "", extra={"event": "run"})
        threading.Thread(target=scheduler, daemon=True).start()
    STARTED["done"] = True
    yield


# ---- a small web layer on Starlette (what FastAPI is built on) ----
# Endpoints take the request and return a dict/list (sent as JSON) or a Response.
# Plain `def` endpoints run in a worker thread (they talk to Docker); the request body is
# read first, so they can use `body()` without awaiting.
ROUTES = []


def route(path, method="GET"):
    def add(fn):
        ROUTES.append(Route(path, _endpoint(fn), methods=[method], name=fn.__name__))
        return fn
    return add


def _json_default(o):
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


class JSON(JSONResponse):
    def render(self, content):
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"),
                          default=_json_default).encode()


def _endpoint(fn):
    is_async = inspect.iscoroutinefunction(fn)

    async def endpoint(request):
        if request.method in UNSAFE_METHODS:
            request.state.raw_body = await request.body()
        out = await fn(request) if is_async else await run_in_threadpool(fn, request)
        return out if isinstance(out, Response) else JSON(out)
    return endpoint


def _bad(name, kind):
    raise HTTPException(422, f"query parameter {name!r} must be {kind}")


def q_str(request, name, default=None):
    return request.query_params.get(name, default)


def q_bool(request, name, default):
    v = request.query_params.get(name)
    if v is None:
        return default
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    _bad(name, "true or false")


def q_num(request, name, default, kind=int):
    v = request.query_params.get(name)
    if v is None or v == "":
        return default
    try:
        return kind(v)
    except ValueError:
        _bad(name, "an integer" if kind is int else "a number")


def q_required(request, name):
    v = request.query_params.get(name)
    if v is None:
        raise HTTPException(422, f"query parameter {name!r} is required")
    return v


def body(request, fields, optional=True):
    """Parse a JSON object body against {field: (type, default)}; default=... = required.
    Returns None for an empty body when `optional`."""
    raw = getattr(request.state, "raw_body", b"")
    if not raw.strip():
        if optional:
            return None
        raw = b"{}"
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "request body must be JSON")
    if not isinstance(data, dict):
        raise HTTPException(422, "request body must be a JSON object")
    out = {}
    for key, (kind, default) in fields.items():
        if key not in data or data[key] is None:
            if default is ...:
                raise HTTPException(422, f"{key!r} is required")
            out[key] = default
        elif type(data[key]) is not kind:  # no silent coercion (True is not an int, "1" not a bool)
            raise HTTPException(422, f"{key!r} must be a {kind.__name__}")
        else:
            out[key] = data[key]
    return out


async def _http_error(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


def csrf_violation(request):
    """Why a state-changing request looks cross-site, or None if it is acceptable.

    Browsers attach saved Basic-auth credentials to any request, including forms posted
    from other sites, so browser requests must prove they come from the Updock page.
    Non-browser clients (curl, CI webhooks) send neither Origin nor Sec-Fetch-Site and
    carry no ambient credentials, so they are not subject to CSRF."""
    if request.method not in UNSAFE_METHODS:
        return None
    h = request.headers
    origin, site = h.get("origin"), h.get("sec-fetch-site")
    if origin is None and site is None:
        return None
    if site is not None and site not in ("same-origin", "none"):
        return f"cross-site request (Sec-Fetch-Site: {site})"
    if origin is not None and origin.rstrip("/") not in ALLOWED_ORIGINS \
            and urlsplit(origin).netloc != h.get("host"):
        return f"origin {origin} not allowed"
    if h.get(CSRF_HEADER) != "1":
        return "missing X-Updock-Request header"
    return None


async def basic_auth(request: Request, call_next):
    path = request.url.path
    public = path in PROBES or (METRICS_PUBLIC and path == "/metrics")
    h = request.headers.get("authorization", "")
    if h.startswith("Bearer ") and (path.startswith("/api/") or path == "/metrics"):
        # API token for scripts, CI webhooks and Prometheus: never falls back to Basic
        if not (API_TOKEN and secrets.compare_digest(h[7:].strip(), API_TOKEN)):
            return JSONResponse({"detail": "invalid API token"}, status_code=401)
        request.state.token = True
        return await call_next(request)
    handled, user = proxy_user(request)
    if handled and not public:
        if not user:
            return JSONResponse({"detail": f"not signed in at the reverse proxy ({AUTH_HEADER} "
                                           "missing or not allowed)"}, status_code=401)
        request.state.user = user
        return await call_next(request)
    if AUTH_HEADER and TRUSTED_PROXIES and not UI_PASS and not public:
        # header auth is the only login: don't let direct requests skip the proxy
        return JSONResponse({"detail": "sign in through the reverse proxy"}, status_code=401)
    if UI_PASS and not public:
        ok = False
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode(errors="ignore").partition(":")
            except ValueError:
                u = p = ""
            ok = secrets.compare_digest(u, UI_USER) & secrets.compare_digest(p, UI_PASS)
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="updock"'})
    return await call_next(request)


async def security(request: Request, call_next):
    reason = csrf_violation(request)
    if reason:
        logging.getLogger("updock").warning(
            "blocked %s %s: %s", request.method, request.url.path, reason,
            extra={"event": "security"})
        resp = JSONResponse({"detail": f"Forbidden: {reason}"}, status_code=403)
    else:
        resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()
    return JSONResponse({"queued": True}, status_code=202)


@route("/health", "GET")
def health(request):
    return {"status": "ok", "version": VERSION, "commit": COMMIT}


@route("/livez", "GET")
def livez(request):
    return {"status": "ok"}


@route("/readyz", "GET")
def readyz(request):
    try:
        ok = up.d.ping()
    except Exception:
        ok = False
    if not ok:
        return JSONResponse({"status": "unavailable", "docker": False}, status_code=503)
    return {"status": "ok", "docker": True}


@route("/startupz", "GET")
def startupz(request):
    if not STARTED["done"]:
        return JSONResponse({"status": "starting"}, status_code=503)
    return {"status": "ok"}


@route("/metrics", "GET")
def metrics(request):
    return PlainTextResponse(metricsmod.render(up, up.list(), VERSION, COMMIT, LOG_COUNTS),
                             media_type="text/plain; version=0.0.4")


@route("/", "GET")
def index(request):
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@route("/api/containers", "GET")
def containers(request):
    return {"containers": up.list(), "next_run": next_run["t"], "auto_update": AUTO_UPDATE,
            "schedule": schedule_info(), "run": up.run_status(), "last_run": up.last_run,
            "version": VERSION, "user": getattr(request.state, "user", None),
            "cooldown_gate": cooldown_gate(), "allow_delete": ALLOW_DELETE}


def _log_query(level, container, event, q, after, since, limit):
    return LOGBUF.query(level=level, container=container, event=event, q=q, after=after,
                        since=since, limit=min(max(limit, 0), 5000))


@route("/api/logs", "GET")
def logs(request):
    level, container, event, q = (q_str(request, k) for k in ("level", "container", "event", "q"))
    after, since = q_num(request, "after", None), q_num(request, "since", None, float)
    limit = q_num(request, "limit", 500)
    return {"entries": _log_query(level, container, event, q, after, since, limit),
            **LOGBUF.facets()}


@route("/api/logs/download", "GET")
def logs_download(request):
    level, container, event, q = (q_str(request, k) for k in ("level", "container", "event", "q"))
    fmt = q_str(request, "fmt", "text")
    entries = _log_query(level, container, event, q, None, None, 0)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        return JSONResponse(entries, headers={
            "Content-Disposition": f'attachment; filename="updock-logs-{stamp}.json"'})
    return PlainTextResponse("\n".join(logmod.format_text(e) for e in entries) + "\n", headers={
        "Content-Disposition": f'attachment; filename="updock-logs-{stamp}.log"'})


@route("/api/containers/{name}/check", "POST")
def check(request):
    name = request.path_params["name"]
    return bg(up.check_only, name)


UPDATE_BODY = {"override_cooldown": (bool, False), "approve": (bool, False), "password": (str, "")}


def refuse_protected(name):
    if up.protected(name):
        raise HTTPException(409, protected_message(name))


DELETE_BODY = {"remove_image": (bool, False), "remove_volumes": (bool, False), "password": (str, "")}
LOGIN_BODY = {"host": (str, ...), "username": (str, ...), "password": (str, ...)}


ALLOW_DELETE = env_bool("UPDOCK_ALLOW_DELETE", True)


ADMIN_FAIL_LIMIT, ADMIN_FAIL_WINDOW = 5, 300
_admin_fails = defaultdict(deque)  # client address -> times of recent wrong passwords


def require_admin(request, password, name, action):
    """Gate for risky actions: the admin password (UPDOCK_PASSWORD, the Updock sign-in
    password) when one is set; an API token counts. Wrong passwords are logged and, after
    ADMIN_FAIL_LIMIT within ADMIN_FAIL_WINDOW seconds from one address, refused with 429."""
    if not UI_PASS or getattr(request.state, "token", False):
        return
    who = request.client.host if request.client else "?"
    fails, now = _admin_fails[who], time.monotonic()
    while fails and now - fails[0] > ADMIN_FAIL_WINDOW:
        fails.popleft()
    if len(fails) >= ADMIN_FAIL_LIMIT:
        wait = int(ADMIN_FAIL_WINDOW - (now - fails[0])) + 1
        raise HTTPException(429, f"too many wrong passwords; try again in {wait}s")
    if secrets.compare_digest((password or "").encode(), UI_PASS.encode()):
        fails.clear()
        return
    fails.append(now)
    logging.getLogger("updock").warning(
        "%s: %s refused: %s from %s (%d of %d tries; it's the Updock sign-in password, "
        "UPDOCK_PASSWORD)", name, action, "empty password" if not password else "wrong admin password",
        who, len(fails), ADMIN_FAIL_LIMIT, extra={"container": name, "event": "security"})
    raise HTTPException(403, "wrong admin password")


def cooldown_gate():
    """What the UI must ask before skipping a cooldown: the admin password when one is
    set, otherwise just a confirmation."""
    return "password" if UI_PASS else "confirm"


@route("/api/containers/{name}/version-policy", "POST")
def version_policy(request):
    name = request.path_params["name"]
    data = body(request, {"policy": (str, ...), "password": (str, "")}, optional=False)
    require_admin(request, data["password"], name, "version policy change")
    if data["policy"] not in ("inherit", "patch", "minor", "major"):
        raise HTTPException(422, "invalid version policy")
    try:
        up.d.inspect(name)
    except Exception as e:
        raise HTTPException(404, f"no container {name!r}: {e}")
    return up.set_version_policy(name, data["policy"])


@route("/api/containers/{name}/update", "POST")
def update(request):
    name, force = request.path_params["name"], q_bool(request, "force", False)
    data = body(request, UPDATE_BODY)
    refuse_protected(name)
    override = bool(data and data["override_cooldown"])
    approve = bool(data and data["approve"])
    if override or approve:
        require_admin(request, data["password"], name,
                      "version approval" if approve else "cooldown override")
    who = getattr(request.state, "user", None) or "admin"
    if approve:
        logging.getLogger("updock").warning("%s: version update approved by %s", name, who,
                                            extra={"container": name, "event": "approval"})
        return bg(up.update, name, force, False, override, True)
    if override:
        logging.getLogger("updock").warning("%s: cooldown skipped by %s", name, who,
                                            extra={"container": name, "event": "cooldown"})
        return bg(up.update, name, force, False, True)
    return bg(up.update, name, force)


@route("/api/containers/{name}/delete", "POST")
def delete(request):
    """Stop and remove a container (admin password required when one is set)."""
    name = request.path_params["name"]
    data = body(request, DELETE_BODY, optional=False)
    if not ALLOW_DELETE:
        raise HTTPException(403, "deleting containers is disabled (UPDOCK_ALLOW_DELETE=false)")
    require_admin(request, data["password"], name, "delete")
    refuse_protected(name)
    try:
        up.d.inspect(name)
    except Exception as e:
        raise HTTPException(404, f"no container {name!r}: {e}")
    logging.getLogger("updock").warning("%s: delete requested by %s", name,
                                        getattr(request.state, "user", None) or "admin",
                                        extra={"container": name, "event": "delete"})
    return bg(up.delete, name, data["remove_image"], data["remove_volumes"])


@route("/api/containers/{name}/tags", "GET")
def tags(request):
    name = request.path_params["name"]
    try:
        return up.tags(name)
    except Exception as e:
        raise HTTPException(502, f"could not list tags: {e}")


@route("/api/containers/{name}/tags/{tag}", "GET")
def tag_info(request):
    name, tag = request.path_params["name"], request.path_params["tag"]
    if not TAG_RE.match(tag):
        raise HTTPException(400, "invalid tag")
    try:
        return up.tag_info(name, tag)
    except Exception as e:
        raise HTTPException(502, f"could not read {tag}: {e}")


@route("/api/containers/{name}/pin", "POST")
def pin(request):
    name, pinned = request.path_params["name"], q_bool(request, "pinned", True)
    try:
        up.pin(name, pinned)
    except Exception as e:
        raise HTTPException(404, str(e))
    return {"name": name, "pinned": pinned}


@route("/api/containers/{name}/deploy", "POST")
def deploy(request):
    name, tag = request.path_params["name"], q_required(request, "tag")
    if not TAG_RE.match(tag):
        raise HTTPException(400, "invalid tag")
    refuse_protected(name)
    return bg(up.deploy, name, tag)


@route("/api/containers/{name}/rollback", "POST")
def rollback(request):
    name = request.path_params["name"]
    refuse_protected(name)
    return bg(up.rollback, name)


@route("/api/disk", "GET")
def disk(request):
    try:
        return up.disk_status(max_age=60)
    except Exception as e:
        raise HTTPException(502, f"could not read disk usage: {e}")


@route("/api/events", "GET")
async def events(request):
    q = HUB.subscribe()
    return StreamingResponse(eventsmod.stream(HUB, q, request.is_disconnected),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@route("/api/registries", "GET")
def registries(request):
    return {"logins": LOGINS.list(), "persisted": bool(LOGINS.path)}


@route("/api/registries", "POST")
def add_registry(request):
    login, verify = body(request, LOGIN_BODY, optional=False), q_bool(request, "verify", True)
    host, user, password = login["host"].strip(), login["username"].strip(), login["password"]
    if not host or not user or not password:
        raise HTTPException(400, "host, username and password are required")
    key = normalize_registry(host)
    if verify and up.registry:
        try:
            up.registry.verify_login(key, user, password)
        except RegistryAuthError:
            raise HTTPException(400, f"{display_host(key)} rejected this username/password")
        except Exception as e:
            raise HTTPException(502, f"could not verify the login with {display_host(key)}: {e}"
                                     " (save without checking to keep it anyway)")
    LOGINS.set(key, user, password)
    if up.registry:
        up.registry.forget(key)
    up._backoff.pop(key, None)
    names = [n for n, st in up.state.items() if st.get("job") == "auth-required"
             and normalize_registry(st.get("auth_host")) == key]
    bg(up.recheck_auth, key)
    return {"saved": True, "host": display_host(key), "rechecking": names}


@route("/api/registries/{host}", "DELETE")
def remove_registry(request):
    host = request.path_params["host"]
    key = normalize_registry(host)
    if not LOGINS.remove(key):
        raise HTTPException(404, f"no saved login for {display_host(key)}")
    if up.registry:
        up.registry.forget(key)
    return {"removed": display_host(key)}


def auth_mode():
    if AUTH_HEADER and TRUSTED_PROXIES:
        return f"reverse proxy ({AUTH_HEADER})" + (" + password" if UI_PASS else "")
    return "password" if UI_PASS else "none"


@route("/api/build-info", "GET")
def build_info(request):
    """Details for the dialog opened by clicking the version in the header."""
    sha = os.getenv("GIT_SHA") or None
    return {"version": VERSION, "commit": sha[:12] if sha else None, "build_date": BUILD_DATE,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}".strip(),
            "started_at": STARTED_AT}


CHANGELOG = Path(__file__).with_name("changelog.json")  # written by the release workflow


def _version_key(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split("-")[0].split("."))
    except ValueError:
        return None


@route("/api/changelog", "GET")
def changelog(request):
    """Release notes bundled into the image. `since=0.5.0`: only newer versions (for the
    "What's new" dialog after an upgrade); without it, the latest five."""
    try:
        data = json.loads(CHANGELOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"current": VERSION, "available": False, "repository": "", "releases": []}
    releases = data.get("releases") or []
    since = _version_key(q_str(request, "since") or "")
    if since:
        releases = [r for r in releases if (_version_key(r.get("version")) or ()) > since]
    else:
        releases = releases[:5]
    return {"current": VERSION, "available": True, "repository": data.get("repository", ""),
            "releases": releases}


@route("/api/about", "GET")
def about(request):
    """Build, engine and effective settings for the About tab (no secrets)."""
    try:
        v = up.d.version()
        engine = {"version": v.get("Version"), "api": v.get("ApiVersion"),
                  "os": v.get("Os"), "arch": v.get("Arch")}
    except Exception as e:
        engine = {"error": str(e)[:200]}
    host = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    return {
        "version": VERSION, "commit": COMMIT, "python": platform.python_version(),
        "engine": engine, "docker_host": host.split("://", 1)[0] + "://" + (
            host.split("://", 1)[1].split("@")[-1] if "://" in host else host),
        "schedule": schedule_info(), "auto_update": AUTO_UPDATE,
        "settings": {
            "cleanup": up.cleanup, "digest_check": up.digest_check,
            "label_enable": up.label_enable, "include_stopped": up.include_stopped,
            "revive_stopped": up.revive_stopped, "health_timeout": up.health_timeout,
            "startup_grace": up.startup_grace, "cooldown": up.cooldown, "hooks": up.hooks,
            "scope": up.scope, "allow": up.allow,
            "insecure_registries": sorted(getattr(up.registry, "insecure", ()) or ())},
        "notify": NOTIFIER.describe(), "auth": auth_mode(), "api_token": bool(API_TOKEN),
        "data_dir": STORE.dir if STORE else None, "registry_logins": len(LOGINS.list()),
    }


@route("/api/notify", "GET")
def notify_info(request):
    return NOTIFIER.describe()


@route("/api/notify/test", "POST")
def notify_test(request):
    try:
        NOTIFIER.test()
    except Exception as e:
        raise HTTPException(502, f"test email failed: {e}")
    return {"sent": True, "to": NOTIFIER.to}


@route("/api/check-all", "POST")
def check_all(request):
    if up.run:
        raise HTTPException(409, "a run is already in progress")
    return bg(up.run_all, False)


@route("/api/projects/{project}/{action}", "POST")
def project_run(request):
    """Check or update every container of one Compose project (dependencies first)."""
    project, action = request.path_params["project"], request.path_params["action"]
    if action not in ("check", "update"):
        raise HTTPException(404, "unknown action")
    if project not in up.projects():
        raise HTTPException(404, f"no Compose project {project!r}")
    if up.run:
        raise HTTPException(409, "a run is already in progress")
    return bg(up.run_all, action == "update", project)


@route("/api/update-all", "POST")
def update_all(request):
    if up.run:
        raise HTTPException(409, "a run is already in progress")
    return bg(up.run_all, True)


app = Starlette(
    routes=ROUTES, lifespan=lifespan,
    exception_handlers={HTTPException: _http_error},
    # first = outermost: security headers and CSRF checks wrap the authentication check
    middleware=[Middleware(BaseHTTPMiddleware, dispatch=security),
                Middleware(BaseHTTPMiddleware, dispatch=basic_auth)])
