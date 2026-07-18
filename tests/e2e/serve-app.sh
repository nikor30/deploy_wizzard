#!/usr/bin/env bash
# App under test for the Playwright e2e suite: fresh DB, built SPA.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ ! -f frontend/dist/index.html ]; then
  echo "frontend/dist missing — run 'make e2e' (it builds the SPA first)" >&2
  exit 1
fi
rm -rf app/static
cp -r frontend/dist app/static

E2E_DATA="${TMPDIR:-/tmp}/pnpb-e2e-data"
rm -rf "$E2E_DATA"
mkdir -p "$E2E_DATA"
export PNPB_DB_PATH="$E2E_DATA/pnpb.sqlite"

exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8061
