# syntax=docker/dockerfile:1
FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# Set by the release workflow (tag without "v", commit); empty = "dev" build
ARG APP_VERSION=
ARG GIT_SHA=
ENV APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA} \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Runtime needs no package manager (and its bundled CVEs)
RUN python -m pip uninstall -y pip setuptools wheel
# Files owned by root and the root filesystem is read-only at runtime (see compose.yaml).
# Runs as root only to reach the Docker socket; all capabilities are dropped.
COPY app ./app
LABEL updock.self="true"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/health').status != 200)"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
