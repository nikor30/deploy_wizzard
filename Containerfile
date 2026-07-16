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

COPY pyproject.toml README.md LICENSE ./
COPY app/ ./app/
RUN pip install --no-cache-dir .

COPY --from=frontend /build/dist/ ./app/static/

RUN mkdir /data
VOLUME /data
ENV PNPB_DB_PATH=/data/pnpb.sqlite
EXPOSE 8060

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8060"]
