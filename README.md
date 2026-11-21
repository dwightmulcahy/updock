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

## How it works

1. Pulls each container's image tag (`nginx:latest`, etc.).
2. If the pulled image ID differs from the running one, it stops the container, renames it, creates a replacement with the same config (env, ports, volumes incl. anonymous ones, networks + aliases, labels, restart policy), and starts it.
3. On failure it rolls back to the old container. On success it removes the old container and, if `UPDOCK_CLEANUP=true`, the old image.

Env vars, cmd, entrypoint, etc. that came from the old image are dropped so the new image's defaults apply; values you set yourself are kept.

## UI / API

| Action | Endpoint |
|---|---|
| Health (no auth) | `GET /health` |
| List | `GET /api/containers` |
| Check (pull only) | `POST /api/containers/{name}/check` |
| Update now | `POST /api/containers/{name}/update` |
| Recreate even if unchanged | `POST /api/containers/{name}/update?force=true` |
| Check / update all | `POST /api/check-all`, `POST /api/update-all` |
| Logs | `GET /api/logs` |

Webhook example (e.g. from CI after pushing an image):
```sh
curl -u admin:changeme -X POST http://host:8080/api/containers/myapp/update
```

## Config

| Var | Default | |
|---|---|---|
| `UPDOCK_INTERVAL` | `86400` | Seconds between scheduled runs; `0` disables the schedule |
| `UPDOCK_AUTO_UPDATE` | `true` | `false` = scheduled runs only check |
| `UPDOCK_CLEANUP` | `true` | Remove old image after update |
| `UPDOCK_LABEL_ENABLE` | `false` | `true` = only containers with label `updock.enable=true` |
| `UPDOCK_RUN_ON_START` | `false` | Run once at startup |
| `UPDOCK_USER` / `UPDOCK_PASSWORD` | `admin` / empty | Basic auth; empty password disables auth |

Exclude a container: label `updock.enable=false`. Images pinned by digest are skipped. Updock never updates itself.

Private registries: mount `~/.docker/config.json` to `/config/config.json` (plain `auth` entries only; credential helpers aren't supported).

## Development

```bash
pip install -r requirements-dev.txt
ruff check . --select F && pyflakes app scripts tests
pytest                     # fails if coverage < 90%
```

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
