import base64
import random
import re
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, Response,
                               StreamingResponse)

from . import metrics as metricsmod
from .config import load_env
from .docker_api import Docker
from .events import EventHub
from . import events as eventsmod
from .notify import EmailNotifier
from .registry import Registry
from .schedule import Cron, Window, parse_duration
from .store import Store
from .units import parse_size, parse_threshold
from .updater import Updater

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

def init_store(directory, buf):
    """Open the data dir; replay saved logs into the buffer and persist new ones."""
    store = Store.open(directory)
    if store:
        buf.load(store.load_logs(buf.entries.maxlen))
        buf.listeners.append(store.append_log)
    return store


STORE = init_store(os.getenv("UPDOCK_DATA_DIR", "/data"), LOGBUF)
LOG_COUNTS = {}
LOGBUF.listeners.append(lambda e: LOG_COUNTS.__setitem__(e["level"], LOG_COUNTS.get(e["level"], 0) + 1))
METRICS_PUBLIC = env_bool("UPDOCK_METRICS_PUBLIC", False)
API_TOKEN = ENV["UPDOCK_API_TOKEN"].strip()
HUB = EventHub()
LOGBUF.listeners.append(lambda e: HUB.publish("log", e))
PROBES = {"/health", "/livez", "/readyz", "/startupz"}
STARTED = {"done": False}
DISK_MAX = parse_size(os.environ["UPDOCK_DISK_MAX"]) if os.getenv("UPDOCK_DISK_MAX") else 0
DISK_WARN = parse_threshold(os.environ["UPDOCK_DISK_WARN"], DISK_MAX) \
    if os.getenv("UPDOCK_DISK_WARN") else 0
NOTIFIER = EmailNotifier.from_env(ENV)
up = Updater(Docker(), cleanup=env_bool("UPDOCK_CLEANUP", True),
             label_enable=env_bool("UPDOCK_LABEL_ENABLE", False), registry=Registry(),
             logbuf=LOGBUF, health_timeout=int(os.getenv("UPDOCK_HEALTH_TIMEOUT", "120")),
             startup_grace=int(os.getenv("UPDOCK_STARTUP_GRACE", "10")),
             digest_check=env_bool("UPDOCK_DIGEST_CHECK", True), notifier=NOTIFIER, store=STORE,
             include_stopped=env_bool("UPDOCK_INCLUDE_STOPPED", False),
             cooldown=parse_duration(os.getenv("UPDOCK_COOLDOWN", "0")),
             hooks=env_bool("UPDOCK_HOOKS", False), disk_max=DISK_MAX, disk_warn=DISK_WARN,
             exclude_names=env_list("UPDOCK_EXCLUDE"),
             include_images=env_list("UPDOCK_INCLUDE_IMAGES"),
             exclude_images=env_list("UPDOCK_EXCLUDE_IMAGES"),
             scope=os.getenv("UPDOCK_SCOPE", "").strip() or None)
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


app = FastAPI(lifespan=lifespan)


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


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    path = request.url.path
    public = path in PROBES or (METRICS_PUBLIC and path == "/metrics")
    h = request.headers.get("authorization", "")
    if h.startswith("Bearer ") and (path.startswith("/api/") or path == "/metrics"):
        # API token for scripts, CI webhooks and Prometheus: never falls back to Basic
        if not (API_TOKEN and secrets.compare_digest(h[7:].strip(), API_TOKEN)):
            return JSONResponse({"detail": "invalid API token"}, status_code=401)
        return await call_next(request)
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


@app.middleware("http")
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


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION, "commit": COMMIT}


@app.get("/livez")
def livez():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        ok = up.d.ping()
    except Exception:
        ok = False
    if not ok:
        return JSONResponse({"status": "unavailable", "docker": False}, status_code=503)
    return {"status": "ok", "docker": True}


@app.get("/startupz")
def startupz():
    if not STARTED["done"]:
        return JSONResponse({"status": "starting"}, status_code=503)
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(metricsmod.render(up, up.list(), VERSION, COMMIT, LOG_COUNTS),
                             media_type="text/plain; version=0.0.4")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/containers")
def containers():
    return {"containers": up.list(), "next_run": next_run["t"], "auto_update": AUTO_UPDATE,
            "schedule": schedule_info(),
            "version": VERSION}


def _log_query(level, container, event, q, after, since, limit):
    return LOGBUF.query(level=level, container=container, event=event, q=q, after=after,
                        since=since, limit=min(max(limit, 0), 5000))


@app.get("/api/logs")
def logs(level: str | None = None, container: str | None = None, event: str | None = None,
         q: str | None = None, after: int | None = None, since: float | None = None,
         limit: int = 500):
    return {"entries": _log_query(level, container, event, q, after, since, limit),
            **LOGBUF.facets()}


@app.get("/api/logs/download")
def logs_download(level: str | None = None, container: str | None = None,
                  event: str | None = None, q: str | None = None, fmt: str = "text"):
    entries = _log_query(level, container, event, q, None, None, 0)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        return JSONResponse(entries, headers={
            "Content-Disposition": f'attachment; filename="updock-logs-{stamp}.json"'})
    return PlainTextResponse("\n".join(logmod.format_text(e) for e in entries) + "\n", headers={
        "Content-Disposition": f'attachment; filename="updock-logs-{stamp}.log"'})


@app.post("/api/containers/{name}/check")
def check(name: str):
    return bg(up.check_only, name)


@app.post("/api/containers/{name}/update")
def update(name: str, force: bool = False):
    return bg(up.update, name, force)


@app.get("/api/containers/{name}/tags")
def tags(name: str):
    try:
        return up.tags(name)
    except Exception as e:
        raise HTTPException(502, f"could not list tags: {e}")


@app.get("/api/containers/{name}/tags/{tag}")
def tag_info(name: str, tag: str):
    if not TAG_RE.match(tag):
        raise HTTPException(400, "invalid tag")
    try:
        return up.tag_info(name, tag)
    except Exception as e:
        raise HTTPException(502, f"could not read {tag}: {e}")


@app.post("/api/containers/{name}/pin")
def pin(name: str, pinned: bool = True):
    try:
        up.pin(name, pinned)
    except Exception as e:
        raise HTTPException(404, str(e))
    return {"name": name, "pinned": pinned}


@app.post("/api/containers/{name}/deploy")
def deploy(name: str, tag: str):
    if not TAG_RE.match(tag):
        raise HTTPException(400, "invalid tag")
    return bg(up.deploy, name, tag)


@app.get("/api/disk")
def disk():
    try:
        return up.disk_status(max_age=60)
    except Exception as e:
        raise HTTPException(502, f"could not read disk usage: {e}")


@app.get("/api/events")
async def events(request: Request):
    q = HUB.subscribe()
    return StreamingResponse(eventsmod.stream(HUB, q, request.is_disconnected),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/notify")
def notify_info():
    return NOTIFIER.describe()


@app.post("/api/notify/test")
def notify_test():
    try:
        NOTIFIER.test()
    except Exception as e:
        raise HTTPException(502, f"test email failed: {e}")
    return {"sent": True, "to": NOTIFIER.to}


@app.post("/api/check-all")
def check_all():
    return bg(up.run_all, False)


@app.post("/api/update-all")
def update_all():
    return bg(up.run_all, True)
