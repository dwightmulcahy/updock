# syntax=docker/dockerfile:1
FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
# Set by the release workflow (tag without "v", commit); empty = "dev" build
ARG APP_VERSION=
ARG GIT_SHA=
ENV APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA} \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# No `apt-get upgrade` here: upgraded files land in a new layer on top of the old ones
# (+28 MB). Security fixes come from bumping the pinned base digest (Dependabot), and
# the CI image scan fails on any fixable HIGH/CRITICAL issue in the meantime.
COPY requirements.txt .
# Exact versions with hashes: reproducible builds, tampered packages are rejected
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt
# Runtime needs no package manager (and its bundled CVEs)
RUN python -m pip uninstall -y pip setuptools wheel
# Files owned by root and the root filesystem is read-only at runtime (see compose.yaml).
# Runs as root only to reach the Docker socket; all capabilities are dropped.
COPY app ./app
# Last so a new date doesn't invalidate the cached dependency layers
ARG BUILD_DATE=
ENV BUILD_DATE=${BUILD_DATE}
# State and log history (mount a volume here)
RUN mkdir -p /data
ENV UPDOCK_DATA_DIR=/data
LABEL updock.self="true"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/readyz').status != 200)"]
# Short graceful-shutdown window: open live-update (SSE) connections never end on their own
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "5"]
