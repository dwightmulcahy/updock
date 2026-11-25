# How it works

1. Asks the registry which digest the container's tag (`nginx:latest`, etc.) points to — a single `HEAD` request, which doesn't download anything or count against Docker Hub pull limits — and compares it with the running image. If the registry can't be reached (or `UPDOCK_DIGEST_CHECK=false`), it falls back to pulling.
2. If there is a newer image, it pulls it, stops the container, renames it, creates a replacement with the same config (env, ports, volumes incl. anonymous ones, networks + aliases, labels, restart policy), and starts it.
3. It then verifies the new container: if the image has a healthcheck it must become `healthy` within `UPDOCK_HEALTH_TIMEOUT` (per container: label `updock.health-timeout=5m`). A temporary `unhealthy` while a slow app starts is tolerated — Docker reports that when an app outlives its healthcheck's start period and flips to `healthy` later — so only the timeout, an exit or a restart loop counts as failure; if the old container was **already** failing its healthcheck before the update (a broken check in the image), the new one only has to stay up, since rolling back to another failing container gains nothing; otherwise it must stay running (no exit, no restart loop) for `UPDOCK_STARTUP_GRACE` seconds. With the label `updock.health-url=http://homenas.local:8111/` Updock also requests that URL after the container is up and requires a 2xx/3xx answer within the health timeout, else it rolls back — useful for images with no (or a broken) healthcheck. Updock makes the request itself, so use an address it can reach: a published port on the host, or the container's name when both share a network. The old container is kept (stopped, renamed) until then.
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

For dependencies Compose can't express (across projects, or plain `docker run` containers), use the label `updock.depends-on` with container names: `updock.depends-on=db,cache` updates `db` and `cache` first in every run; `updock.depends-on=db:restart` also restarts this container whenever `db` is recreated.

Env vars, cmd, entrypoint, etc. that came from the old image are dropped so the new image's defaults apply; values you set yourself are kept.

## Lifecycle hooks

With `UPDOCK_HOOKS=true`, containers can declare commands that Updock runs inside them (via `docker exec`) around updates:

| Label | Runs | On failure |
|---|---|---|
| `updock.hook.pre-check` | before each check | logged, continue |
| `updock.hook.pre-stop` | before the container is stopped: the one being updated, containers sharing its network (`network_mode: container:…`), and containers restarted because they depend on it | update target or network sharer: exit `75` skips, any other non-zero exit aborts the update before anything is stopped; restarted dependent: that restart is skipped |
| `updock.hook.pre-update` | before the old container is stopped (after `pre-stop`) | exit `75` skips this update (try again next run); any other non-zero exit aborts it |
| `updock.hook.post-update` | after the new container passed its health check | logged |
| `updock.hook.pre-rollback` | in the failed new container (if still running), before it's removed | logged; rollback continues |
| `updock.hook.post-rollback` | in the restored old container, after it's started again | logged |
| `updock.hook.post-check` | after each check/update, even if it failed | logged |

Values run with `sh -c`, or give a JSON array (`["/app/backup","--fast"]`) for images without a shell. Each hook has a `-timeout` label (default `60s`, `0` = none), e.g. `updock.hook.pre-update-timeout=10m`. Hooks only run in running containers, and their exit code and output tail go to the log (`hook` events).

```yaml
labels:
  updock.hook.pre-update: "pg_dump -U app app > /backups/before-update.sql"
  updock.hook.pre-update-timeout: "10m"
```

## Health monitor

With `UPDOCK_HEALTH_INTERVAL` (e.g. `3m`), Updock also watches Docker healthchecks between updates. Each pass reads every running container's health from the container list (one request, not one per container); containers without a healthcheck, and containers Updock is updating at that moment, are skipped.

- After `UPDOCK_HEALTH_NOTIFY_AFTER` (default `3`) unhealthy checks in a row you get an `unhealthy` notification, once per episode, and a `healthy` one when it recovers. `0` turns these off.
- With `UPDOCK_HEALTH_RESTART_AFTER=N` (or per container, label `updock.health.restart-after=N`; `0` = never) the container is restarted every N failed checks, at most `UPDOCK_HEALTH_RESTART_ATTEMPTS` (default `3`) times per episode. Protected containers and Updock itself are never restarted.
- Label `updock.health.monitor=false` leaves a container out. `UPDOCK_SCOPE` applies as for updates.
- Status changes, restarts and notifications are kept for `UPDOCK_HEALTH_HISTORY` (default `7d`) in `/data/health.jsonl` and shown on the **Health** tab, with a **Health** badge on the row. API: `GET /api/health`, `GET /api/health/history?container=web&limit=200`, `POST /api/health/check` (run a pass now).

