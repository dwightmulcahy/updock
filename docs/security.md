# Security

## Docker socket proxy

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

Step-by-step guide, checks and troubleshooting: [socket-proxy.md](socket-proxy.md).

If the proxy blocks a request, the error says which setting to enable (e.g. `blocked by the Docker socket proxy; enable SYSTEM=1`).

Be realistic about what this buys: Updock still needs to create containers, and anything that may create containers can create a privileged one, so the proxy narrows the API surface and keeps the raw socket out of a web-facing container, but it does not make Updock unprivileged.

## Browser request protection

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

`scripts/init-secrets.sh` creates all three (random password and token, empty SMTP password; never overwrites). An empty file means "not set"; a `*_FILE` pointing at a missing file stops Updock at startup rather than running without a password. Any `UPDOCK_<NAME>_FILE` overrides `UPDOCK_<NAME>`, and plain values still work. See [secrets/README.md](../secrets/README.md).

## Sign-in through a reverse proxy (Authelia, Authentik, …)

If Updock sits behind a proxy that already signs users in, let the proxy's user header replace the password:

```env
UPDOCK_AUTH_HEADER=Remote-User              # Authelia; Authentik: X-authentik-username
UPDOCK_TRUSTED_PROXIES=172.18.0.10          # the proxy's address(es) as Updock sees them; CIDRs allowed
UPDOCK_AUTH_USERS=alice,bob                 # optional allowlist
```

The header is only believed on requests that come from a trusted proxy address; from there, a missing or non-allowed user gets 401. Requests that bypass the proxy still need `UPDOCK_PASSWORD` — and if no password is set, they are refused, so publishing port 8080 directly can't skip the proxy's login. Without `UPDOCK_TRUSTED_PROXIES` the header is ignored (and an error is logged). The signed-in user is shown in the header. Probes, the API token and public metrics work as before. Make sure the proxy strips the header from incoming requests (Authelia and Authentik do).

## Signature checks (cosign)

Opt in per container to require a valid [cosign](https://github.com/sigstore/cosign) signature before a new image replaces the running one. The check runs on the exact digest that was pulled, after the pull and before anything is stopped; a failure leaves the container untouched, shows **unverified**, sends an error email, and scheduled runs skip that digest until you press **Update now**. Version deploys are checked too; manual rollbacks (to an image that already ran) are not.

| Label | Meaning |
|---|---|
| `updock.cosign.key=/keys/app.pub` | Signed with this public key (mount it into the Updock container, e.g. `./keys:/keys:ro`). KMS URIs work as well |
| `updock.cosign.identity=^https://github.com/me/app/` + `updock.cosign.issuer=^https://token\.actions\.githubusercontent\.com$` | Keyless (Sigstore) signature whose certificate identity and OIDC issuer match these regular expressions — e.g. images signed from GitHub Actions |

cosign is **not** bundled: the published cosign binaries carry dozens of known HIGH/CRITICAL CVEs in their Go dependencies, which would fail Updock's image scan and add ~100 MB for a feature most setups don't use. Add it when you want signature checks — either a two-line image:

```dockerfile
FROM ghcr.io/dwightmulcahy/updock:latest
COPY --from=ghcr.io/sigstore/cosign/cosign:v3.0.4 /ko-app/cosign /usr/local/bin/cosign
```

or mount a cosign binary from the host (`- /usr/local/bin/cosign:/usr/local/bin/cosign:ro`; another path via `UPDOCK_COSIGN=/path/to/cosign`). Without it, labelled containers show **unverified** ("cosign is not installed") rather than updating unchecked. Keyless checks and the transparency log need outbound HTTPS to Sigstore (`tuf-repo-cdn.sigstore.dev`, `rekor.sigstore.dev`, `fulcio.sigstore.dev`); the trust root is cached in `/data/sigstore`. Private registries use the same logins as pulls.
