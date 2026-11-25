# API and monitoring

| Action | Endpoint |
|---|---|
| Health / probes (no auth) | `GET /health`, `/livez`, `/readyz`, `/startupz` |
| Prometheus metrics | `GET /metrics` |
| List | `GET /api/containers` |
| Check (pull only) | `POST /api/containers/{name}/check` |
| Update now | `POST /api/containers/{name}/update` |
| Recreate even if unchanged | `POST /api/containers/{name}/update?force=true` |
| List registry tags | `GET /api/containers/{name}/tags` |
| Deploy a specific tag | `POST /api/containers/{name}/deploy?tag=1.2.3` |
| Registry details for one tag | `GET /api/containers/{name}/tags/{tag}` |
| Pin / unpin | `POST /api/containers/{name}/pin?pinned=true\|false` |
| Check / update all | `POST /api/check-all`, `POST /api/update-all` |
| Check / update selected | `POST /api/selected/check`, `POST /api/selected/update` with `{"names": [...]}`; add `"override_cooldown": true, "password": "…"` to skip the cooldown |
| Health monitor | `GET /api/health`, `GET /api/health/history`, `POST /api/health/check` |
| Self-update | `GET /api/self`, `POST /api/self/check`, `POST /api/self/update` |
| Counts for dashboards | `GET /api/public/summary` (no login with `UPDOCK_PUBLIC_API=true`) |
| Logs (filters: `level`, `container`, `event`, `q`, `after`, `since`, `limit`) | `GET /api/logs` |
| Download logs (`fmt=text\|json`, same filters) | `GET /api/logs/download` |
| Image storage vs. limits | `GET /api/disk` |
| Registry logins (list / add `{host,username,password}` / remove) | `GET`, `POST /api/registries`, `DELETE /api/registries/{host}` |
| Live events (SSE: `state`, `log`) | `GET /api/events` |
| Email settings / send test email | `GET /api/notify`, `POST /api/notify/test` |

Webhook example (e.g. from CI after pushing an image), with an API token so scripts don't need the UI password:
```sh
curl -H "Authorization: Bearer $UPDOCK_API_TOKEN" -X POST http://host:8080/api/containers/myapp/update
```

`UPDOCK_API_TOKEN` is accepted as `Authorization: Bearer …` on `/api/*` and `/metrics` only (never for the UI page); a wrong token is rejected rather than falling back to the password. It's an alternative to the password, so set `UPDOCK_PASSWORD` too.

`GET /api/events` is a Server-Sent Events stream of `state` (container status/progress changes) and `log` (new log entries) events. The UI uses it for live updates and only falls back to polling when it can't connect.

## Monitoring

`GET /metrics` serves Prometheus metrics: containers by state, monitored/pinned counts, per-container `update_available`, status and last check/update times, outcome counters (`updock_results_total{result="updated|recreated|rolled_back|failed|check_failed|skipped|cooldown|protected|rate_limited"}`), runs and run failures, last run time/duration, log messages by level, image storage vs. limits, build info, and with the health monitor on `updock_container_unhealthy{container}` and `updock_health_restarts_total`. It uses the UI login (Prometheus `basic_auth`) unless `UPDOCK_METRICS_PUBLIC=true`.

Probes without auth: `/livez` (process up), `/readyz` (Docker reachable, else 503; used by the image's `HEALTHCHECK`), `/startupz` (scheduler started), plus `/health` (version).

## Summary for dashboards

`GET /api/public/summary` returns counts only, no container names: `{"version", "containers", "running", "monitored", "updates_available", "problems", "unhealthy", "is_update_available", "last_run": {"t", "kind", "duration"}, "next_run"}` (cached for 15 s). It needs the usual login or API token unless `UPDOCK_PUBLIC_API=true`, which serves everything under `/api/public/` without login, for widgets such as Homepage's `customapi`:

```yaml
- Updock:
    widget:
      type: customapi
      url: http://updock:8080/api/public/summary
      mappings:
        - { field: updates_available, label: Updates }
        - { field: problems, label: Problems }
```

```yaml
scrape_configs:
  - job_name: updock
    static_configs: [{ targets: ["updock:8080"] }]
    basic_auth: { username: admin, password_file: /etc/prometheus/updock_password }
```
