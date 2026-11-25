import base64
import json
import logging
from datetime import UTC
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

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
    m.run, m.last_run = None, {}
    m.run_status.return_value = None
    m.protected.return_value = False
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
        "version": main.VERSION, "schedule": main.schedule_info(), "run": None, "last_run": {},
        "user": None, "cooldown_gate": "confirm", "allow_delete": True, "self_update": False}


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


class FakeTime:
    def __init__(self, t=1_800_000_000.0):
        self.t, self.sleeps = t, []

    def time(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _run_scheduler(monkeypatch, runs, run_on_start="false", interval=150, cron=None, window=None):
    monkeypatch.setenv("UPDOCK_RUN_ON_START", run_on_start)
    monkeypatch.setattr(main, "INTERVAL", interval)
    monkeypatch.setattr(main, "CRON", cron)
    monkeypatch.setattr(main, "WINDOW", window)
    monkeypatch.setattr(main, "JITTER", 0)
    ft = FakeTime()
    monkeypatch.setattr(main.time, "time", ft.time)
    monkeypatch.setattr(main.time, "sleep", ft.sleep)
    calls = []
    real = main.scheduled_run

    def counting():
        calls.append(ft.t)
        if len(calls) > runs:
            raise StopLoop
        real()
    monkeypatch.setattr(main, "scheduled_run", counting)
    with pytest.raises(StopLoop):
        main.scheduler()
    return ft, calls


def test_scheduler_interval_naps_in_chunks(up, monkeypatch):
    ft, calls = _run_scheduler(monkeypatch, runs=2)
    assert up.run_all.call_count == 2
    assert ft.sleeps[:3] == [60, 60, 30] and calls[1] - calls[0] == 150
    assert main.next_run["t"] == calls[-1]


def test_scheduler_run_on_start(up, monkeypatch):
    _run_scheduler(monkeypatch, runs=1, run_on_start="true")
    assert up.run_all.call_count == 1
    up.run_all.assert_called_with(main.AUTO_UPDATE)


def test_scheduler_survives_errors(up, monkeypatch):
    up.run_all.side_effect = RuntimeError("boom")
    _run_scheduler(monkeypatch, runs=2)
    assert up.run_all.call_count == 2


def test_scheduler_cron_times(up, monkeypatch):
    from datetime import datetime

    from app.schedule import Cron
    monkeypatch.setattr(main, "TZ", UTC)
    ft, calls = _run_scheduler(monkeypatch, runs=2, cron=Cron("30 4 * * *"))
    times = [datetime.fromtimestamp(t, UTC) for t in calls[:2]]
    assert [(x.hour, x.minute) for x in times] == [(4, 30), (4, 30)]
    assert calls[1] - calls[0] == 86400


def test_scheduled_run_window(up, monkeypatch):

    from app.schedule import Window
    monkeypatch.setattr(main, "TZ", UTC)
    monkeypatch.setattr(main, "AUTO_UPDATE", True)
    hour = __import__("datetime").datetime.now(UTC).hour
    monkeypatch.setattr(main, "WINDOW", Window(f"{hour:02d}:00-{(hour + 1) % 24:02d}:00"))
    main.scheduled_run()
    up.run_all.assert_called_with(True)
    monkeypatch.setattr(main, "WINDOW", Window(f"{(hour + 2) % 24:02d}:00-{(hour + 3) % 24:02d}:00"))
    main.scheduled_run()
    up.run_all.assert_called_with(False)
    monkeypatch.setattr(main, "AUTO_UPDATE", False)
    monkeypatch.setattr(main, "WINDOW", None)
    main.scheduled_run()
    up.run_all.assert_called_with(False)


def test_schedule_info_and_tz(monkeypatch):
    from app.schedule import Cron, Window
    monkeypatch.setattr(main, "CRON", Cron("@daily"))
    monkeypatch.setattr(main, "WINDOW", Window("02:00-05:00"))
    monkeypatch.setattr(main, "TZ", main._tz("Europe/Berlin"))
    monkeypatch.setattr(main, "JITTER", 60)
    assert main.schedule_info() == {"cron": "@daily", "interval": None, "jitter": 60,
                                    "window": "02:00-05:00", "tz": "Europe/Berlin"}
    monkeypatch.setattr(main, "CRON", None)
    monkeypatch.setattr(main, "INTERVAL", 0)
    assert main.schedule_info()["interval"] is None
    assert str(main._tz("")) == "UTC" and str(main._tz("Not/AZone")) == "UTC"


def test_lifespan_logs_cron(up, monkeypatch):
    from app.schedule import Cron, Window
    monkeypatch.setattr(main, "CRON", Cron("0 4 * * *"))
    monkeypatch.setattr(main, "WINDOW", Window("02:00-05:00"))
    monkeypatch.setattr(main, "scheduler", lambda: None)
    with TestClient(main.app):
        pass
    assert len(SyncThread.started) == 1


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


def test_rollback_endpoint(client, up):
    assert client.post("/api/containers/web/rollback").status_code == 202
    up.rollback.assert_called_with("web")


def test_project_run_endpoint(client, up):
    up.projects.return_value = ["proj"]
    assert client.post("/api/projects/proj/update").status_code == 202
    up.run_all.assert_called_with(True, "proj")
    assert client.post("/api/projects/proj/check").status_code == 202
    up.run_all.assert_called_with(False, "proj")
    assert client.post("/api/projects/nope/check").status_code == 404
    assert client.post("/api/projects/proj/delete").status_code == 404
    up.run = {"kind": "check"}
    assert client.post("/api/projects/proj/update").status_code == 409


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



def test_tag_info_endpoint(client, up):
    up.tag_info.return_value = {"ref": "x:1", "version": "1"}
    assert client.get("/api/containers/web/tags/1.0").json()["version"] == "1"
    up.tag_info.assert_called_with("web", "1.0")
    assert client.get("/api/containers/web/tags/-bad").status_code == 400
    up.tag_info.side_effect = RuntimeError("manifest unknown")
    r = client.get("/api/containers/web/tags/9.9")
    assert r.status_code == 502 and "manifest unknown" in r.json()["detail"]


def test_pin_endpoint(client, up):
    assert client.post("/api/containers/web/pin").json() == {"name": "web", "pinned": True}
    up.pin.assert_called_with("web", True)
    client.post("/api/containers/web/pin?pinned=false")
    up.pin.assert_called_with("web", False)
    up.pin.side_effect = KeyError("ghost")
    assert client.post("/api/containers/ghost/pin").status_code == 404



def test_disk_endpoint(client, up):
    up.disk_status.return_value = {"enabled": True, "used": 5, "status": "ok"}
    assert client.get("/api/disk").json()["used"] == 5
    up.disk_status.assert_called_with(max_age=60)
    up.disk_status.side_effect = RuntimeError("no df")
    r = client.get("/api/disk")
    assert r.status_code == 502 and "no df" in r.json()["detail"]



# ---------- probes / metrics ----------

def test_probes_public_and_readiness(client, up, monkeypatch):
    monkeypatch.setattr(main, "UI_PASS", "secret")
    assert client.get("/livez").json() == {"status": "ok"}
    up.d.ping.return_value = True
    assert client.get("/readyz").json() == {"status": "ok", "docker": True}
    up.d.ping.return_value = False
    assert client.get("/readyz").status_code == 503
    up.d.ping.side_effect = OSError("no socket")
    assert client.get("/readyz").json()["docker"] is False
    assert client.get("/startupz").json() == {"status": "ok"}  # lifespan ran
    monkeypatch.setitem(main.STARTED, "done", False)
    assert client.get("/startupz").status_code == 503


def test_metrics_endpoint_auth(client, up, monkeypatch):
    up.list.return_value = []
    up.counters = __import__("collections").Counter()
    up.last_run, up._disk = {}, None
    monkeypatch.setattr(main, "UI_PASS", "secret")
    assert client.get("/metrics").status_code == 401
    r = client.get("/metrics", headers=_auth("admin", "secret"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "updock_runs_total 0" in r.text
    monkeypatch.setattr(main, "METRICS_PUBLIC", True)
    assert client.get("/metrics").status_code == 200


def test_log_counts_listener():
    import logging as lg
    before = main.LOG_COUNTS.get("ERROR", 0)
    main.LOGBUF.emit(lg.LogRecord("x", lg.ERROR, "", 0, "boom", (), None))
    assert main.LOG_COUNTS["ERROR"] == before + 1



# ---------- API token / events ----------

def test_api_token(client, up, monkeypatch):
    monkeypatch.setattr(main, "UI_PASS", "secret")
    monkeypatch.setattr(main, "API_TOKEN", "tok-123")
    up.list.return_value = []
    up.counters = __import__("collections").Counter()
    up.last_run, up._disk = {}, None
    bearer = {"Authorization": "Bearer tok-123"}
    assert client.post("/api/update-all", headers=bearer).status_code == 202
    assert client.get("/metrics", headers=bearer).status_code == 200
    r = client.post("/api/update-all", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["detail"] == "invalid API token"
    assert client.get("/", headers=bearer).status_code == 401  # token is API-only
    monkeypatch.setattr(main, "API_TOKEN", "")
    assert client.get("/api/containers", headers=bearer).status_code == 401


def test_state_changes_published(monkeypatch):
    seen = []
    monkeypatch.setattr(main.HUB, "publish", lambda kind, data: seen.append((kind, data)))
    main.up.on_change("web", {"job": "pulling"})
    assert seen == [("state", {"name": "web", "job": "pulling"})]


def test_log_entries_published(monkeypatch):
    import logging as lg
    seen = []
    monkeypatch.setattr(main.HUB, "publish", lambda kind, data: seen.append(kind))
    main.LOGBUF.emit(lg.LogRecord("x", lg.INFO, "", 0, "hello", (), None))
    assert "log" in seen


def test_events_endpoint(client, monkeypatch):
    async def fake_stream(hub, q, is_disconnected):
        yield "retry: 3000\n\n"
        yield "id: 1\nevent: log\ndata: {}\n\n"
    monkeypatch.setattr(main.eventsmod, "stream", fake_stream)
    with client.stream("GET", "/api/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        body = "".join(r.iter_text())
    assert "event: log" in body



def test_next_due_jitter(monkeypatch):

    from app.schedule import Cron
    monkeypatch.setattr(main, "CRON", None)
    monkeypatch.setattr(main, "INTERVAL", 100)
    monkeypatch.setattr(main, "JITTER", 30)
    monkeypatch.setattr(main.random, "uniform", lambda a, b: b)
    assert main.next_due(1000) == 1130
    monkeypatch.setattr(main, "CRON", Cron("0 0 * * *"))
    monkeypatch.setattr(main, "TZ", UTC)
    assert main.next_due(0) == 86400 + 30
    monkeypatch.setattr(main, "JITTER", 0)
    assert main.next_due(0) == 86400



# ---------- registry logins API ----------

@pytest.fixture
def logins(monkeypatch, tmp_path):
    from app.logins import RegistryLogins
    lg = RegistryLogins(str(tmp_path / "r.json"))
    monkeypatch.setattr(main, "LOGINS", lg)
    return lg


def test_registries_api(client, up, logins):
    up.state = {"web": {"job": "auth-required", "auth_host": "ghcr.io"}, "db": {"job": "up-to-date"}}
    up._backoff = {"ghcr.io": {"hits": 2}}
    r = client.post("/api/registries", json={"host": "https://ghcr.io/", "username": " me ", "password": "tok"})
    assert r.json() == {"saved": True, "host": "ghcr.io", "rechecking": ["web"]}
    up.registry.verify_login.assert_called_with("ghcr.io", "me", "tok")
    up.registry.forget.assert_called_with("ghcr.io")
    up.recheck_auth.assert_called_with("ghcr.io")
    assert "ghcr.io" not in up._backoff
    d = client.get("/api/registries").json()
    assert d == {"logins": [{"host": "ghcr.io", "username": "me", "persisted": True}], "persisted": True}
    assert client.delete("/api/registries/ghcr.io").json() == {"removed": "ghcr.io"}
    assert client.delete("/api/registries/ghcr.io").status_code == 404


def test_registries_api_errors(client, up, logins):
    from app.registry import RegistryAuthError
    assert client.post("/api/registries", json={"host": "", "username": "u", "password": "p"}).status_code == 400
    up.registry.verify_login.side_effect = RegistryAuthError("ghcr.io", "x")
    r = client.post("/api/registries", json={"host": "ghcr.io", "username": "u", "password": "bad"})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    up.registry.verify_login.side_effect = OSError("no route")
    r = client.post("/api/registries", json={"host": "docker.io", "username": "u", "password": "p"})
    assert r.status_code == 502 and "no route" in r.json()["detail"] and "docker.io" in r.json()["detail"]
    up.state, up._backoff = {}, {}
    r = client.post("/api/registries?verify=false", json={"host": "docker.io", "username": "u", "password": "p"})
    assert r.json()["host"] == "docker.io" and logins.get("docker.io") == ("u", "p")


def test_registries_without_registry_client(client, up, logins, monkeypatch):
    up.registry = None
    up.state, up._backoff = {}, {}
    assert client.post("/api/registries", json={"host": "q.io", "username": "u", "password": "p"}).status_code == 200
    assert client.delete("/api/registries/q.io").status_code == 200



def test_bulk_runs_refused_while_running(client, up):
    up.run = {"kind": "check"}
    for path in ("/api/check-all", "/api/update-all"):
        r = client.post(path)
        assert r.status_code == 409 and "already in progress" in r.json()["detail"]
    up.run_all.assert_not_called()


def test_run_events_published(monkeypatch):
    seen = []
    monkeypatch.setattr(main.HUB, "publish", lambda kind, data: seen.append((kind, data)))
    main.up.on_run({"kind": "check", "total": 3})
    assert seen == [("run", {"run": {"kind": "check", "total": 3}, "last_run": main.up.last_run})]


# ---------- reverse-proxy header auth ----------

@pytest.fixture
def proxied(up, monkeypatch):
    monkeypatch.setattr(main, "AUTH_HEADER", "Remote-User")
    monkeypatch.setattr(main, "TRUSTED_PROXIES", main.parse_networks("10.0.0.0/24, ::1"))
    monkeypatch.setattr(main, "AUTH_USERS", set())

    def make(host):
        return TestClient(main.app, client=(host, 1234))
    return make


def test_proxy_header_signs_in(proxied, up):
    with proxied("10.0.0.5") as c:
        r = c.get("/api/containers", headers={"Remote-User": "alice"})
        assert r.status_code == 200 and r.json()["user"] == "alice"
        r = c.get("/api/containers")
        assert r.status_code == 401 and "Remote-User" in r.json()["detail"]
        assert c.get("/livez").status_code == 200


def test_proxy_header_allowlist(proxied, monkeypatch):
    monkeypatch.setattr(main, "AUTH_USERS", {"alice"})
    with proxied("10.0.0.5") as c:
        assert c.get("/", headers={"Remote-User": "alice"}).status_code == 200
        assert c.get("/", headers={"Remote-User": "mallory"}).status_code == 401


def test_proxy_header_ignored_from_untrusted_peer(proxied, monkeypatch):
    with proxied("192.168.1.9") as c:  # no password: direct access is refused
        r = c.get("/api/containers", headers={"Remote-User": "alice"})
        assert r.status_code == 401 and "reverse proxy" in r.json()["detail"]
    monkeypatch.setattr(main, "UI_PASS", "secret")
    with proxied("192.168.1.9") as c:  # with a password: Basic auth still works
        assert c.get("/", headers={"Remote-User": "alice"}).status_code == 401
        r = c.get("/api/containers", headers=_auth("admin", "secret"))
        assert r.status_code == 200 and r.json()["user"] is None
    with proxied("not-an-ip") as c:
        assert c.get("/", headers={"Remote-User": "alice"}).status_code == 401


def test_proxy_header_needs_trusted_proxies(up, monkeypatch, caplog):
    monkeypatch.setattr(main, "AUTH_HEADER", "Remote-User")
    monkeypatch.setattr(main, "TRUSTED_PROXIES", [])
    with TestClient(main.app, client=("10.0.0.5", 1)) as c:
        r = c.get("/api/containers", headers={"Remote-User": "alice"})
        assert r.status_code == 200 and r.json()["user"] is None  # no password, header ignored
    with caplog.at_level("ERROR", logger="updock"):
        main.check_auth_config()
    assert "TRUSTED_PROXIES is empty" in caplog.text


def test_parse_networks(caplog):
    with caplog.at_level("ERROR", logger="updock"):
        nets = main.parse_networks(" 10.1.2.3 , bogus, fd00::/8,")
    assert [str(n) for n in nets] == ["10.1.2.3/32", "fd00::/8"]
    assert "bogus" in caplog.text
    main.check_auth_config()  # header auth off: nothing to report


# ---------- about ----------

def test_about(client, up, monkeypatch):
    up.d.version.return_value = {"Version": "29.0.1", "ApiVersion": "1.52", "Os": "linux",
                                 "Arch": "amd64"}
    for k, v in dict(cleanup=True, digest_check=True, label_enable=False, include_stopped=False,
                     revive_stopped=False, health_timeout=120, startup_grace=10, cooldown=0,
                     hooks=False, scope=None, allow="minor").items():
        setattr(up, k, v)
    monkeypatch.setenv("DOCKER_HOST", "tcp://user:pw@socket-proxy:2375")
    monkeypatch.setattr(main, "UI_PASS", "x")
    a = client.get("/api/about", headers=_auth("admin", "x")).json()
    assert a["engine"] == {"version": "29.0.1", "api": "1.52", "os": "linux", "arch": "amd64"}
    assert a["docker_host"] == "tcp://socket-proxy:2375"  # credentials never shown
    assert a["auth"] == "password" and a["settings"]["health_timeout"] == 120
    assert a["settings"]["allow"] == "minor"
    up.d.version.side_effect = RuntimeError("proxy says no")
    monkeypatch.setenv("DOCKER_HOST", "unix")
    a = client.get("/api/about", headers=_auth("admin", "x")).json()
    assert a["engine"] == {"error": "proxy says no"} and a["docker_host"] == "unix://unix"


def test_auth_mode(monkeypatch):
    monkeypatch.setattr(main, "UI_PASS", "")
    monkeypatch.setattr(main, "AUTH_HEADER", "")
    assert main.auth_mode() == "none"
    monkeypatch.setattr(main, "AUTH_HEADER", "Remote-User")
    monkeypatch.setattr(main, "TRUSTED_PROXIES", main.parse_networks("10.0.0.1"))
    assert main.auth_mode() == "reverse proxy (Remote-User)"
    monkeypatch.setattr(main, "UI_PASS", "x")
    assert main.auth_mode() == "reverse proxy (Remote-User) + password"


def test_build_info(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "0123456789abcdef")
    monkeypatch.setattr(main, "BUILD_DATE", "2026-11-23T17:00:00Z")
    b = client.get("/api/build-info").json()
    assert b["commit"] == "0123456789ab" and b["build_date"] == "2026-11-23T17:00:00Z"
    assert b["python"] and b["platform"] and b["started_at"].endswith("Z")
    monkeypatch.delenv("GIT_SHA")
    assert client.get("/api/build-info").json()["commit"] is None


# ---------- cooldown override gate ----------

def test_cooldown_override_needs_admin_password(client, up, monkeypatch, caplog):
    monkeypatch.setattr(main, "UI_PASS", "s3cret")
    h = {**_auth("admin", "s3cret"), "Content-Type": "application/json"}
    r = client.post("/api/containers/web/update", headers=h,
                    content='{"override_cooldown": true, "password": "nope"}')
    assert r.status_code == 403 and "wrong admin password" in r.text
    up.update.assert_not_called()
    r = client.post("/api/containers/web/update", headers=h,
                    content='{"override_cooldown": true, "password": "s3cret"}')
    assert r.status_code == 202
    up.update.assert_called_with("web", False, False, True)
    assert "cooldown skipped by admin" in caplog.text
    assert client.get("/api/containers", headers=h).json()["cooldown_gate"] == "password"


def test_cooldown_override_with_token_or_without_password(client, up, monkeypatch):
    monkeypatch.setattr(main, "UI_PASS", "s3cret")
    monkeypatch.setattr(main, "API_TOKEN", "tok")
    r = client.post("/api/containers/web/update", json={"override_cooldown": True},
                    headers={"Authorization": "Bearer tok"})
    assert r.status_code == 202
    up.update.assert_called_with("web", False, False, True)
    monkeypatch.setattr(main, "UI_PASS", "")
    assert client.post("/api/containers/web/update", json={"override_cooldown": True}).status_code == 202
    assert client.post("/api/containers/web/update", json={}).status_code == 202
    up.update.assert_called_with("web", False)


def test_version_approval_needs_admin_password(client, up, monkeypatch, caplog):
    monkeypatch.setattr(main, "UI_PASS", "s3cret")
    h = _auth("admin", "s3cret")
    r = client.post("/api/containers/web/update", headers=h, json={"approve": True, "password": "x"})
    assert r.status_code == 403 and "version approval refused" in caplog.text
    up.update.assert_not_called()
    r = client.post("/api/containers/web/update", headers=h,
                    json={"approve": True, "override_cooldown": True, "password": "s3cret"})
    assert r.status_code == 202
    up.update.assert_called_with("web", False, False, True, True)
    assert "version update approved by admin" in caplog.text


# ---------- delete ----------

def test_delete_endpoint(client, up, monkeypatch):
    assert client.post("/api/containers/web/delete").status_code == 202
    up.delete.assert_called_with("web", False, False)
    r = client.post("/api/containers/web/delete", json={"remove_image": True, "remove_volumes": True})
    assert r.status_code == 202
    up.delete.assert_called_with("web", True, True)
    up.d.inspect.side_effect = KeyError("nope")
    assert client.post("/api/containers/nope/delete").status_code == 404


def test_delete_needs_admin_password_and_can_be_disabled(client, up, monkeypatch, caplog):
    monkeypatch.setattr(main, "UI_PASS", "s3cret")
    h = _auth("admin", "s3cret")
    r = client.post("/api/containers/web/delete", json={"password": "x"}, headers=h)
    assert r.status_code == 403 and "delete refused: wrong admin password" in caplog.text
    up.delete.assert_not_called()
    assert client.post("/api/containers/web/delete", json={"password": "s3cret"}, headers=h).status_code == 202
    assert client.get("/api/containers", headers=h).json()["allow_delete"] is True
    monkeypatch.setattr(main, "ALLOW_DELETE", False)
    r = client.post("/api/containers/web/delete", json={"password": "s3cret"}, headers=h)
    assert r.status_code == 403 and "UPDOCK_ALLOW_DELETE" in r.text



@pytest.fixture(autouse=True)
def _reset_admin_fails():
    main._admin_fails.clear()
    yield
    main._admin_fails.clear()


def test_admin_password_throttled_after_repeated_failures(client, up, monkeypatch, caplog):
    monkeypatch.setattr(main, "UI_PASS", "s3cret")
    h = _auth("admin", "s3cret")
    url = "/api/containers/web/update"
    for i in range(main.ADMIN_FAIL_LIMIT):
        r = client.post(url, json={"override_cooldown": True, "password": "" if i == 0 else "bad"},
                        headers=h)
        assert r.status_code == 403
    assert "empty password from testclient (1 of 5 tries" in caplog.text
    assert "wrong admin password from testclient (5 of 5 tries" in caplog.text
    r = client.post(url, json={"override_cooldown": True, "password": "s3cret"}, headers=h)
    assert r.status_code == 429 and "try again in" in r.json()["detail"]  # even when right
    up.update.assert_not_called()
    t = main.time.monotonic() + main.ADMIN_FAIL_WINDOW + 1  # the window passes
    monkeypatch.setattr(main.time, "monotonic", lambda: t)
    r = client.post(url, json={"override_cooldown": True, "password": "s3cret"}, headers=h)
    assert r.status_code == 202 and not main._admin_fails["testclient"]


# ---------- web layer (Starlette) ----------

def test_json_encoding_of_sets_dates_paths():
    from datetime import datetime
    from pathlib import Path
    out = main.JSON({"s": {1}, "t": (2,), "d": datetime(2026, 1, 2, tzinfo=UTC),
                     "p": Path("/x"), "u": "é"}).body
    assert json.loads(out) == {"s": [1], "t": [2], "d": "2026-01-02T00:00:00+00:00", "p": "/x",
                               "u": "é"}
    with pytest.raises(TypeError):
        main._json_default(object())


def test_query_parameter_validation(client, up):
    r = client.post("/api/containers/web/update?force=maybe")
    assert r.status_code == 422 and r.json() == {"detail": "query parameter 'force' must be true or false"}
    assert client.post("/api/containers/web/update?force=yes").status_code == 202
    up.update.assert_called_with("web", True)
    assert client.post("/api/containers/web/pin?pinned=0").json() == {"name": "web", "pinned": False}
    assert client.get("/api/logs?limit=ten").json()["detail"] == "query parameter 'limit' must be an integer"
    assert client.get("/api/logs?since=soon").json()["detail"] == "query parameter 'since' must be a number"
    assert client.get("/api/logs?since=&after=").status_code == 200


def test_body_validation(client, up):
    url = "/api/containers/web/update"
    for raw, why in (("{nope", "must be JSON"), ("[1]", "must be a JSON object"),
                     ('{"override_cooldown": "yes"}', "'override_cooldown' must be a bool")):
        r = client.post(url, content=raw, headers={"Content-Type": "application/json"})
        assert r.status_code == 422 and why in r.json()["detail"], raw
    assert client.post(url, json={"override_cooldown": None}).status_code == 202  # null = default
    r = client.post("/api/registries", json={"host": "ghcr.io", "username": "me"})
    assert r.status_code == 422 and r.json()["detail"] == "'password' is required"
    r = client.post("/api/registries")
    assert r.status_code == 422 and r.json()["detail"] == "'host' is required"


def test_unknown_routes_and_methods_are_json(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    r = client.get("/api/containers/web/update")
    assert r.status_code == 405 and r.json() == {"detail": "Method Not Allowed"}


# ---------- bundled changelog ----------

def test_changelog_endpoint(client, monkeypatch, tmp_path):
    f = tmp_path / "changelog.json"
    monkeypatch.setattr(main, "CHANGELOG", f)
    monkeypatch.setattr(main, "VERSION", "0.8.0")
    assert client.get("/api/changelog").json() == {"current": "0.8.0", "available": False,
                                                  "repository": "", "releases": []}
    rels = [{"version": v} for v in ("0.8.0", "0.7.0", "0.6.0", "0.5.0", "0.4.0", "0.3.0")]
    f.write_text(json.dumps({"repository": "me/updock", "releases": rels}))
    d = client.get("/api/changelog?since=v0.6.0").json()
    assert [r["version"] for r in d["releases"]] == ["0.8.0", "0.7.0"] and d["repository"] == "me/updock"
    assert len(client.get("/api/changelog").json()["releases"]) == 5
    assert len(client.get("/api/changelog?since=garbage").json()["releases"]) == 5
    f.write_text("{not json")
    assert client.get("/api/changelog").json()["available"] is False
    assert main._version_key("1.2.3-rc1") == (1, 2, 3) and main._version_key("x") is None


def test_version_policy_endpoint(client, up):
    url = "/api/containers/web/version-policy"
    up.set_version_policy.return_value = {"effective": "patch", "source": "gui", "override": "patch"}
    assert client.post(url, json={"policy": "patch"}).json()["effective"] == "patch"
    up.set_version_policy.assert_called_once_with("web", "patch")
    assert client.post(url, json={"policy": "invalid"}).status_code == 422
    up.d.inspect.side_effect = KeyError("web")
    assert client.post(url, json={"policy": "inherit"}).status_code == 404


# ---------- more endpoint tests ----------

def test_selected_runs(client, up):
    r = client.post("/api/selected/update", json={"names": ["web", " db ", "web"]})
    assert r.status_code == 202
    up.run_all.assert_called_with(True, None, ["db", "web"])
    client.post("/api/selected/check", json={"names": ["web"]})
    up.run_all.assert_called_with(False, None, ["web"])
    assert client.post("/api/selected/nuke", json={"names": ["web"]}).status_code == 404
    for bad in ({"names": []}, {"names": [""]}, {"names": [1]}, {"names": "web"}, {}):
        assert client.post("/api/selected/check", json=bad).status_code == 422
    assert client.post("/api/selected/check",
                       json={"names": [f"c{i}" for i in range(501)]}).status_code == 422
    up.run = {"kind": "check"}
    assert client.post("/api/selected/check", json={"names": ["web"]}).status_code == 409


def test_self_update_in_progress_blocks_runs(client, up, monkeypatch):
    monkeypatch.setattr(main.SELF, "in_progress", True)
    r = client.post("/api/update-all")
    assert r.status_code == 409 and "self-update" in r.json()["detail"]


def test_protected_refused_by_endpoints(client, up):
    up.protected.return_value = True
    for path, kw in (("/api/containers/web/update", {}), ("/api/containers/web/deploy?tag=1", {}),
                     ("/api/containers/web/rollback", {}),
                     ("/api/containers/web/delete", {"json": {}})):
        r = client.post(path, **kw)
        assert r.status_code == 409 and "is protected" in r.json()["detail"], path
    up.update.assert_not_called()
    up.delete.assert_not_called()


@pytest.fixture
def health(monkeypatch):
    m = MagicMock()
    m.enabled, m.interval, m.notify_after, m.restart_after = True, 60, 3, 0
    m.state = {"web": {"status": "unhealthy", "failures": 4}}
    m.restarts_total = 2
    m.status.return_value = {"enabled": True}
    m.query.return_value = [{"container": "web", "status": "unhealthy"}]
    monkeypatch.setattr(main, "HEALTH", m)
    return m


def test_health_endpoints(client, up, health):
    up.list.return_value = [{"name": "web"}, {"name": "db"}]
    rows = client.get("/api/containers").json()["containers"]
    assert rows[0]["health"]["status"] == "unhealthy" and rows[1]["health"] is None
    assert client.get("/api/health").json() == {"enabled": True}
    r = client.get("/api/health/history?container=web&limit=99999").json()
    assert r["entries"][0]["status"] == "unhealthy"
    health.query.assert_called_with("web", 5000)
    assert client.post("/api/health/check").status_code == 202
    health.tick.assert_called_once()


def test_health_metrics(client, up, health):
    up.list.return_value = []
    up.counters = __import__("collections").Counter()
    up.last_run, up._disk = {}, None
    text = client.get("/metrics").text
    assert 'updock_container_unhealthy{container="web"} 1' in text
    assert "updock_health_restarts_total 2" in text


def test_lifespan_starts_health_monitor_and_report(up, health, monkeypatch):
    reports = []
    monkeypatch.setattr(main.SELF, "pending", lambda: True)
    monkeypatch.setattr(main.SELF, "report_in_background", lambda wait: reports.append(wait))
    up.health_timeout = 120
    with TestClient(main.app):
        pass
    assert SyncThread.started == [health.loop] and reports == [240]


@pytest.fixture
def selfu(monkeypatch):
    m = MagicMock()
    m.status.return_value = {"allowed": True, "update_available": False}
    m.check.return_value = True
    monkeypatch.setattr(main, "SELF", m)
    m.in_progress = False
    return m


def test_self_endpoints(client, up, selfu, monkeypatch):
    assert client.get("/api/self").json()["allowed"] is True
    assert client.post("/api/self/check").json()["update_available"] is True
    selfu.check.side_effect = RuntimeError("registry down")
    r = client.post("/api/self/check")
    assert r.status_code == 502 and "registry down" in r.json()["detail"]
    assert client.post("/api/self/update").status_code == 202
    selfu.start_safely.assert_called_once()
    selfu.preflight.side_effect = main.SelfUpdateError("self-update is disabled")
    r = client.post("/api/self/update", json={})
    assert r.status_code == 409 and "disabled" in r.json()["detail"]


def test_self_update_needs_admin_password(client, up, selfu, monkeypatch):
    monkeypatch.setattr(main, "UI_PASS", "pw")
    h = _auth("admin", "pw")
    assert client.post("/api/self/update", json={"password": "bad"}, headers=h).status_code == 403
    assert client.post("/api/self/update", json={"password": "pw"}, headers=h).status_code == 202


def test_public_summary(client, up, health, monkeypatch):
    main._summary.update(t=0.0, data=None)
    monkeypatch.setitem(main.next_run, "t", None)
    up.list.return_value = [
        {"name": "a", "eligible": True, "state": "running", "update_available": True},
        {"name": "b", "eligible": True, "state": "exited", "job": "error"},
        {"name": "c", "eligible": False, "state": "running", "update_available": True}]
    up.last_run = {"t": 5, "kind": "check", "duration": 1.5, "results": {}}
    monkeypatch.setattr(main, "UI_PASS", "pw")
    assert client.get("/api/public/summary").status_code == 401  # off by default
    monkeypatch.setattr(main, "PUBLIC_API", True)
    s = client.get("/api/public/summary").json()
    assert s == {"version": main.VERSION, "containers": 3, "running": 2, "monitored": 2,
                 "updates_available": 1, "problems": 1, "unhealthy": 1,
                 "last_run": {"t": 5, "kind": "check", "duration": 1.5}, "next_run": None,
                 "is_update_available": True}
    up.list.return_value = []
    assert client.get("/api/public/summary").json()["containers"] == 3  # cached
    monkeypatch.setattr(main, "SUMMARY_TTL", -1)
    health.enabled = False
    up.last_run = {}
    s = client.get("/api/public/summary").json()
    assert s["containers"] == 0 and s["unhealthy"] is None and s["last_run"] is None
    assert client.get("/api/containers").status_code == 401  # only /api/public/ is open


def test_selected_update_skipping_cooldown(client, up, monkeypatch, caplog):
    monkeypatch.setattr(main, "UI_PASS", "pw")
    h = _auth("admin", "pw")
    body = {"names": ["web", "db"], "override_cooldown": True}
    r = client.post("/api/selected/update", json={**body, "password": "bad"}, headers=h)
    assert r.status_code == 403
    up.run_all.assert_not_called()
    r = client.post("/api/selected/update", json={**body, "password": "pw"}, headers=h)
    assert r.status_code == 202
    up.run_all.assert_called_with(True, None, ["db", "web"], True)
    assert "cooldown skipped for db, web by admin" in caplog.text
    up.run_all.reset_mock()
    client.post("/api/selected/check", json={**body, "password": "bad"}, headers=h)  # ignored
    up.run_all.assert_called_with(False, None, ["db", "web"])
