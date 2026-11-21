import re

from app import metrics
from app.updater import Updater
from conftest import make_ctr


def parse(text):
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line:
            continue
        name_labels, value = line.rsplit(" ", 1)
        out[name_labels] = float(value)
    return out


def test_render_full(fake):
    u = Updater(fake)
    fake.add(make_ctr())
    skip = make_ctr("skip", cid="s" * 64)
    skip["Config"]["Labels"] = {"updock.enable": "false"}
    fake.add(skip)
    u.state["web"].update(update_available=True, last_check=100.0, last_update=50.0, pinned=True,
                          job="update-available")
    u.counters.update(result_updated=2, result_rolled_back=1, runs=3, run_failures=1)
    u.last_run = {"t": 200.0, "duration": 1.5}
    u._disk = {"used": 10, "reclaimable": 4, "max": None, "warn": 8}
    u.state["web"]["last_check"] = 1_800_000_123.25
    text = metrics.render(u, u.list(), "1.2.3", "abc1234", {"ERROR": 2, "INFO": 7})
    m = parse(text)
    assert m['updock_build_info{version="1.2.3",commit="abc1234"}'] == 1
    assert m['updock_containers{state="running"}'] == 2
    assert m["updock_containers_monitored"] == 1 and m["updock_containers_pinned"] == 1
    assert m['updock_container_update_available{container="web",image="test/app:latest"}'] == 1
    assert 'updock_container_last_check_timestamp_seconds{container="web"} 1800000123.25' in text
    assert m['updock_container_last_update_timestamp_seconds{container="web"}'] == 50
    assert m['updock_container_status{container="web",status="update-available"}'] == 1
    assert m['updock_results_total{result="updated"}'] == 2
    assert m['updock_results_total{result="cooldown"}'] == 0
    assert m["updock_runs_total"] == 3 and m["updock_run_failures_total"] == 1
    assert m["updock_last_run_duration_seconds"] == 1.5
    assert m['updock_log_messages_total{level="ERROR"}'] == 2
    assert m["updock_docker_images_bytes"] == 10 and m["updock_disk_max_bytes"] == 0
    assert m["updock_disk_warn_bytes"] == 8
    assert "updock_last_run_timestamp_seconds 200\n" in text  # no float rounding of big values
    # every sample has HELP/TYPE and valid syntax
    for line in text.splitlines():
        assert re.match(r'^(# (HELP|TYPE) \w+ .+|\w+(\{[^}]*\})? -?[\d.e+]+)$', line), line


def test_render_minimal(fake):
    u = Updater(fake)
    fake.add(make_ctr())
    m = parse(metrics.render(u, u.list(), "dev", "", {}))
    assert m['updock_container_status{container="web",status="unchecked"}'] == 1
    assert not any(k.startswith(("updock_last_run", "updock_docker_images")) for k in m)


def test_label_escaping():
    assert metrics._labels({"a": 'x"y\\z\nw'}) == '{a="x\\"y\\\\z\\nw"}'
    assert metrics._labels({}) == ""
