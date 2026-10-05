# Stage 1: build the frontend. Every base image is pinned by digest (tag kept for
# readability); `npm ci` only - a stale package-lock.json must fail the build.
FROM docker.io/library/node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: pin the runtime dependencies to uv.lock (the versions CI tests with).
# `--locked` fails the build if uv.lock is out of date with pyproject.toml; the export
# carries hashes, so stage 3 installs exactly the locked artifacts. uv image pinned by digest.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS deps
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv --version \
    && uv export --locked --no-dev --no-emit-project --format requirements-txt \
       -o requirements.lock.txt

# Stage 3: python runtime
FROM docker.io/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
WORKDIR /srv

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Locked dependencies first (hash-checked), then the app itself without resolving anything.
COPY --from=deps /build/requirements.lock.txt ./requirements.lock.txt
RUN pip install --require-hashes -r requirements.lock.txt
COPY pyproject.toml README.md LICENSE ./
COPY app/ ./app/
RUN pip install --no-deps . && pip check

COPY --from=frontend /build/dist/ ./app/static/
# Readable for pnpb regardless of the build context's file modes: a release archive
# packed with umask 027 would leave root-owned 0640 files the app cannot read.
RUN chmod -R u=rwX,go=rX /srv

# The app runs as a dedicated non-root user (uid 10001). The entrypoint starts
# as root only to repair /data ownership (volumes from pre-1.2 root containers)
# and immediately drops privileges; `--user 10001` also works for fresh volumes.
COPY entrypoint.sh /entrypoint.sh
RUN useradd --system --uid 10001 --no-create-home pnpb \
    && mkdir /data \
    && chown pnpb:pnpb /data \
    && chmod 0755 /entrypoint.sh
VOLUME /data
ENV PNPB_DB_PATH=/data/pnpb.sqlite \
    PNPB_PORT=8060
EXPOSE 8060

# Probes the port the app actually listens on (`-e PNPB_PORT=...` included).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ['PNPB_PORT'], timeout=4)"]

CMD ["/entrypoint.sh"]