A container recreated under the same name (by an update, or by you) starts with a clean count.

## Self-update

Set `UPDOCK_ALLOW_SELF_UPDATE=true` and the About tab can update Updock itself: **Check for a new Updock**, then **Self-update…** (admin password when one is set). A process can't replace the container it runs in, so Updock pulls the new image and starts a short-lived helper container from it (`<name>-self-update`: same Docker access, data volume, secrets, env and networks; no published ports; removed when done). The helper does a normal update of the Updock container: stop, rename, create the replacement, wait for its healthcheck, roll back to the old container if it fails. The Updock that comes up (the new one, or the old one after a rollback) logs the outcome with the helper's log and sends a `self-update` notification. A self-update is refused while a run or any job is in progress, and runs wait until it's done.

Updock finds its own container through its hostname (the container ID by default). If you set `hostname:` in Compose, also set `UPDOCK_SELF` to the container's name. Scheduled runs never update Updock; use this or `docker compose pull && docker compose up -d`. API: `GET /api/self`, `POST /api/self/check`, `POST /api/self/update` (`{"password": "…"}`).

## Disk space guard

Set `UPDOCK_DISK_MAX` (e.g. `40GB`) and/or `UPDOCK_DISK_WARN` (a size, or a percentage of the max such as `80%`). At the start of each scheduled run (and **Update all**), Updock reads Docker's image storage (`docker system df`). At or over the max, the run only checks — nothing new is downloaded — and you get a `disk` email; over the warning level it logs a warning and emails once. If the usage can't be read, updates are skipped (fails closed). The header shows current image storage. This only covers Docker images, not volumes, logs or free space on the host; pair it with `UPDOCK_CLEANUP=true`.

## Release notes links

When an image says where its source lives (`org.opencontainers.image.source`, or the older `org.label-schema.vcs-url` / `…url` labels), the running and latest-available cells, the version picker and update/available emails link to its release notes: GitHub's releases page searched for that version (works whether tags are `v1.2` or `1.2`), GitLab's releases page, or the source URL elsewhere. Set a container label to point somewhere else, with `{version}` / `{source}` placeholders:

```yaml
labels:
  updock.changelog-url: "https://example.com/app/changelog#{version}"
```

Only `http(s)` links are ever shown.

## Image cooldown

Set `UPDOCK_COOLDOWN` (e.g. `24h`, `3d12h`) to make scheduled runs wait until a new image is at least that old before applying it. A compromised or broken release then has time to be noticed and pulled before it reaches you. The age comes from the image's build date, read from the registry without downloading the image; images with no (or a future) build date are not applied automatically. The row shows **cooldown** and when the update will be applied. Override per container with the label `updock.cooldown=12h` (`0` disables it for that container). The cooldown applies to **Update** too: the row shows ◷ and an **Update…** button that explains how new the image is and asks for confirmation — plus the admin password (`UPDOCK_PASSWORD`) when one is set — before skipping it (`POST /api/containers/{name}/update` with `{"override_cooldown": true, "password": "…"}`; an API token counts as admin). Overrides and refused attempts are logged; after 5 wrong passwords from one address within 5 minutes, further tries get HTTP 429 until the window passes. The password is the one you sign in to Updock with. **Update all** never skips it. Version deploys (an explicit choice of version) ignore the cooldown.

Caveats: the build date isn't the push date, and it can be faked, so this is defence in depth, not a guarantee. Security fixes wait too.

## Versions

For every container the UI shows the **running** image and, after a check, the **latest available** image (read from the registry without pulling it): version label (`org.opencontainers.image.version`, falling back to `org.label-schema.version` / `build_version`), image ID, registry digest, VCS revision, and build date.

**Versions…** lists the tags published for the container's image (selecting one shows its version, digest, image ID and build date from the registry) (Docker Hub, GHCR, Quay, LSCR or any v2 registry; private ones via the mounted `config.json`), newest version first. Deploying a tag recreates the container from `repo:<tag>`, so it stays on that tag; deploy `latest` (or the tag you track) to resume normal updates. The previous image is kept after a version deploy so switching back is instant.

**Roll back to …** (row menu, or `POST /api/containers/{name}/rollback`) returns a container to the image it ran before its last update or version deploy — also after a `latest` update, where there is no older tag to pick. If old-image cleanup already deleted that image, it is pulled back from the registry by its digest. The container is then **pinned** so scheduled runs don't re-apply the newer image; unpin it when a fixed release is out. Rolling back again returns to the newer image.

