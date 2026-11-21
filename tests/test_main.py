import base64
import logging
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main


class SyncThread:
    """Runs the target inline so background jobs are deterministic."""
    started = []

    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        SyncThread.started.append(self.target)
        self.target(*self.args)


@pytest.fixture
def up(monkeypatch):
    m = MagicMock()
    m.list.return_value = [{"name": "web"}]
    monkeypatch.setattr(main, "up", m)
    monkeypatch.setattr(main.threading, "Thread", SyncThread)
    monkeypatch.setattr(main, "UI_PASS", "")
    monkeypatch.setattr(main, "INTERVAL", 0)
    SyncThread.started = []
    return m


@pytest.fixture
def client(up):
    with TestClient(main.app) as c:
        yield c


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200 and "Updock" in r.text


def test_containers(client):
    assert client.get("/api/containers").json() == {
        "containers": [{"name": "web"}], "next_run": None, "auto_update": True,
        "version": main.VERSION}


def test_health_bypasses_auth(client, monkeypatch):
    monkeypatch.setattr(main, "UI_PASS", "secret")
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.fixture
def buf(monkeypatch):
    from app.logs import LogBuffer
    b = LogBuffer()
    lg = logging.getLogger("t")
    lg.addHandler(b)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.info("hello", extra={"container": "web", "event": "check"})
    lg.warning("careful", extra={"container": "db", "event": "pull"})
    lg.error("broken thing")
    monkeypatch.setattr(main, "LOGBUF", b)
    yield b
    lg.removeHandler(b)


def test_logs_filters_and_facets(client, buf):
    d = client.get("/api/logs").json()
    assert [e["msg"] for e in d["entries"]] == ["hello", "careful", "broken thing"]
    assert d["containers"] == ["db", "web"] and d["events"] == ["check", "pull"]
    assert d["last_id"] == 3
    msgs = lambda **p: [e["msg"] for e in client.get("/api/logs", params=p).json()["entries"]]
    assert msgs(level="warning") == ["careful", "broken thing"]
    assert msgs(container="web") == ["hello"]
    assert msgs(event="pull") == ["careful"]
    assert msgs(q="BROKEN") == ["broken thing"]
    assert msgs(q="db") == ["careful"]  # search also matches container
    assert msgs(after=2) == ["broken thing"]
    assert msgs(limit=1) == ["broken thing"]
    assert msgs(limit=-5) == ["hello", "careful", "broken thing"]


def test_logs_download(client, buf):
    r = client.get("/api/logs/download", params={"level": "WARNING"})
    assert r.headers["content-disposition"].endswith('.log"')
    lines = r.text.strip().splitlines()
    assert len(lines) == 2 and "WARNING [db] (pull) careful" in lines[0]
    r = client.get("/api/logs/download", params={"fmt": "json", "container": "web"})
    assert r.headers["content-disposition"].endswith('.json"')
    assert [e["msg"] for e in r.json()] == ["hello"]


def test_actions(client, up):
    assert client.post("/api/containers/web/check").status_code == 202
    up.check_only.assert_called_with("web")
    client.post("/api/containers/web/update")
    up.update.assert_called_with("web", False)
    client.post("/api/containers/web/update?force=true")
    up.update.assert_called_with("web", True)
    client.post("/api/check-all")
    up.run_all.assert_called_with(False)
    r = client.post("/api/update-all")
    up.run_all.assert_called_with(True)
    assert r.json() == {"queued": True}


