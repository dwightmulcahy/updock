import base64
import re
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

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
             logbuf=LOGBUF)
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
