# Running Updock behind a Docker socket proxy

By default Updock mounts the Docker socket (`/var/run/docker.sock`) directly. With the
socket proxy, Updock no longer gets the socket: a small proxy container
([linuxserver/socket-proxy](https://github.com/linuxserver/docker-socket-proxy)) mounts it
**read-only**, and Updock sends its Docker requests to that proxy over a private, internal
network. The proxy only passes the parts of the Docker API that Updock needs.

```
 browser ──► updock :8080 ──(internal network)──► socket-proxy :2375 ──► docker.sock (ro)
```

Everything is set up by `examples/compose.socket-proxy.yaml`, an override that you combine with your
normal Compose file.

## 1. Start it

Run both Compose files together:

```bash
# normal setup (builds locally)
docker compose -f compose.yaml -f examples/compose.socket-proxy.yaml up -d

# QNAP / published image
docker compose -f compose.qnap.yaml -f examples/compose.socket-proxy.yaml pull
docker compose -f compose.qnap.yaml -f examples/compose.socket-proxy.yaml up -d
```

Use the same two `-f` flags for **every** later `docker compose` command too (`pull`,
`down`, `logs`, …). To avoid typing them, add this to `.env`:

```dotenv
COMPOSE_FILE=compose.yaml:examples/compose.socket-proxy.yaml
# QNAP: COMPOSE_FILE=compose.qnap.yaml:examples/compose.socket-proxy.yaml
```

After that, a plain `docker compose up -d` uses both files.

## 2. Turn on the optional features you use

These are always enabled on the proxy: `CONTAINERS`, `IMAGES`, `NETWORKS`, `POST` and
`PING`. Two Updock features need proxy settings that are **off by default**. Set them in
`.env`:

```dotenv
SOCKET_PROXY_EXEC=1     # needed if UPDOCK_HOOKS=true (lifecycle hooks)
SOCKET_PROXY_SYSTEM=1   # needed if UPDOCK_DISK_MAX / UPDOCK_DISK_WARN is set
SOCKET_PROXY_TAG=latest # optional: pin the proxy image version
```

Then run `docker compose up -d` again.

| Proxy setting | Needed for |
|---|---|
| `CONTAINERS=1`, `IMAGES=1`, `NETWORKS=1`, `POST=1` | Always: checks, pulls, recreating containers, cleanup |
| `EXEC=1` (`SOCKET_PROXY_EXEC=1`) | Lifecycle hooks (`UPDOCK_HOOKS=true`) |
| `SYSTEM=1` (`SOCKET_PROXY_SYSTEM=1`) | Disk space guard (`UPDOCK_DISK_MAX` / `UPDOCK_DISK_WARN`) |
| `PING=1` (default) | `/readyz` and the image's health check |

## 3. Check it's working

```bash
# no docker.sock in Updock's mounts
docker inspect updock --format '{{range .Mounts}}{{.Source}} {{end}}'

# Updock talks to the proxy
docker exec updock printenv DOCKER_HOST          # tcp://socket-proxy:2375

# Docker is reachable through the proxy
curl -s http://localhost:8080/readyz             # {"status":"ok","docker":true}
```

In the UI, `updock-socket-proxy` shows as **excluded**. That's intentional: it carries the
label `updock.enable=false` because if Updock stopped the proxy during an update, it would
lose its connection to Docker with the proxy down. Update the proxy yourself with
`docker compose pull socket-proxy && docker compose up -d socket-proxy`.

## If something is blocked

When the proxy refuses a request, the error names the setting to turn on. It appears in
the container row's message and in the logs:

```
blocked by the Docker socket proxy; enable SYSTEM=1 (GET /system/df)
```

- `EXEC` or `SYSTEM`: set `SOCKET_PROXY_EXEC=1` / `SOCKET_PROXY_SYSTEM=1` in `.env`.
- Anything else: add it to the `environment:` section of the `socket-proxy` service in
  `examples/compose.socket-proxy.yaml`.

Then run `docker compose up -d` again.

Other common problems:

| Symptom | Cause / fix |
|---|---|
| `/readyz` returns 503, logs show connection errors | The proxy isn't running, or Updock isn't on the `docker-api` network. Check `docker compose ps` and that you used both `-f` files. |
| `docker.sock` still appears in Updock's mounts | The override wasn't applied: run `up -d` with both files (or set `COMPOSE_FILE`). Needs Docker Compose 2.24+ for `!override`. |
| Hooks never run | `UPDOCK_HOOKS=true` and `SOCKET_PROXY_EXEC=1` are both needed. |
| Header shows no image storage | The disk guard needs `SOCKET_PROXY_SYSTEM=1`. |

## Without Compose

```bash
docker network create docker-api

docker run -d --name socket-proxy --network docker-api --restart unless-stopped \
  --label updock.enable=false \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -e CONTAINERS=1 -e IMAGES=1 -e NETWORKS=1 -e POST=1 \
  lscr.io/linuxserver/socket-proxy:latest

docker run -d --name updock --network docker-api -p 8080:8080 --restart unless-stopped \
  -e DOCKER_HOST=tcp://socket-proxy:2375 \
  ...your other -e / -v options (no docker.sock)... \
  ghcr.io/dwightmulcahy/updock:latest
```

Add `-e EXEC=1` and/or `-e SYSTEM=1` to the proxy if you use hooks or the disk space
guard. This network isn't marked internal like the Compose one, so **don't publish the
proxy's port 2375**.

## What it protects, and what it doesn't

**It does:**
- Block the parts of the Docker API Updock never uses: volumes, builds, swarm, secrets,
  plugins, system-wide changes.
- Keep the raw Docker socket out of the container that serves the web UI.
- Keep the proxy unreachable from outside: in the Compose setup it sits only on an
  internal network.

**It doesn't make Updock unprivileged.** Updock still has to create containers, and
anything that can create containers can create a privileged one. Treat Updock as
root-equivalent on the host either way:

- Keep the UI password set (`UPDOCK_PASSWORD_FILE`, see [secrets/README.md](../secrets/README.md)).
- Don't publish port 8080 to the internet.
