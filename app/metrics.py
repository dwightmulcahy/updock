"""Prometheus text exposition (format 0.0.4) without a client library."""
import time

RESULTS = ("updated", "recreated", "rolled_back", "failed", "check_failed", "skipped", "cooldown",
           "rate_limited", "auth_required")


def _esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(d):
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in d.items()) + "}" if d else ""


class Writer:
    def __init__(self):
        self.lines = []

    def metric(self, name, kind, help_, samples):
        self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
        for labels, value in samples:
            v = float(value)
            self.lines.append(f"{name}{_labels(labels)} {int(v) if v.is_integer() else repr(v)}")

    def text(self):
        return "\n".join(self.lines) + "\n"


def render(up, rows, version, commit, log_counts):
    """`rows` = up.list(); `log_counts` = {level: n} since start."""
    w = Writer()
    w.metric("updock_build_info", "gauge", "Updock version.",
             [({"version": version, "commit": commit}, 1)])
    eligible = [r for r in rows if r["eligible"]]
    w.metric("updock_containers", "gauge", "Containers seen, by state.",
             [({"state": s}, sum(1 for r in rows if r["state"] == s))
              for s in sorted({r["state"] for r in rows})])
    w.metric("updock_containers_monitored", "gauge", "Containers Updock may update.",
             [({}, len(eligible))])
    w.metric("updock_containers_pinned", "gauge",
             "Containers pinned in the UI or labelled monitor-only.",
             [({}, sum(1 for r in eligible if r.get("pinned") or r.get("monitor_only")))])
    w.metric("updock_container_update_available", "gauge",
             "1 if a newer image was found at the last check.",
             [({"container": r["name"], "image": r["image"]}, 1 if r.get("update_available") else 0)
              for r in eligible])
    w.metric("updock_container_last_check_timestamp_seconds", "gauge",
             "Unix time of the last check.",
             [({"container": r["name"]}, r["last_check"]) for r in eligible if r.get("last_check")])
    w.metric("updock_container_last_update_timestamp_seconds", "gauge",
             "Unix time of the last update or recreate.",
             [({"container": r["name"]}, r["last_update"]) for r in eligible if r.get("last_update")])
    w.metric("updock_container_status", "gauge", "1 for the container's current Updock status.",
             [({"container": r["name"], "status": r.get("job") or "unchecked"}, 1) for r in eligible])
    w.metric("updock_results_total", "counter", "Update/check outcomes since start.",
             [({"result": k}, up.counters[f"result_{k}"]) for k in RESULTS])
    w.metric("updock_runs_total", "counter", "Scheduled/bulk runs since start.",
             [({}, up.counters["runs"])])
    w.metric("updock_run_failures_total", "counter", "Runs that ended in an error.",
             [({}, up.counters["run_failures"])])
    if up.last_run:
        w.metric("updock_last_run_timestamp_seconds", "gauge", "Unix time the last run ended.",
                 [({}, up.last_run["t"])])
        w.metric("updock_last_run_duration_seconds", "gauge", "Duration of the last run.",
                 [({}, up.last_run["duration"])])
    w.metric("updock_log_messages_total", "counter", "Log entries by level since start.",
             [({"level": lv}, n) for lv, n in sorted(log_counts.items())])
    d = up._disk
    if d:
        w.metric("updock_docker_images_bytes", "gauge", "Docker image storage at the last check.",
                 [({}, d["used"])])
        w.metric("updock_docker_images_reclaimable_bytes", "gauge",
                 "Reclaimable image storage at the last check.", [({}, d["reclaimable"])])
        w.metric("updock_disk_max_bytes", "gauge", "Configured image storage limit (0 = none).",
                 [({}, d["max"] or 0)])
        w.metric("updock_disk_warn_bytes", "gauge", "Configured warning level (0 = none).",
                 [({}, d["warn"] or 0)])
    w.metric("updock_scrape_timestamp_seconds", "gauge", "When these metrics were generated.",
             [({}, time.time())])
    return w.text()
