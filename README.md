# Updock

[![CI](https://github.com/dwightmulcahy/updock/actions/workflows/ci.yml/badge.svg)](https://github.com/dwightmulcahy/updock/actions/workflows/ci.yml)
[![Release](https://github.com/dwightmulcahy/updock/actions/workflows/release.yml/badge.svg)](https://github.com/dwightmulcahy/updock/actions/workflows/release.yml)
[![GitHub release](https://img.shields.io/github/v/release/dwightmulcahy/updock?sort=semver)](https://github.com/dwightmulcahy/updock/releases/latest)
[![Dependabot](https://img.shields.io/badge/dependabot-enabled-025e8c?logo=dependabot)](.github/dependabot.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Watchtower-style auto-updater for Docker containers, with a web UI to check, update or recreate any container on demand.

## Quick start

```bash
cp .env.example .env
./scripts/init-secrets.sh     # random UI password + API token in ./secrets (prints the login)
docker compose up -d --build
curl http://localhost:8080/health
# UI: http://localhost:8080
```

Published image: `ghcr.io/dwightmulcahy/updock`.

QNAP: `docker compose -f compose.qnap.yaml pull && docker compose -f compose.qnap.yaml up -d`

Both Compose files run with a read-only root filesystem, all capabilities
dropped, `no-new-privileges`, bounded memory/PIDs, an init process, a graceful
shutdown window, and rotated JSON logs. The process runs as root inside the
container only so it can reach the mounted Docker socket.

> [!WARNING]
> Access to the Docker socket is equivalent to root on the host. Always set
> `UPDOCK_PASSWORD` and don't expose port 8080 publicly.

### Docker socket proxy

Instead of mounting the Docker socket into Updock, you can put a [socket proxy](https://github.com/linuxserver/docker-socket-proxy) in front of it:

```bash
docker compose -f compose.yaml -f compose.socket-proxy.yaml up -d
```

Only the proxy mounts the socket (read-only); Updock talks to it over an internal network (`DOCKER_HOST=tcp://socket-proxy:2375`), and the proxy refuses every API section Updock doesn't need — volumes, builds, swarm, secrets, plugins, system-wide changes. The proxy is labelled `updock.enable=false` so Updock never stops the proxy it depends on.

| Proxy setting | Needed for |
|---|---|
| `CONTAINERS=1`, `IMAGES=1`, `NETWORKS=1`, `POST=1` | always (checks, pulls, recreating containers, cleanup) |
| `EXEC=1` | lifecycle hooks (`SOCKET_PROXY_EXEC=1` in `.env`) |
| `SYSTEM=1` | disk space guard (`SOCKET_PROXY_SYSTEM=1` in `.env`) |
| `PING=1` (default) | `/readyz` |

If the proxy blocks a request, the error says which setting to enable (e.g. `blocked by the Docker socket proxy; enable SYSTEM=1`).

Be realistic about what this buys: Updock still needs to create containers, and anything that may create containers can create a privileged one, so the proxy narrows the API surface and keeps the raw socket out of a web-facing container, but it does not make Updock unprivileged.

State-changing requests from browsers must come from the Updock page itself
(same origin, `Sec-Fetch-Site`, and an `X-Updock-Request` header), so another
website can't use your saved login to trigger updates. Scripts and webhooks
(curl, CI) are unaffected. Behind a reverse proxy that rewrites `Host`, list
the public URL in `UPDOCK_ALLOWED_ORIGINS`. Responses also send a strict
Content-Security-Policy, `X-Frame-Options: DENY` and `nosniff`.

## Secrets

Passwords and tokens are kept out of `.env`: they live as files in `./secrets` (git-ignored), Compose mounts them at `/run/secrets/<name>`, and `.env` points Updock at them:

```dotenv
UPDOCK_PASSWORD_FILE=/run/secrets/updock_password
UPDOCK_API_TOKEN_FILE=/run/secrets/updock_api_token
UPDOCK_SMTP_PASSWORD_FILE=/run/secrets/updock_smtp_password
```

`scripts/init-secrets.sh` creates all three (random password and token, empty SMTP password; never overwrites). An empty file means "not set"; a `*_FILE` pointing at a missing file stops Updock at startup rather than running without a password. Any `UPDOCK_<NAME>_FILE` overrides `UPDOCK_<NAME>`, and plain values still work. See [secrets/README.md](secrets/README.md).

## How it works

1. Asks the registry which digest the container's tag (`nginx:latest`, etc.) points to — a single `HEAD` request, which doesn't download anything or count against Docker Hub pull limits — and compares it with the running image. If the registry can't be reached (or `UPDOCK_DIGEST_CHECK=false`), it falls back to pulling.
2. If there is a newer image, it pulls it, stops the container, renames it, creates a replacement with the same config (env, ports, volumes incl. anonymous ones, networks + aliases, labels, restart policy), and starts it.
3. It then verifies the new container: if the image has a healthcheck it must become `healthy` within `UPDOCK_HEALTH_TIMEOUT`; otherwise it must stay running (no exit, no restart loop) for `UPDOCK_STARTUP_GRACE` seconds. The old container is kept (stopped, renamed) until then.
4. On any failure it rolls back to the old container and marks the row **rolled-back** with the reason. Scheduled runs won't retry the same broken image; **Update now** still can.
5. On success it removes the old container and, if `UPDOCK_CLEANUP=true`, the old image (see below).

Containers that share the updated container's network (`network_mode: container:<name>`, or Compose `network_mode: service:<name>`, e.g. apps behind a VPN container) are stopped first and recreated afterwards to join the new container's network. If the update rolls back, they are simply restarted.

Files that exist only in the container's writable layer are lost on any recreate — notably Compose `configs:` with `content:` or `environment:` (and anything an app wrote outside a volume). List them in the label `updock.copy-file` (comma-separated absolute paths) and Updock copies each one into the replacement before it starts, keeping owner and permissions:

```yaml
labels:
  updock.copy-file: "/etc/app/config.yml,/etc/app/extra.conf"
```

Paths on volumes or bind mounts are skipped (they survive anyway), as are missing files and containers with a read-only root filesystem (logged). Directories, symlinks, files over 1 MiB (16 MiB total) or paths with `..` abort that container's update before it's stopped; a failed copy after stopping rolls back to the old container.

Docker Compose projects are handled as a unit: within a project, services are updated after the services they `depends_on`; services with `depends_on: {<svc>: {restart: true}}` are restarted when that service is recreated (as `docker compose` would); and the `com.docker.compose.image` label is updated so a later `docker compose up -d` leaves Updock's containers alone instead of recreating them.

Env vars, cmd, entrypoint, etc. that came from the old image are dropped so the new image's defaults apply; values you set yourself are kept.

### Lifecycle hooks

With `UPDOCK_HOOKS=true`, containers can declare commands that Updock runs inside them (via `docker exec`) around updates:

| Label | Runs | On failure |
|---|---|---|
| `updock.hook.pre-check` | before each check | logged, continue |
| `updock.hook.pre-update` | before the old container is stopped | exit `75` skips this update (try again next run); any other non-zero exit aborts it |
| `updock.hook.post-update` | after the new container passed its health check | logged |
| `updock.hook.post-check` | after each check/update, even if it failed | logged |

Values run with `sh -c`, or give a JSON array (`["/app/backup","--fast"]`) for images without a shell. Each hook has a `-timeout` label (default `60s`, `0` = none), e.g. `updock.hook.pre-update-timeout=10m`. Hooks only run in running containers, and their exit code and output tail go to the log (`hook` events).

```yaml
labels:
  updock.hook.pre-update: "pg_dump -U app app > /backups/before-update.sql"
  updock.hook.pre-update-timeout: "10m"
```

### Disk space guard

Set `UPDOCK_DISK_MAX` (e.g. `40GB`) and/or `UPDOCK_DISK_WARN` (a size, or a percentage of the max such as `80%`). At the start of each scheduled run (and **Update all**), Updock reads Docker's image storage (`docker system df`). At or over the max, the run only checks — nothing new is downloaded — and you get a `disk` email; over the warning level it logs a warning and emails once. If the usage can't be read, updates are skipped (fails closed). The header shows current image storage. This only covers Docker images, not volumes, logs or free space on the host; pair it with `UPDOCK_CLEANUP=true`.

### Release notes links

When an image says where its source lives (`org.opencontainers.image.source`, or the older `org.label-schema.vcs-url` / `…url` labels), the running and latest-available cells, the version picker and update/available emails link to its release notes: GitHub's releases page searched for that version (works whether tags are `v1.2` or `1.2`), GitLab's releases page, or the source URL elsewhere. Set a container label to point somewhere else, with `{version}` / `{source}` placeholders:

```yaml
labels:
  updock.changelog-url: "https://example.com/app/changelog#{version}"
```

Only `http(s)` links are ever shown.

### Image cooldown

Set `UPDOCK_COOLDOWN` (e.g. `24h`, `3d12h`) to make scheduled runs wait until a new image is at least that old before applying it. A compromised or broken release then has time to be noticed and pulled before it reaches you. The age comes from the image's build date, read from the registry without downloading the image; images with no (or a future) build date are not applied automatically. The row shows **cooldown** and when the update will be applied. Override per container with the label `updock.cooldown=12h` (`0` disables it for that container). **Update now** and version deploys ignore the cooldown.

Caveats: the build date isn't the push date, and it can be faked, so this is defence in depth, not a guarantee. Security fixes wait too.

### Versions

For every container the UI shows the **running** image and, after a check, the **latest available** image (read from the registry without pulling it): version label (`org.opencontainers.image.version`, falling back to `org.label-schema.version` / `build_version`), image ID, registry digest, VCS revision, and build date.

**Versions…** lists the tags published for the container's image (selecting one shows its version, digest, image ID and build date from the registry) (Docker Hub, GHCR, Quay, LSCR or any v2 registry; private ones via the mounted `config.json`), newest version first. Deploying a tag recreates the container from `repo:<tag>`, so it stays on that tag; deploy `latest` (or the tag you track) to resume normal updates. The previous image is kept after a version deploy so switching back is instant.

### Old image cleanup

After an update to a new image, the superseded image is deleted: any other tags of the same repository still pointing at it are untagged, then the image is removed. It is kept (and the log says why) if another container — running or stopped — still uses it, or if a different repository also tags it.

## Monitoring

`GET /metrics` serves Prometheus metrics: containers by state, monitored/pinned counts, per-container `update_available`, status and last check/update times, outcome counters (`updock_results_total{result="updated|recreated|rolled_back|failed|check_failed|skipped|cooldown|rate_limited"}`), runs and run failures, last run time/duration, log messages by level, image storage vs. limits, and build info. It uses the UI login (Prometheus `basic_auth`) unless `UPDOCK_METRICS_PUBLIC=true`.

Probes without auth: `/livez` (process up), `/readyz` (Docker reachable, else 503; used by the image's `HEALTHCHECK`), `/startupz` (scheduler started), plus `/health` (version).

```yaml
scrape_configs:
  - job_name: updock
    static_configs: [{ targets: ["updock:8080"] }]
    basic_auth: { username: admin, password_file: /etc/prometheus/updock_password }
```

## Email notifications

Set `UPDOCK_SMTP_HOST` and `UPDOCK_SMTP_TO` to get emails for updates, rollbacks, failed updates and (in check-only mode) newly available images. Everything that happens during one scheduled run arrives as a single email; manual actions email immediately. "Update available" is sent once per new image, not every run. Use **Test email** in the header to check the settings; every send (or failure) is in the logs as a `notify` event.

| Var | Default | |
|---|---|---|
| `UPDOCK_SMTP_HOST` | empty | SMTP server; empty disables email |
| `UPDOCK_SMTP_PORT` | `587` | `465` for SSL |
| `UPDOCK_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none` |
| `UPDOCK_SMTP_USER` / `UPDOCK_SMTP_PASSWORD` | empty | Login, if the server needs one |
| `UPDOCK_SMTP_FROM` | user or `updock@<host>` | Sender address |
| `UPDOCK_SMTP_TO` | empty | Recipients, comma-separated |
| `UPDOCK_NOTIFY_EVENTS` | `update,rollback,error,available,disk` | Also: `deploy` |
| `UPDOCK_INSTANCE` | host name | Name shown in subjects (handy with several hosts) |
| `UPDOCK_URL` | empty | Link to the UI included in emails |

## Registry rate limits

When a registry answers `429 Too Many Requests` (or a pull fails with Docker Hub's `toomanyrequests`), Updock pauses **all** requests to that registry: 1 minute, doubling on each further 429 up to 6 hours, or the registry's `Retry-After` if that's longer. Containers on that registry show **rate limited** with the next try time; other registries carry on. A 429 during a digest check never falls back to pulling (which would spend pull quota too). The first successful request clears the backoff. Scheduled runs also start at a random point within `UPDOCK_JITTER` (default `1m`) after their time, so many installations don't all hit registries at exactly 04:00:00.

## Persistence

Container state (last check, latest-image details, rollbacks, failed images, notification markers) and the log history are kept in `/data`, which both Compose files mount as the `updock-data` volume. After a restart the UI shows what it showed before; jobs that were interrupted mid-way are marked as such. Logs rotate at 5 MB (two backups). Without a writable `/data`, Updock still works but forgets everything on restart (and logs a warning).

## Logging

Every action is logged with its level, container and event type (`check`, `pull`, `update`, `recreate`, `deploy`, `rollback`, `cleanup`, `run`, `error`, …). The **Logs** panel in the UI filters by minimum level, container, event and free text (with highlighting), can pause live updates, expands to full height, and downloads the current view as `.log` or `.json`. Each container row has a **Logs** button that filters to it. Unexpected errors include a collapsible traceback.

The same entries go to stdout (`docker logs updock`) as text or JSON lines.

## UI / API

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
| Logs (filters: `level`, `container`, `event`, `q`, `after`, `since`, `limit`) | `GET /api/logs` |
| Download logs (`fmt=text\|json`, same filters) | `GET /api/logs/download` |
| Image storage vs. limits | `GET /api/disk` |
| Live events (SSE: `state`, `log`) | `GET /api/events` |
| Email settings / send test email | `GET /api/notify`, `POST /api/notify/test` |

Webhook example (e.g. from CI after pushing an image), with an API token so scripts don't need the UI password:
```sh
curl -H "Authorization: Bearer $UPDOCK_API_TOKEN" -X POST http://host:8080/api/containers/myapp/update
```

`UPDOCK_API_TOKEN` is accepted as `Authorization: Bearer …` on `/api/*` and `/metrics` only (never for the UI page); a wrong token is rejected rather than falling back to the password. It's an alternative to the password, so set `UPDOCK_PASSWORD` too.

`GET /api/events` is a Server-Sent Events stream of `state` (container status/progress changes) and `log` (new log entries) events. The UI uses it for live updates and only falls back to polling when it can't connect.

## Config

| Var | Default | |
|---|---|---|
| `UPDOCK_SCHEDULE` | empty | Cron expression for scheduled runs, e.g. `30 4 * * *` or `@daily` (overrides `UPDOCK_INTERVAL`) |
| `UPDOCK_INTERVAL` | `86400` | Seconds between scheduled runs when no cron is set; `0` disables the schedule |
| `UPDOCK_JITTER` | `1m` | Random delay (0 to this) added to each scheduled run; `0` disables |
| `UPDOCK_WINDOW` | empty | Maintenance window like `02:00-05:00` (may cross midnight): scheduled runs outside it only check |
| `TZ` | `UTC` | Time zone for the cron expression and window, e.g. `America/Costa_Rica` |
| `UPDOCK_AUTO_UPDATE` | `true` | `false` = scheduled runs only check |
| `UPDOCK_CLEANUP` | `true` | Delete the superseded image after an update |
| `UPDOCK_INCLUDE_STOPPED` | `false` | Let scheduled runs update stopped containers too (they stay stopped) |
| `UPDOCK_EXCLUDE` | empty | Container names to leave alone (globs, comma-separated) |
| `UPDOCK_INCLUDE_IMAGES` / `UPDOCK_EXCLUDE_IMAGES` | empty | Only / never manage containers whose image matches (globs) |
| `UPDOCK_SCOPE` | empty | Manage only containers labelled `updock.scope=<this>`; empty = only unscoped ones |
| `UPDOCK_LABEL_ENABLE` | `false` | `true` = only containers with label `updock.enable=true` |
| `UPDOCK_RUN_ON_START` | `false` | Run once at startup |
| `UPDOCK_USER` / `UPDOCK_PASSWORD` | `admin` / empty | Basic auth; empty password disables auth. Prefer `UPDOCK_PASSWORD_FILE` (see Secrets) |
| `UPDOCK_DISK_MAX` | empty | Image storage limit (`40GB`, `20GiB`); at/over it scheduled runs only check |
| `UPDOCK_DISK_WARN` | empty | Warning level: a size or a percentage of the max (`80%`) |
| `UPDOCK_HOOKS` | `false` | Run `updock.hook.*` lifecycle hooks from container labels |
| `UPDOCK_COOLDOWN` | `0` | Minimum image age before scheduled runs apply it (`90`, `30m`, `24h`, `3d12h`, `2w`, `1M`) |
| `UPDOCK_DIGEST_CHECK` | `true` | Check the registry digest before pulling; `false` = always pull to check |
| `UPDOCK_HEALTH_TIMEOUT` | `120` | Seconds to wait for a healthcheck to report `healthy` |
| `UPDOCK_STARTUP_GRACE` | `10` | Seconds a container without a healthcheck must stay up; `0` = only check it started |
| `UPDOCK_API_TOKEN` | empty | Bearer token for `/api/*` and `/metrics` (scripts, webhooks, Prometheus) |
| `UPDOCK_ALLOWED_ORIGINS` | empty | Extra allowed browser origins, comma-separated (e.g. `https://updock.example.com`) |
| `UPDOCK_DATA_DIR` | `/data` | Where state and log history are stored |
| `UPDOCK_METRICS_PUBLIC` | `false` | Serve `/metrics` without login |
| `UPDOCK_LOG_LEVEL` | `INFO` | `DEBUG` adds pull timings and each recreate step |
| `UPDOCK_LOG_FORMAT` | `text` | `json` for one JSON object per line on stdout |
| `UPDOCK_LOG_BUFFER` | `2000` | Entries kept for the UI |

### Choosing containers

- Exclude one container: label `updock.enable=false`; or opt in instead with `UPDOCK_LABEL_ENABLE=true` + `updock.enable=true`.
- `UPDOCK_EXCLUDE`: container names to skip, comma-separated globs (`db-*,backup`).
- `UPDOCK_INCLUDE_IMAGES` / `UPDOCK_EXCLUDE_IMAGES`: only / never manage containers whose image matches a glob (`ghcr.io/acme/*`, `*:pinned`).
- `updock.monitor-only=true` label: scheduled runs only check it (same as **Pin**, but declared in Compose).
- Several Updock instances on one host: give each `UPDOCK_SCOPE=<name>` and label its containers `updock.scope=<name>`. An instance without a scope ignores containers that have one.

The **excluded** badge's tooltip says which rule applies.

Exclude a container: label `updock.enable=false`. **Pin** a container in the UI to keep it on its current image: scheduled runs still check it (and email if something new is available) but never update it; **Update now** still works. Stopped containers are listed too; scheduled runs update them only with `UPDOCK_INCLUDE_STOPPED=true`. Pulls show a progress bar. Images pinned by digest are skipped. Updock never updates itself.

Private registries: mount `~/.docker/config.json` to `/config/config.json`. Inline `auths` entries work as-is; for `credHelpers` / `credsStore`, the matching `docker-credential-<name>` binary must also be available inside the container (mount it onto the `PATH`, e.g. `/usr/local/bin`).

## Development

```bash
pip install -r requirements-dev.txt   # hashed lock: exact versions only
ruff check . --select F && pyflakes app scripts tests
pytest                     # fails if coverage < 90%
```

Dependencies are declared in `requirements.in` / `requirements-dev.in` and locked
with hashes in `requirements.txt` / `requirements-dev.txt`. After editing a `.in`
file, run `pip-compile --generate-hashes --strip-extras --allow-unsafe <file>.in`.
The image installs with `--require-hashes`, so builds are reproducible and a
tampered package fails the build. Dependabot updates both files, grouped into one
runtime PR and one dev PR per week.

CI pins every external GitHub Action to an immutable commit SHA, pins the
Docker base image by version and multi-architecture digest, smoke-tests the
hardened image against a real Docker daemon (including an end-to-end
recreate), and rejects fixable high or critical image vulnerabilities with
Trivy. Dependabot keeps those pins reviewable and current.

## Releasing

1. Merge to `main`.
2. `git tag v1.2.3 && git push origin v1.2.3`
3. The Release workflow verifies the tag is on `main`, reruns CI, and publishes
   the multi-architecture image and grouped Conventional Commit release notes.

For the newest stable `v1.2.3` release, the image receives `1.2.3`, `1.2`, `1`,
and `latest`. Older releases publish only their exact version, so rerunning or
backfilling an old tag cannot move a rolling tag backward. Tags containing `-`
(for example `v1.3.0-rc.1`) are prereleases and publish only the exact tag.

Registry: Docker Hub when the `DOCKERHUB_USERNAME` variable and `DOCKERHUB_TOKEN` secret are both set (optional `DOCKERHUB_REPO`, e.g. `user/updock`); otherwise `ghcr.io/dwightmulcahy/updock`, with no setup.

## License

[MIT](LICENSE)
