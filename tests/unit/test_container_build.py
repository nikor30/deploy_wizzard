"""Build and CI rules for the container (code review 2026-09-29, low findings).

The runtime behaviour (HEALTHCHECK honours PNPB_PORT, /data ownership is only
repaired when needed) is checked against the real image in the image smoke test.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTAINERFILE = (ROOT / "Containerfile").read_text(encoding="utf-8")


def test_every_base_image_is_pinned_by_digest() -> None:
    # A moving tag (node:22-alpine, python:3.12-slim) makes two builds of the
    # same commit differ; the uv image already was pinned.
    images = re.findall(r"^FROM\s+(\S+)", CONTAINERFILE, re.MULTILINE)
    assert images, "no FROM lines found"
    unpinned = [image for image in images if "@sha256:" not in image]
    assert unpinned == []


def test_frontend_install_has_no_silent_fallback() -> None:
    # `npm ci || npm install` ignored package-lock.json whenever `npm ci` failed.
    assert "npm install" not in CONTAINERFILE
    assert re.search(r"^RUN npm ci\b", CONTAINERFILE, re.MULTILINE)


def test_healthcheck_follows_pnpb_port() -> None:
    healthcheck = CONTAINERFILE.split("HEALTHCHECK", 1)[1].split("\nCMD", 1)[0]
    assert "8060" not in healthcheck
    assert "PNPB_PORT" in healthcheck


def test_e2e_runs_without_retries() -> None:
    # Platform rule: no retries in CI - a flaky test is a finding, not noise.
    config = (ROOT / "playwright.config.ts").read_text(encoding="utf-8")
    assert re.search(r"^\s*retries:\s*0,", config, re.MULTILINE)
