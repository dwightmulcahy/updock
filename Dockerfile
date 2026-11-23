# syntax=docker/dockerfile:1
# cosign, for the optional image signature checks (labels updock.cosign.*)
FROM ghcr.io/sigstore/cosign/cosign:v3.0.2@sha256:b29487e48205d875c324c79583e2806d9d269c0fa299e0861bbec023d8430c8b AS cosign

FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
# Set by the release workflow (tag without "v", commit); empty = "dev" build
ARG APP_VERSION=
ARG GIT_SHA=
ENV APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA} \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Pick up Debian security fixes released after the pinned base image
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# Exact versions with hashes: reproducible builds, tampered packages are rejected
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt
# Runtime needs no package manager (and its bundled CVEs)
RUN python -m pip uninstall -y pip setuptools wheel
# Files owned by root and the root filesystem is read-only at runtime (see compose.yaml).
# Runs as root only to reach the Docker socket; all capabilities are dropped.
COPY --from=cosign /ko-app/cosign /usr/local/bin/cosign
COPY app ./app
# State and log history (mount a volume here)
RUN mkdir -p /data
ENV UPDOCK_DATA_DIR=/data
LABEL updock.self="true"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/readyz').status != 200)"]
# Short graceful-shutdown window: open live-update (SSE) connections never end on their own
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "5"]