def _auth(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_basic_auth(client, monkeypatch):
    monkeypatch.setattr(main, "UI_PASS", "secret")
    r = client.get("/api/containers")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert client.get("/", headers=_auth("admin", "wrong")).status_code == 401
    assert client.get("/", headers=_auth("bob", "secret")).status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.get("/", headers={"Authorization": "Basic !!notb64"}).status_code == 401
    assert client.get("/", headers=_auth("admin", "secret")).status_code == 200


def test_lifespan_starts_scheduler(up, monkeypatch):
    monkeypatch.setattr(main, "INTERVAL", 5)
    monkeypatch.setattr(main, "scheduler", lambda: None)
    with TestClient(main.app):
        pass
    assert len(SyncThread.started) == 1


class StopLoop(BaseException):
    pass


def _run_scheduler(monkeypatch, loops, run_on_start="false"):
    monkeypatch.setenv("UPDOCK_RUN_ON_START", run_on_start)
    monkeypatch.setattr(main, "INTERVAL", 60)
    calls = {"n": 0}

    def fake_sleep(s):
        assert s == 60
        calls["n"] += 1
        if calls["n"] > loops:
            raise StopLoop

    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        main.scheduler()


def test_scheduler_runs_each_interval(up, monkeypatch):
    _run_scheduler(monkeypatch, loops=2)
    assert up.run_all.call_count == 2
    assert main.next_run["t"] is not None


def test_scheduler_run_on_start(up, monkeypatch):
    _run_scheduler(monkeypatch, loops=0, run_on_start="true")
    up.run_all.assert_called_once_with(main.AUTO_UPDATE)


def test_scheduler_survives_errors(up, monkeypatch):
    up.run_all.side_effect = RuntimeError("boom")
    _run_scheduler(monkeypatch, loops=2)
    assert up.run_all.call_count == 2


def test_tags_endpoint(client, up):
    up.tags.return_value = {"image": "x", "current": "1", "tags": ["2", "1"]}
    assert client.get("/api/containers/web/tags").json()["tags"] == ["2", "1"]
    up.tags.side_effect = RuntimeError("registry down")
    r = client.get("/api/containers/web/tags")
    assert r.status_code == 502 and "registry down" in r.json()["detail"]


def test_deploy_endpoint(client, up):
    assert client.post("/api/containers/web/deploy?tag=1.2.3-alpine").status_code == 202
    up.deploy.assert_called_with("web", "1.2.3-alpine")
    for bad in ("-x", "a/b", "a:b", "x" * 129):
        assert client.post("/api/containers/web/deploy", params={"tag": bad}).status_code == 400
    assert client.post("/api/containers/web/deploy").status_code == 422


# ---------- CSRF / security headers ----------

SAME = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin", "X-Updock-Request": "1"}


def test_post_from_ui_allowed(client, up):
    assert client.post("/api/check-all", headers=SAME).status_code == 202


def test_post_without_browser_headers_allowed(client, up):
    # curl / CI webhooks: no Origin, no Sec-Fetch-Site
    assert client.post("/api/check-all").status_code == 202


@pytest.mark.parametrize("headers,reason", [
    ({"Origin": "https://evil.example"}, "origin https://evil.example not allowed"),
    ({"Origin": "null"}, "origin null not allowed"),
    ({"Sec-Fetch-Site": "cross-site"}, "Sec-Fetch-Site: cross-site"),
    ({"Sec-Fetch-Site": "same-site", "Origin": "http://testserver"}, "same-site"),
    ({"Origin": "http://testserver"}, "missing X-Updock-Request"),
    ({"Sec-Fetch-Site": "same-origin", "X-Updock-Request": "0"}, "missing X-Updock-Request"),
])
def test_cross_site_posts_blocked(client, up, buf, headers, reason):
    r = client.post("/api/update-all", headers=headers)
    assert r.status_code == 403 and reason in r.json()["detail"]
    up.run_all.assert_not_called()
    assert r.headers["x-frame-options"] == "DENY"


def test_blocked_request_is_logged(client, up, monkeypatch):
    from app.logs import LogBuffer
    b = LogBuffer()
    logging.getLogger("updock").addHandler(b)
    try:
        client.post("/api/update-all", headers={"Origin": "https://evil.example"})
    finally:
        logging.getLogger("updock").removeHandler(b)
    e = b.entries[-1]
    assert e["level"] == "WARNING" and e["event"] == "security"
    assert "blocked POST /api/update-all" in e["msg"]


def test_allowed_origins_for_reverse_proxy(client, up, monkeypatch):
    monkeypatch.setattr(main, "ALLOWED_ORIGINS", {"https://updock.example.com"})
    h = {"Origin": "https://updock.example.com/", "X-Updock-Request": "1"}
    assert client.post("/api/check-all", headers=h).status_code == 202


def test_sec_fetch_site_none_allowed(client, up):
    h = {"Sec-Fetch-Site": "none", "X-Updock-Request": "1"}
    assert client.post("/api/check-all", headers=h).status_code == 202


def test_get_never_csrf_checked(client, up):
    r = client.get("/api/containers", headers={"Origin": "https://evil.example",
                                               "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_security_headers(client, up, monkeypatch):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "cache-control" not in r.headers
    assert client.get("/api/containers").headers["cache-control"] == "no-store"
    monkeypatch.setattr(main, "UI_PASS", "secret")
    r401 = client.get("/api/containers")
    assert r401.status_code == 401 and r401.headers["x-frame-options"] == "DENY"



def test_notify_endpoints(client, monkeypatch):
    from unittest.mock import MagicMock
    n = MagicMock()
    n.describe.return_value = {"enabled": True, "to": ["a@x"], "events": ["update"], "server": "h:587"}
    n.to = ["a@x"]
    monkeypatch.setattr(main, "NOTIFIER", n)
    assert client.get("/api/notify").json()["enabled"] is True
    assert client.post("/api/notify/test").json() == {"sent": True, "to": ["a@x"]}
    n.test.side_effect = OSError("refused")
    r = client.post("/api/notify/test")
    assert r.status_code == 502 and "refused" in r.json()["detail"]
