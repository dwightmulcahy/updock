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
        "version": main.VERSION, "schedule": main.schedule_info()}


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
    from datetime import datetime, timezone
    from app.schedule import Cron
    monkeypatch.setattr(main, "TZ", timezone.utc)
    ft, calls = _run_scheduler(monkeypatch, runs=2, cron=Cron("30 4 * * *"))
    times = [datetime.fromtimestamp(t, timezone.utc) for t in calls[:2]]
    assert [(x.hour, x.minute) for x in times] == [(4, 30), (4, 30)]
    assert calls[1] - calls[0] == 86400


def test_scheduled_run_window(up, monkeypatch):
    from datetime import timezone
    from app.schedule import Window
    monkeypatch.setattr(main, "TZ", timezone.utc)
    monkeypatch.setattr(main, "AUTO_UPDATE", True)
    hour = __import__("datetime").datetime.now(timezone.utc).hour
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
    assert main.schedule_info() == {"cron": "@daily", "interval": None, "window": "02:00-05:00",
                                    "tz": "Europe/Berlin"}
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