## Deleting a container

**Delete container…** (row menu) stops and removes a container. You type its name to confirm and, when `UPDOCK_PASSWORD` is set, enter the admin password; you can also delete its image (only if no other container uses it) and its anonymous volumes — named volumes and bind mounts are never touched. Compose-managed containers come back on the next `docker compose up` unless removed from the compose file, and the dialog says so. Updock refuses to delete itself. Every deletion and refused attempt is logged (and emailed if `delete` is in `UPDOCK_NOTIFY_EVENTS`). API: `POST /api/containers/{name}/delete` with `{"remove_image": false, "remove_volumes": false, "password": "…"}` (an API token counts as admin). Turn the feature off with `UPDOCK_ALLOW_DELETE=false`.

## Following version tags (semver)

A container on a version tag (`app:1.4.2`) only gets rebuilds of that same tag. Add the label `updock.semver` to move it to newer version tags instead:

| Label | `1.4.2` may move to | Never to |
|---|---|---|
| `updock.semver=patch` | `1.4.9` | `1.5.0`, `2.0.0` |
| `updock.semver=minor` | `1.9.0` | `2.0.0` |
| `updock.semver=major` | `3.0.0` | — |

The tag keeps its shape: a `v` prefix, the number of parts (`1.4` follows `1.x`, not `1.5.1`) and a suffix (`1.4.2-alpine` only moves to `-alpine` tags; pre-releases such as `-rc1` are never picked for a plain release). When no newer tag is in range, the current tag is still checked for a rebuild. Cooldown, health checks and rollback work as usual; the row shows the new tag. Compose users: `docker compose up` recreates from the tag in the compose file, so bump it there too (or use a range-style tag such as `1.4`).

## Approving bigger version steps

Set `UPDOCK_ALLOW=patch|minor|major` (or the label `updock.allow` per container, which wins) to apply only steps up to that size automatically. With `minor` on `1.2.3`, `1.2.9` and `1.4.0` are applied as usual, but `2.0.0` is held: the row shows ⚑ **needs approval** with an **Approve…** button, and an `approval` email is sent once per new image. Approving asks for the admin password (`UPDOCK_PASSWORD`) when one is set and updates right away; if a cooldown is still running, the approval is kept and the update follows when it ends. API: `POST /api/containers/{name}/update` with `{"approve": true, "password": "…"}`. Versions come from the tags when following tags (`updock.semver`), otherwise from the images' version labels (`org.opencontainers.image.version` …); images with an unknown or older version are held for approval when a policy is configured; rebuilds of the same known version remain eligible. For the specific example of allowing only `1.2.x`, use `patch`. To discover out-of-range tags as approval candidates, combine `updock.semver=major` with `updock.allow=patch` (or `minor`). Unset (the default) means no approval is needed. In the Containers tab, open a container’s ⋯ menu and select **Version update policy…** to choose Patch, Minor, Major, or Inherit. GUI settings override Compose labels, then the global default, and persist in the state volume. Changing a policy requires the admin password when configured and clears previous approvals. For full version tags, a GUI override discovers newer major versions so out-of-policy releases can be presented for approval.

## Old image cleanup

After an update to a new image, the superseded image is deleted: any other tags of the same repository still pointing at it are untagged, then the image is removed. It is kept (and the log says why) if another container — running or stopped — still uses it, or if a different repository also tags it.

## Registry rate limits

When a registry answers `429 Too Many Requests` (or a pull fails with Docker Hub's `toomanyrequests`), Updock pauses **all** requests to that registry: 1 minute, doubling on each further 429 up to 6 hours, or the registry's `Retry-After` if that's longer. Containers on that registry show **rate limited** with the next try time; other registries carry on. A 429 during a digest check never falls back to pulling (which would spend pull quota too). The first successful request clears the backoff. Scheduled runs also start at a random point within `UPDOCK_JITTER` (default `1m`) after their time, so many installations don't all hit registries at exactly 04:00:00.

## Persistence

Container state (last check, latest-image details, rollbacks, failed images, notification markers) and the log history are kept in `/data`, which both Compose files mount as the `updock-data` volume. After a restart the UI shows what it showed before; jobs that were interrupted mid-way are marked as such. Logs rotate at 5 MB (two backups). Without a writable `/data`, Updock still works but forgets everything on restart (and logs a warning).
