import base64
import re
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from .docker_api import Docker
from .registry import Registry
from .updater import Updater

from . import logs as logmod

LOGBUF = logmod.setup(os.getenv("UPDOCK_LOG_LEVEL", "INFO"), os.getenv("UPDOCK_LOG_FORMAT", "text"),
                      int(os.getenv("UPDOCK_LOG_BUFFER", "2000")))

env_bool = lambda k, d: os.getenv(k, str(d)).lower() in ("1", "true", "yes")
INTERVAL = int(os.getenv("UPDOCK_INTERVAL", "86400"))  # seconds, 0 = UI only
AUTO_UPDATE = env_bool("UPDOCK_AUTO_UPDATE", True)  # false = scheduled check only
UI_USER = os.getenv("UPDOCK_USER", "admin")
UI_PASS = os.getenv("UPDOCK_PASSWORD", "")
VERSION = os.getenv("APP_VERSION") or "dev"
COMMIT = os.getenv("GIT_SHA", "")[:7]

up = Updater(Docker(), cleanup=env_bool("UPDOCK_CLEANUP", True),
             label_enable=env_bool("UPDOCK_LABEL_ENABLE", False), registry=Registry(),
             logbuf=LOGBUF, health_timeout=int(os.getenv("UPDOCK_HEALTH_TIMEOUT", "120")),
             startup_grace=int(os.getenv("UPDOCK_STARTUP_GRACE", "10")),
             digest_check=env_bool("UPDOCK_DIGEST_CHECK", True))
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
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
next_run = {"t": None}


def scheduler():
    if env_bool("UPDOCK_RUN_ON_START", False):
        up.run_all(AUTO_UPDATE)
    while True:
        next_run["t"] = time.time() + INTERVAL
        time.sleep(INTERVAL)
        try:
            up.run_all(AUTO_UPDATE)
        except Exception as e:
            logging.getLogger("updock").exception("scheduled run failed: %s", e,
                                                  extra={"event": "run"})


@asynccontextmanager
async def lifespan(_):
    if INTERVAL > 0:
        threading.Thread(target=scheduler, daemon=True).start()
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
    if UI_PASS and request.url.path != "/health":
        ok = False
        h = request.headers.get("authorization", "")
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


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/containers")
def containers():
    return {"containers": up.list(), "next_run": next_run["t"], "auto_update": AUTO_UPDATE,
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


@app.post("/api/containers/{name}/deploy")
def deploy(name: str, tag: str):
    if not TAG_RE.match(tag):
        raise HTTPException(400, "invalid tag")
    return bg(up.deploy, name, tag)


@app.post("/api/check-all")
def check_all():
    return bg(up.run_all, False)


@app.post("/api/update-all")
def update_all():
    return bg(up.run_all, True)
