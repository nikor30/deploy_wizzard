# Stage 1: build the frontend
FROM docker.io/library/node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: python runtime
FROM docker.io/library/python:3.12-slim
WORKDIR /srv

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md LICENSE ./
COPY app/ ./app/
RUN pip install .

COPY --from=frontend /build/dist/ ./app/static/

# The app runs as a dedicated non-root user (uid 10001). The entrypoint starts
# as root only to repair /data ownership (volumes from pre-1.2 root containers)
# and immediately drops privileges; `--user 10001` also works for fresh volumes.
COPY entrypoint.sh /entrypoint.sh
RUN useradd --system --uid 10001 --no-create-home pnpb \
    && mkdir /data \
    && chown pnpb:pnpb /data \
    && chmod +x /entrypoint.sh
VOLUME /data
ENV PNPB_DB_PATH=/data/pnpb.sqlite
EXPOSE 8060

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8060/api/health', timeout=4)"]

CMD ["/entrypoint.sh"]
