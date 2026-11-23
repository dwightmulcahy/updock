"""Browser tests: the real app and UI, driven by Playwright, against an in-memory Docker
and registry (no daemon, no network). Run: pip install -r requirements-e2e.txt &&
playwright install chromium && python -m pytest e2e -p no:cacheprovider"""
import importlib.util
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update(UPDOCK_DATA_DIR=tempfile.mkdtemp(prefix="updock-e2e-"), UPDOCK_INTERVAL="0")

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import main  # noqa: E402
from app.registry import RegistryAuthError  # noqa: E402
from app.updater import Updater  # noqa: E402

# the unit tests' in-memory Docker (tests/conftest.py; imported by path: same module name)
_spec = importlib.util.spec_from_file_location("unit_fakes", ROOT / "tests" / "conftest.py")
fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fakes)
IMG_CFG, NEW_IMG, OLD_IMG = fakes.IMG_CFG, fakes.NEW_IMG, fakes.OLD_IMG
FakeDocker, make_ctr = fakes.FakeDocker, fakes.make_ctr


class E2ERegistry:
    """web has a newer image, db is current, private needs a login."""

    def digest(self, ref):
        if ref.startswith("ghcr.io/"):
            raise RegistryAuthError("ghcr.io", "me/private")
        return "sha256:db" if ref.startswith("test/db") else "sha256:newer"

    def remote_info(self, ref, platform=None):
        return {"id": "new", "digest": "sha256:newer", "tags": [], "version": "2.0.0",
                "revision": None, "created": "2026-11-01T00:00:00Z"}

    def tags(self, ref):
        return ["2.0.0", "1.0.0", "latest"]


def build_docker():
    d = FakeDocker()
    d.images[OLD_IMG] = {"Id": OLD_IMG, "Config": {**IMG_CFG, "Labels": {
        "org.opencontainers.image.version": "1.0.0"}}, "RepoDigests": ["test/app@sha256:old"]}
    d.images[NEW_IMG] = {"Id": NEW_IMG, "Config": IMG_CFG}
    d.images["sha256:db"] = {"Id": "sha256:db", "Config": IMG_CFG,
                             "RepoDigests": ["test/db@sha256:db"]}
    d.images["sha256:priv"] = {"Id": "sha256:priv", "Config": IMG_CFG}
    proj = {"com.docker.compose.project": "shop"}
    web = make_ctr("web", cid="w" * 64)
    web["Config"]["Labels"] = {**proj, "com.docker.compose.service": "web",
                               "com.docker.compose.depends_on": "db:service_started"}
    db = make_ctr("db", image="test/db:16", image_id="sha256:db", cid="d" * 64)
    db["Config"]["Labels"] = {**proj, "com.docker.compose.service": "db"}
    priv = make_ctr("private", image="ghcr.io/me/private:latest", image_id="sha256:priv",
                    cid="p" * 64)
    old = make_ctr("old-tool", image="test/db:16", image_id="sha256:db", cid="o" * 64)
    old["State"] = {"Running": False}
    for c in (web, db, priv, old):
        d.add(c)
    d.tags["test/app:latest"] = NEW_IMG  # what a pull of web's tag returns
    return d


@pytest.fixture(scope="session")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


@pytest.fixture
def up(monkeypatch):
    """A fresh Updater on fake Docker for every test, wired into the running app."""
    u = Updater(build_docker(), registry=E2ERegistry(), logbuf=main.LOGBUF, startup_grace=0,
                include_stopped=True)
    u.on_change, u.on_run = main.up.on_change, main.up.on_run
    monkeypatch.setattr(main, "up", u)
    main.LOGBUF.entries.clear()
    return u


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        exe = os.getenv("E2E_CHROME")  # use a local Chrome instead of Playwright's
        b = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, server, up):
    ctx = browser.new_context(viewport={"width": 1200, "height": 900})
    p = ctx.new_page()
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.goto(server)
    p.wait_for_selector("tr.row")
    yield p
    ctx.close()
    assert not errors, f"JavaScript errors: {errors}"
