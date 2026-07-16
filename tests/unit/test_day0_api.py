"""Claim endpoint, templates endpoint, and SSE snapshot behavior."""

import json

import app.clients.webhook as webhook_module
import pytest
import respx
from fastapi.testclient import TestClient
from tests.unit.test_day0_service import CCC, HOOK, _mock_ccc, _pnp_state, _setup


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "BACKOFF_BASE_SECONDS", 0)


def test_templates_endpoint(client: TestClient) -> None:
    client.put(
        "/api/settings/credentials",
        json={"catalyst": {"base_url": CCC, "username": "admin", "secret": "pw"}},
    )
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "t"})
        respx_mock.get(f"{CCC}/dna/intent/api/v1/template-programmer/template").respond(
            200,
            json=[
                {"templateId": "tmpl-1", "name": "Day0-Onboarding", "projectName": "Onboarding"},
                {"name": "broken-no-id"},
            ],
        )
        response = client.get("/api/wizard/day0/templates")
    assert response.status_code == 200
    assert response.json() == [{"id": "tmpl-1", "name": "Day0-Onboarding", "project": "Onboarding"}]


def test_claim_runs_in_background_and_updates_job(client: TestClient) -> None:
    job_id = _setup(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioned"})
        respx_mock.post(HOOK).respond(200)
        # TestClient runs BackgroundTasks before returning, so the job is
        # already terminal when we read it back.
        response = client.post(
            f"/api/wizard/jobs/{job_id}/claim",
            json={"config_id": "tmpl-1", "poll_interval": 0, "timeout": 5},
        )
    assert response.status_code == 200
    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "day0_complete"
    assert job["current_step"] == 3


def test_claim_without_matched_devices_rejected(client: TestClient) -> None:
    job_id = client.post(
        "/api/wizard/jobs",
        json={"devices": [{"serial": "X", "pid": None, "ccc_device_id": "pnp-9"}]},
    ).json()["id"]
    response = client.post(f"/api/wizard/jobs/{job_id}/claim", json={"config_id": "t"})
    assert response.status_code == 422


def test_sse_stream_ends_with_terminal_snapshot(client: TestClient) -> None:
    job_id = _setup(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioned"})
        respx_mock.post(HOOK).respond(200)
        client.post(
            f"/api/wizard/jobs/{job_id}/claim",
            json={"config_id": "tmpl-1", "poll_interval": 0, "timeout": 5},
        )

    with client.stream("GET", f"/api/wizard/jobs/{job_id}/events") as stream:
        events = [line for line in stream.iter_lines() if line.startswith("data: ")]
    assert len(events) == 1  # job already terminal -> single snapshot, then close
    snapshot = json.loads(events[0].removeprefix("data: "))
    assert snapshot["status"] == "day0_complete"
    assert all(d["state"] == "success" for d in snapshot["devices"])
