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
from fastapi.responses import FileResponse, JSONResponse, Response

from .docker_api import Docker
from .registry import Registry
from .updater import Updater

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

env_bool = lambda k, d: os.getenv(k, str(d)).lower() in ("1", "true", "yes")
INTERVAL = int(os.getenv("UPDOCK_INTERVAL", "86400"))  # seconds, 0 = UI only
AUTO_UPDATE = env_bool("UPDOCK_AUTO_UPDATE", True)  # false = scheduled check only
UI_USER = os.getenv("UPDOCK_USER", "admin")
UI_PASS = os.getenv("UPDOCK_PASSWORD", "")
VERSION = os.getenv("APP_VERSION") or "dev"
COMMIT = os.getenv("GIT_SHA", "")[:7]

up = Updater(Docker(), cleanup=env_bool("UPDOCK_CLEANUP", True),
             label_enable=env_bool("UPDOCK_LABEL_ENABLE", False), registry=Registry())
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
            logging.exception("run failed: %s", e)


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


@app.get("/api/logs")
def logs():
    return list(up.logs)[-200:]


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
