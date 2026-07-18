#!/usr/bin/env bash
# Mock CCC/NetBox/ISE stack for the Playwright e2e suite.
set -euo pipefail
cd "$(dirname "$0")/../.."
exec uv run python -m tests.mocks.stack --port 9100
