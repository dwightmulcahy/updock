import json
from contextlib import contextmanager

import pytest

from app.health import HealthMonitor, health_of
from app.updater import Updater
from conftest import make_ctr


class Notifier:
    def __init__(self):
        self.sent, self.batches = [], 0

    def notify(self, event, container, message, details=None):
        self.sent.append((event, container, message))

    @contextmanager
    def batch(self):
        self.batches += 1
        yield


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("HOSTNAME", "selfhost")
    fake.add(make_ctr())
    status = {"web": "Up 1 minute (healthy)"}
    real = fake._entries

    def entries():
        out = real()
        for e in out:
            e["Status"] = status.get(e["Names"][0][1:], "Up")
        return out
    monkeypatch.setattr(fake, "_entries", entries)
    up, n, clock = Updater(fake), Notifier(), Clock()

    def make(**kw):
        kw.setdefault("interval", 60)
        return HealthMonitor(fake, up, n, data_dir=str(tmp_path), clock=clock, **kw)
    return make, status, n, clock, fake, up


@pytest.mark.parametrize("text,expected", [
    ("Up 3 minutes (unhealthy)", "unhealthy"), ("Up 2 hours (healthy)", "healthy"),
    ("Up 5 seconds (health: starting)", "starting"), ("Up 2 hours", None), (None, None)])
def test_health_of(text, expected):
    assert health_of(text) == expected


def test_notify_once_then_recovery(env):
    make, status, n, clock, fake, up = env
    hm = make(notify_after=2)
    assert hm.enabled and hm.tick() == 1 and n.sent == []
    status["web"] = "Up (unhealthy)"
    hm.tick()
    assert n.sent == [] and hm.state["web"]["failures"] == 1
    for _ in range(3):
        clock.t += 60
        hm.tick()
    assert n.sent == [("unhealthy", "web", "unhealthy for 2 checks in a row")]
    status["web"] = "Up (healthy)"
    clock.t += 60
    hm.tick()
    assert n.sent[-1][0] == "healthy" and "healthy again after 4m" in n.sent[-1][2]
    assert hm.state["web"]["failures"] == 0 and not hm.state["web"]["notified"]
    statuses = [e["status"] for e in hm.query()]
    assert statuses[0] == "healthy" and "unhealthy" in statuses
    assert hm.query("other") == [] and len(hm.query(limit=1)) == 1 and hm.query(limit=0)


def test_short_blip_not_notified(env):
    make, status, n, clock, fake, up = env
    hm = make()
    status["web"] = "Up (unhealthy)"
    hm.tick()
    status["web"] = "Up (healthy)"
    hm.tick()
    assert n.sent == []


def test_notifications_disabled(env):
    make, status, n, *_ = env
    hm = make(notify_after=0)
    status["web"] = "Up (unhealthy)"
    for _ in range(5):
        hm.tick()
    assert n.sent == []


def test_starting_not_counted(env):
    make, status, n, *_ = env
    hm = make(notify_after=1)
    status["web"] = "Up (health: starting)"
    hm.tick()
    hm.tick()
    assert hm.state["web"]["failures"] == 0 and n.sent == []
    assert [e["status"] for e in hm.history] == ["starting"]


def test_restart_after_n_failures_and_give_up(env, caplog):
    make, status, n, clock, fake, up = env
    hm = make(notify_after=0, restart_after=2, restart_attempts=2)
    status["web"] = "Up (unhealthy)"
    for _ in range(7):
        hm.tick()
    assert fake.restarted == [fake.ctrs["web"]["Id"]] * 2 and hm.restarts_total == 2
    assert hm.state["web"]["gave_up"] and "not restarting again" in caplog.text
    assert any(e.get("restart") for e in hm.history)


def test_restart_label_override(env):
    make, status, n, clock, fake, up = env
    fake.ctrs["web"]["Config"]["Labels"]["updock.health.restart-after"] = "1"
    hm = make(restart_after=0)
    status["web"] = "Up (unhealthy)"
    hm.tick()
    assert fake.restarted and hm.restart_after_for("web", {"updock.health.restart-after": "x"}) == 0
    assert hm.restart_after_for("web", {"updock.health.restart-after": "-3"}) == 0


