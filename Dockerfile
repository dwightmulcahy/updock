# syntax=docker/dockerfile:1
# Stage 1: everything is installed and trimmed here, then copied out as one layer, so
# files deleted below really leave the image (in a single-stage build they would still
# sit in the layers underneath).
FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
# Security fixes released after the pinned base image, and only those: a full
# `apt-get upgrade` rewrote ~28 MB of packages. Regular updates come from bumping the
# base digest.
RUN apt-get update && \
    pkgs="$(apt list --upgradable 2>/dev/null | grep -- '-security' | cut -d/ -f1 | tr '\n' ' ')" && \
    if [ -n "$pkgs" ]; then apt-get install -y --only-upgrade --no-install-recommends $pkgs; fi && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/cache/debconf/*-old /var/log/*
COPY requirements.txt /tmp/
# Exact versions with hashes: reproducible builds, tampered packages are rejected.
# --no-compile: no .pyc files (~9 MB); the root filesystem is read-only at runtime, so
# Python compiles in memory at startup instead (well under a second).
RUN pip install --no-cache-dir --no-compile --require-hashes --no-deps -r /tmp/requirements.txt && \
    # runtime needs no package manager (and its bundled CVEs)
    python -m pip uninstall -y pip setuptools wheel && \
    lib="$(python -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')" && \
    # parts of Python Updock never uses: bundled pip installer, IDLE, docs data, sqlite
    rm -rf "$lib/ensurepip" "$lib/idlelib" "$lib/pydoc_data" "$lib/sqlite3" \
           "$lib"/lib-dynload/_sqlite3* "$lib/tkinter" "$lib/turtledemo" "$lib/turtle.py" \
           /usr/local/bin/idle* /usr/local/bin/pydoc* /usr/local/bin/pip* && \
    find "$lib/site-packages" -depth -type d \( -name tests -o -name test \) -exec rm -rf {} + && \
    rm -rf /tmp/* /root/.cache && \
    # state and log history (mount a volume here)
    mkdir -p /data /app

# Stage 2: the trimmed filesystem as a single layer
FROM scratch
COPY --from=build / /
# The base image's environment (FROM scratch doesn't inherit it)
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Set by the release workflow (tag without "v", commit); empty = "dev" build
ARG APP_VERSION=
ARG GIT_SHA=
ENV APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA}
WORKDIR /app
# Files owned by root and the root filesystem is read-only at runtime (see compose.yaml).
# Runs as root only to reach the Docker socket; all capabilities are dropped.
COPY app ./app
# Last so a new date doesn't invalidate the cached layers
ARG BUILD_DATE=
ENV BUILD_DATE=${BUILD_DATE} UPDOCK_DATA_DIR=/data
LABEL updock.self="true"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/readyz').status != 200)"]
# Short graceful-shutdown window: open live-update (SSE) connections never end on their own
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "5"]
