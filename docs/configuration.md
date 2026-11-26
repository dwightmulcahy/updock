# Configuration

Values are trimmed (trailing spaces or a Windows line ending in an env file are ignored). An `UPDOCK_*` variable Updock doesn't know is logged at startup as `unknown setting … is ignored`, with the closest known name when one is similar, so a typo doesn't silently leave a feature off.

| Var | Default | |
|---|---|---|
| `UPDOCK_SCHEDULE` | empty | Cron expression for scheduled runs, e.g. `30 4 * * *` or `@daily` (overrides `UPDOCK_INTERVAL`) |
| `UPDOCK_INTERVAL` | `86400` | Seconds between scheduled runs when no cron is set; `0` disables the schedule |
| `UPDOCK_JITTER` | `1m` | Random delay (0 to this) added to each scheduled run; `0` disables |
| `UPDOCK_WINDOW` | empty | Maintenance window like `02:00-05:00` (may cross midnight): scheduled runs outside it only check |
| `TZ` | `UTC` | Time zone for the cron expression and window, e.g. `America/Costa_Rica` |
| `UPDOCK_AUTO_UPDATE` | `true` | `false` = scheduled runs only check |
| `UPDOCK_CLEANUP` | `true` | Delete the superseded image after an update |
| `UPDOCK_INCLUDE_STOPPED` | `false` | Let scheduled runs update stopped containers too (they stay stopped unless revived) |
| `UPDOCK_REVIVE_STOPPED` | `false` | Start a stopped container after its image was updated; label `updock.revive=true\|false` overrides per container |
| `UPDOCK_EXCLUDE` | empty | Container names to leave alone (globs, comma-separated) |
| `UPDOCK_INCLUDE_IMAGES` / `UPDOCK_EXCLUDE_IMAGES` | empty | Only / never manage containers whose image matches (globs) |
| `UPDOCK_SCOPE` | empty | Manage only containers labelled `updock.scope=<this>`; empty = only unscoped ones |
| `UPDOCK_LABEL_ENABLE` | `false` | `true` = only containers with label `updock.enable=true` |
| `UPDOCK_RUN_ON_START` | `false` | Run once at startup |
| `UPDOCK_USER` / `UPDOCK_PASSWORD` | `admin` / empty | Basic auth; empty password disables auth. Prefer `UPDOCK_PASSWORD_FILE` (see [Secrets](security.md#secrets)) |
| `UPDOCK_DISK_MAX` | empty | Image storage limit (`40GB`, `20GiB`); at/over it scheduled runs only check |
| `UPDOCK_DISK_WARN` | empty | Warning level: a size or a percentage of the max (`80%`) |
| `UPDOCK_HOOKS` | `false` | Run `updock.hook.*` lifecycle hooks from container labels |
| `UPDOCK_ALLOW` | *(unset)* | Largest version step applied without approval: `patch`, `minor` or `major` (label `updock.allow` overrides) |
| `UPDOCK_HEALTH_INTERVAL` | `0` | [Health monitor](how-it-works.md#health-monitor) pass every (`3m`, `30s`); `0` = off |
| `UPDOCK_HEALTH_NOTIFY_AFTER` | `3` | Unhealthy checks in a row before an `unhealthy` notification; `0` = never |
| `UPDOCK_HEALTH_RESTART_AFTER` | `0` | Restart after this many failed checks; `0` = never (label `updock.health.restart-after`) |
| `UPDOCK_HEALTH_RESTART_ATTEMPTS` | `3` | Restarts per unhealthy episode |
| `UPDOCK_HEALTH_HISTORY` | `7d` | How long health history is kept |
| `UPDOCK_ALLOW_SELF_UPDATE` | `false` | Allow [self-update](how-it-works.md#self-update) from the About tab / API |
| `UPDOCK_SELF` | hostname | Updock's container name, if you changed its hostname |
| `UPDOCK_PUBLIC_API` | `false` | Serve `/api/public/summary` without login |
| `UPDOCK_INSECURE_REGISTRIES` | empty | Registries reached over plain HTTP (`registry.lan:5000`, comma-separated) |
| `UPDOCK_REGISTRY_CA` | empty | PEM file with extra CA certificates for registry checks |
| `UPDOCK_COOLDOWN` | `0` | Minimum image age before scheduled runs apply it (`90`, `30m`, `24h`, `3d12h`, `2w`, `1M`) |
| `UPDOCK_DIGEST_CHECK` | `true` | Check the registry digest before pulling; `false` = always pull to check |
| `UPDOCK_HEALTH_TIMEOUT` | `120` | Seconds to wait for a healthcheck to report `healthy` |
| `UPDOCK_STARTUP_GRACE` | `10` | Seconds a container without a healthcheck must stay up; `0` = only check it started |
| `UPDOCK_API_TOKEN` | empty | Bearer token for `/api/*` and `/metrics` (scripts, webhooks, Prometheus) |
| `UPDOCK_AUTH_HEADER` / `UPDOCK_TRUSTED_PROXIES` / `UPDOCK_AUTH_USERS` | empty | Reverse-proxy sign-in (see [reverse-proxy sign-in](security.md#sign-in-through-a-reverse-proxy-authelia-authentik-)) |
| `UPDOCK_ALLOW_DELETE` | `true` | Allow **Delete container…** in the UI/API |
| `UPDOCK_ALLOWED_ORIGINS` | empty | Extra allowed browser origins, comma-separated (e.g. `https://updock.example.com`) |
| `UPDOCK_DATA_DIR` | `/data` | Where state and log history are stored |
| `UPDOCK_METRICS_PUBLIC` | `false` | Serve `/metrics` without login |
| `UPDOCK_LOG_LEVEL` | `INFO` | `DEBUG` adds pull timings and each recreate step |
| `UPDOCK_LOG_FORMAT` | `text` | `json` for one JSON object per line on stdout |
| `UPDOCK_LOG_BUFFER` | `2000` | Entries kept for the UI |

## Choosing containers

- Exclude one container: label `updock.enable=false`; or opt in instead with `UPDOCK_LABEL_ENABLE=true` + `updock.enable=true`.
- `UPDOCK_EXCLUDE`: container names to skip, comma-separated globs (`db-*,backup`).
- `UPDOCK_INCLUDE_IMAGES` / `UPDOCK_EXCLUDE_IMAGES`: only / never manage containers whose image matches a glob (`ghcr.io/acme/*`, `*:pinned`).
- `updock.monitor-only=true` label: scheduled runs only check it (same as **Pin**, but declared in Compose).
- Several Updock instances on one host: give each `UPDOCK_SCOPE=<name>` and label its containers `updock.scope=<name>`. An instance without a scope ignores containers that have one.

The **excluded** badge's tooltip says which rule applies.

Protect a container with the label `updock.protected=true` (databases, the socket proxy, anything you only want to hear about): it's checked and shown like any other, and you get "update available" emails, but Updock refuses to stop, update, deploy, roll back or delete it, even from the UI or API (HTTP 409), and the health monitor won't restart it. An update of a container whose network a protected container shares (`network_mode: container:…`) is refused before anything is stopped, and a protected container that depends on a recreated one isn't restarted.

Exclude a container: label `updock.enable=false`. **Pin** a container in the UI to keep it on its current image: scheduled runs still check it (and email if something new is available) but never update it; **Update now** still works. Stopped containers are listed too; scheduled runs update them only with `UPDOCK_INCLUDE_STOPPED=true`, and they stay stopped unless `UPDOCK_REVIVE_STOPPED=true` (or the label `updock.revive=true`) starts them after a new image is applied — health-checked like any other update, and a failed start leaves the original container stopped as it was. Pulls show a progress bar. Images pinned by digest are skipped. Scheduled runs never update Updock itself (see [Self-update](how-it-works.md#self-update)).

Private registries: click **Registries** in the header (or **Add login** on a row that says **auth required**) and enter the registry, username and token — for ghcr.io a GitHub token with `read:packages`, for Docker Hub an access token. Updock checks the login with the registry before saving it, stores it in `/data/registries.json` (mode 600, never shown again), uses it for checks and pulls, and immediately re-checks the containers that were refused. Alternatively mount `~/.docker/config.json` to `/config/config.json`; logins saved in the UI take precedence. Inline `auths` entries work as-is, including the `identitytoken` (OAuth refresh token) that `az acr login` and some other registries write: Updock exchanges it for an access token for checks and passes it to Docker for pulls. For `credHelpers` / `credsStore`, the matching `docker-credential-<name>` binary must also be available inside the container (mount it onto the `PATH`, e.g. `/usr/local/bin`); for AWS ECR that's `docker-credential-ecr-login`.

Registries on your own network:

- Plain HTTP (e.g. `registry.lan:5000`): list them in `UPDOCK_INSECURE_REGISTRIES` (comma-separated `host:port`). Docker itself must allow them too (`insecure-registries` in `daemon.json`), since Docker does the pulls.
- HTTPS with a private CA or a self-signed certificate: mount the CA (PEM) and point `UPDOCK_REGISTRY_CA` at it, e.g. `./ca.pem:/certs/ca.pem:ro` and `UPDOCK_REGISTRY_CA=/certs/ca.pem`. It's added to the system trust store. For pulls, Docker needs it as well (`/etc/docker/certs.d/<host>/ca.crt`).

On Docker Hub, the version list and `updock.semver` read tags from Docker Hub's API newest first, so the newest releases are always among the 300 kept even in repositories with thousands of tags; for private Hub repositories, or if that API fails, the registry's own (alphabetical) tag list is used.