def test_restart_failure_recorded(env, caplog):
    make, status, n, clock, fake, up = env
    fake.fail["restart"] = RuntimeError("nope")
    hm = make(restart_after=1)
    status["web"] = "Up (unhealthy)"
    hm.tick()
    assert "restart failed: nope" in caplog.text
    assert hm.history[-1]["restart"] is False


def test_protected_and_self_never_restarted(env, caplog):
    make, status, n, clock, fake, up = env
    fake.ctrs["web"]["Config"]["Labels"]["updock.protected"] = "true"
    me = make_ctr("updock", cid="selfhost" + "0" * 56)
    fake.add(me)
    status.update(web="Up (unhealthy)", updock="Up (unhealthy)")
    hm = make(restart_after=1)
    hm.tick()
    hm.tick()
    assert not getattr(fake, "restarted", None)
    assert caplog.text.count("not restarting: protected") == 1


def test_skips_unmonitored_busy_and_forgets_gone(env):
    make, status, n, clock, fake, up = env
    fake.add(make_ctr("off", cid="o" * 64))
    fake.ctrs["off"]["Config"]["Labels"]["updock.health.monitor"] = "false"
    fake.add(make_ctr("scoped", cid="s" * 64))
    fake.ctrs["scoped"]["Config"]["Labels"]["updock.scope"] = "other"
    fake.add(make_ctr("web-updock-old", cid="w" * 64))
    status.update({"off": "Up (unhealthy)", "scoped": "Up (unhealthy)",
                   "web-updock-old": "Up (unhealthy)"})
    hm = make()
    assert hm.tick() == 1
    up.locks["web"].acquire()
    status["web"] = "Up (unhealthy)"
    hm.tick()
    assert hm.state["web"]["failures"] == 0
    up.locks["web"].release()
    del fake.ctrs["web"]
    hm.tick()
    assert hm.state == {}


def test_observe_error_is_logged(env, monkeypatch, caplog):
    make, status, *_ = env
    hm = make()
    monkeypatch.setattr(hm, "_observe", lambda *a: (_ for _ in ()).throw(RuntimeError("bad")))
    hm.tick()
    assert "health check of web failed: bad" in caplog.text


def test_history_persisted_and_pruned(env, tmp_path):
    make, status, n, clock, *_ = env
    hm = make(history_seconds=3600)
    status["web"] = "Up (unhealthy)"
    hm.tick()
    path = tmp_path / "health.jsonl"
    with open(path, "a") as f:
        f.write("not json\n[1]\n")
    again = make(history_seconds=3600)
    assert [e["status"] for e in again.history] == ["unhealthy"]
    clock.t += 7200
    again.tick()
    assert all(e["t"] >= clock.t - 3600 for e in again.history)
    assert [json.loads(l)["t"] for l in path.read_text().splitlines()] == [e["t"] for e in again.history]


def test_history_write_errors_ignored(env, tmp_path, caplog):
    make, status, n, clock, fake, up = env
    hm = make()
    hm.path = str(tmp_path / "missing" / "health.jsonl")
    status["web"] = "Up (unhealthy)"
    hm.tick()
    hm._rewrite()
    assert "could not save health history" in caplog.text
    mem = HealthMonitor(fake, up, n, data_dir=None, clock=clock)
    assert not mem.enabled and mem.tick() == 1 and mem.history


def test_status_and_loop(env):
    make, status, *_ = env
    hm = make()
    hm.tick()
    st = hm.status()
    assert st["enabled"] and st["containers"]["web"]["status"] == "healthy"
    ticks = {"n": 0}

    def tick():
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise RuntimeError("boom")  # logged, the loop goes on
        if ticks["n"] == 3:
            raise KeyboardInterrupt
    hm.tick = tick
    naps = []
    with pytest.raises(KeyboardInterrupt):
        hm.loop(sleep=naps.append)
    assert ticks["n"] == 3 and naps == [60, 60, 60]


def test_replaced_container_starts_fresh(env):
    make, status, n, clock, fake, up = env
    hm = make(notify_after=0, restart_after=2)
    status["web"] = "Up (unhealthy)"
    hm.tick()
    assert hm.state["web"]["failures"] == 1
    fake.ctrs["web"]["Id"] = "f" * 64  # recreated (e.g. updated) under the same name
    hm.tick()
    assert hm.state["web"]["failures"] == 1 and hm.state["web"]["id"] == "f" * 64
    assert not getattr(fake, "restarted", None)
