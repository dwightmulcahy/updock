# Updock

[![CI](https://github.com/dwightmulcahy/updock/actions/workflows/ci.yml/badge.svg)](https://github.com/dwightmulcahy/updock/actions/workflows/ci.yml)
[![Release](https://github.com/dwightmulcahy/updock/actions/workflows/release.yml/badge.svg)](https://github.com/dwightmulcahy/updock/actions/workflows/release.yml)
[![GitHub release](https://img.shields.io/github/v/release/dwightmulcahy/updock?sort=semver)](https://github.com/dwightmulcahy/updock/releases/latest)
[![Dependabot](https://img.shields.io/badge/dependabot-enabled-025e8c?logo=dependabot)](.github/dependabot.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Watchtower-style auto-updater for Docker containers, with a web UI to check, update or recreate any container on demand.

## Quick start

```bash
cp .env.example .env          # set UPDOCK_PASSWORD
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

State-changing requests from browsers must come from the Updock page itself
(same origin, `Sec-Fetch-Site`, and an `X-Updock-Request` header), so another
website can't use your saved login to trigger updates. Scripts and webhooks
(curl, CI) are unaffected. Behind a reverse proxy that rewrites `Host`, list
the public URL in `UPDOCK_ALLOWED_ORIGINS`. Responses also send a strict
Content-Security-Policy, `X-Frame-Options: DENY` and `nosniff`.

## How it works

1. Asks the registry which digest the container's tag (`nginx:latest`, etc.) points to — a single `HEAD` request, which doesn't download anything or count against Docker Hub pull limits — and compares it with the running image. If the registry can't be reached (or `UPDOCK_DIGEST_CHECK=false`), it falls back to pulling.
2. If there is a newer image, it pulls it, stops the container, renames it, creates a replacement with the same config (env, ports, volumes incl. anonymous ones, networks + aliases, labels, restart policy), and starts it.
3. It then verifies the new container: if the image has a healthcheck it must become `healthy` within `UPDOCK_HEALTH_TIMEOUT`; otherwise it must stay running (no exit, no restart loop) for `UPDOCK_STARTUP_GRACE` seconds. The old container is kept (stopped, renamed) until then.
4. On any failure it rolls back to the old container and marks the row **rolled-back** with the reason. Scheduled runs won't retry the same broken image; **Update now** still can.
5. On success it removes the old container and, if `UPDOCK_CLEANUP=true`, the old image (see below).

Containers that share the updated container's network (`network_mode: container:<name>`, or Compose `network_mode: service:<name>`, e.g. apps behind a VPN container) are stopped first and recreated afterwards to join the new container's network. If the update rolls back, they are simply restarted.

Docker Compose projects are handled as a unit: within a project, services are updated after the services they `depends_on`; services with `depends_on: {<svc>: {restart: true}}` are restarted when that service is recreated (as `docker compose` would); and the `com.docker.compose.image` label is updated so a later `docker compose up -d` leaves Updock's containers alone instead of recreating them.

Env vars, cmd, entrypoint, etc. that came from the old image are dropped so the new image's defaults apply; values you set yourself are kept.

### Image cooldown

Set `UPDOCK_COOLDOWN` (e.g. `24h`, `3d12h`) to make scheduled runs wait until a new image is at least that old before applying it. A compromised or broken release then has time to be noticed and pulled before it reaches you. The age comes from the image's build date, read from the registry without downloading the image; images with no (or a future) build date are not applied automatically. The row shows **cooldown** and when the update will be applied. Override per container with the label `updock.cooldown=12h` (`0` disables it for that container). **Update now** and version deploys ignore the cooldown.

Caveats: the build date isn't the push date, and it can be faked, so this is defence in depth, not a guarantee. Security fixes wait too.

### Versions

For every container the UI shows the **running** image and, after a check, the **latest available** image (read from the registry without pulling it): version label (`org.opencontainers.image.version`, falling back to `org.label-schema.version` / `build_version`), image ID, registry digest, VCS revision, and build date.

**Versions…** lists the tags published for the container's image (selecting one shows its version, digest, image ID and build date from the registry) (Docker Hub, GHCR, Quay, LSCR or any v2 registry; private ones via the mounted `config.json`), newest version first. Deploying a tag recreates the container from `repo:<tag>`, so it stays on that tag; deploy `latest` (or the tag you track) to resume normal updates. The previous image is kept after a version deploy so switching back is instant.

### Old image cleanup

After an update to a new image, the superseded image is deleted: any other tags of the same repository still pointing at it are untagged, then the image is removed. It is kept (and the log says why) if another container — running or stopped — still uses it, or if a different repository also tags it.

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
| `UPDOCK_NOTIFY_EVENTS` | `update,rollback,error,available` | Also: `deploy` |
| `UPDOCK_INSTANCE` | host name | Name shown in subjects (handy with several hosts) |
| `UPDOCK_URL` | empty | Link to the UI included in emails |

## Persistence

Container state (last check, latest-image details, rollbacks, failed images, notification markers) and the log history are kept in `/data`, which both Compose files mount as the `updock-data` volume. After a restart the UI shows what it showed before; jobs that were interrupted mid-way are marked as such. Logs rotate at 5 MB (two backups). Without a writable `/data`, Updock still works but forgets everything on restart (and logs a warning).

## Logging

Every action is logged with its level, container and event type (`check`, `pull`, `update`, `recreate`, `deploy`, `rollback`, `cleanup`, `run`, `error`, …). The **Logs** panel in the UI filters by minimum level, container, event and free text (with highlighting), can pause live updates, expands to full height, and downloads the current view as `.log` or `.json`. Each container row has a **Logs** button that filters to it. Unexpected errors include a collapsible traceback.

The same entries go to stdout (`docker logs updock`) as text or JSON lines.

## UI / API

| Action | Endpoint |
|---|---|
| Health (no auth) | `GET /health` |
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
| Email settings / send test email | `GET /api/notify`, `POST /api/notify/test` |

Webhook example (e.g. from CI after pushing an image):
```sh
curl -u admin:changeme -X POST http://host:8080/api/containers/myapp/update
```

## Config

| Var | Default | |
|---|---|---|
| `UPDOCK_SCHEDULE` | empty | Cron expression for scheduled runs, e.g. `30 4 * * *` or `@daily` (overrides `UPDOCK_INTERVAL`) |
| `UPDOCK_INTERVAL` | `86400` | Seconds between scheduled runs when no cron is set; `0` disables the schedule |
| `UPDOCK_WINDOW` | empty | Maintenance window like `02:00-05:00` (may cross midnight): scheduled runs outside it only check |
| `TZ` | `UTC` | Time zone for the cron expression and window, e.g. `America/Costa_Rica` |
| `UPDOCK_AUTO_UPDATE` | `true` | `false` = scheduled runs only check |
| `UPDOCK_CLEANUP` | `true` | Delete the superseded image after an update |
| `UPDOCK_INCLUDE_STOPPED` | `false` | Let scheduled runs update stopped containers too (they stay stopped) |
| `UPDOCK_LABEL_ENABLE` | `false` | `true` = only containers with label `updock.enable=true` |
| `UPDOCK_RUN_ON_START` | `false` | Run once at startup |
| `UPDOCK_USER` / `UPDOCK_PASSWORD` | `admin` / empty | Basic auth; empty password disables auth |
| `UPDOCK_COOLDOWN` | `0` | Minimum image age before scheduled runs apply it (`90`, `30m`, `24h`, `3d12h`, `2w`, `1M`) |
| `UPDOCK_DIGEST_CHECK` | `true` | Check the registry digest before pulling; `false` = always pull to check |
| `UPDOCK_HEALTH_TIMEOUT` | `120` | Seconds to wait for a healthcheck to report `healthy` |
| `UPDOCK_STARTUP_GRACE` | `10` | Seconds a container without a healthcheck must stay up; `0` = only check it started |
| `UPDOCK_ALLOWED_ORIGINS` | empty | Extra allowed browser origins, comma-separated (e.g. `https://updock.example.com`) |
| `UPDOCK_DATA_DIR` | `/data` | Where state and log history are stored |
| `UPDOCK_LOG_LEVEL` | `INFO` | `DEBUG` adds pull timings and each recreate step |
| `UPDOCK_LOG_FORMAT` | `text` | `json` for one JSON object per line on stdout |
| `UPDOCK_LOG_BUFFER` | `2000` | Entries kept for the UI |

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
