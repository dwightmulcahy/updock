# Secrets

Compose mounts each file here into the container at `/run/secrets/<name>`, and
`.env` points Updock at them with `UPDOCK_*_FILE` variables. Everything in this
directory except this README is ignored by git.

| File | Used for | `.env` setting |
|---|---|---|
| `updock_password` | UI / API login password | `UPDOCK_PASSWORD_FILE=/run/secrets/updock_password` |
| `updock_api_token` | Bearer token for scripts, webhooks, Prometheus | `UPDOCK_API_TOKEN_FILE=/run/secrets/updock_api_token` |
| `updock_smtp_password` | SMTP login for email notifications | `UPDOCK_SMTP_PASSWORD_FILE=/run/secrets/updock_smtp_password` |

Create them (random password and token, empty SMTP password) with:

```bash
./scripts/init-secrets.sh
```

or by hand, e.g. `printf '%s' 'my password' > secrets/updock_password && chmod 600 secrets/updock_password`.

- All three files must exist, because Compose refuses to start with a missing secret file.
  An **empty** file means "not set" (e.g. no SMTP login).
- Surrounding whitespace and trailing newlines are ignored.
- Files can stay `chmod 600` for your user: the container is allowed to read them
  (`cap_add: DAC_READ_SEARCH`) even though all other capabilities are dropped.
- If a `*_FILE` points to a file that doesn't exist, Updock refuses to start
  instead of silently running without a password.
