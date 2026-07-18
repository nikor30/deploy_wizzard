"""Load test for the Day-0 polling loops: a 25-device batch must complete
with the CCC concurrency semaphore (max 5 in-flight requests) respected."""

import time

import app.clients.base as base
import httpx
import pytest
from fastapi.testclient import TestClient

BATCH = 25


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "BACKOFF_BASE_SECONDS", 0)


def test_25_device_batch_respects_ccc_rate_limit(
    configured_client: TestClient, mock: httpx.Client
) -> None:
    client = configured_client
    mock.post("/__mock__/reset", json={"devices": BATCH})
    # make every device need a few polls so the loops genuinely interleave
    mock.post("/__mock__/config", json={"claim_polls": 3})

    devices = client.get("/api/wizard/pnp-devices").json()
    assert len(devices) == BATCH
    job_id = client.post(
        "/api/wizard/jobs",
        json={
            "devices": [
                {"serial": d["serial"], "pid": d["pid"], "ccc_device_id": d["ccc_device_id"]}
                for d in devices
            ]
        },
    ).json()["id"]
    matched = client.post(f"/api/wizard/jobs/{job_id}/match").json()
    assert all(d["match_status"] == "matched" for d in matched["devices"])

    started = time.monotonic()
    client.post(
        f"/api/wizard/jobs/{job_id}/claim",
        json={"config_id": "tpl-day0", "poll_interval": 0.02, "timeout": 30},
    )
    duration = time.monotonic() - started

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "day0_complete"
    assert sum(d["state"] == "success" for d in job["devices"]) == BATCH

    stats = mock.get("/__mock__/state").json()["stats"]
    assert stats["ccc_max_in_flight"] <= 5, stats
    # one token for the listing client, one shared by the whole claim batch —
    # never one per device
    assert stats["token_fetches"] == 2
    assert duration < 60, f"25-device batch took {duration:.1f}s"
