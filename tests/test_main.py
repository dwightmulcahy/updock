import base64
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
    m.logs = [{"msg": str(i)} for i in range(250)]
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


def test_logs_truncated(client):
    logs = client.get("/api/logs").json()
    assert len(logs) == 200 and logs[-1]["msg"] == "249"


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
