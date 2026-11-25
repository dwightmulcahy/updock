# Development

```bash
pip install -r requirements-dev.txt   # hashed lock: exact versions only
ruff check .
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

Browser tests drive the real UI with Playwright against an in-memory Docker and registry (no daemon or network needed); CI runs them on every PR:

```bash
pip install -r requirements-e2e.txt
python -m playwright install chromium     # or E2E_CHROME=/path/to/chrome to use a local one
python -m pytest e2e -o addopts=""
```

## Releasing

1. Merge to `main`.
2. `git tag v1.2.3 && git push origin v1.2.3`
3. The Release workflow verifies the tag is on `main`, reruns CI, and publishes
   the multi-architecture image (amd64, arm64, arm/v7) and grouped Conventional Commit release notes.

For the newest stable `v1.2.3` release, the image receives `1.2.3`, `1.2`, `1`,
and `latest`. Older releases publish only their exact version, so rerunning or
backfilling an old tag cannot move a rolling tag backward. Tags containing `-`
(for example `v1.3.0-rc.1`) are prereleases and publish only the exact tag.

Registry: Docker Hub when the `DOCKERHUB_USERNAME` variable and `DOCKERHUB_TOKEN` secret are both set (optional `DOCKERHUB_REPO`, e.g. `user/updock`); otherwise `ghcr.io/dwightmulcahy/updock`, with no setup.
