"""Job phase guards (code review, package 2).

- a claim can never run twice or on devices that already succeeded
- only one phase can start at a time, decided by the DB row, not a stale object
- a restart or an early crash never leaves a job stuck in *_running
- Day-N deploys exactly the template whose variables were resolved
"""

import json

import app.clients.webhook as webhook_module
import app.services.day0 as day0_module
import pytest
import respx
from app.db.models import Job
from app.db.session import open_session
from app.errors import ConfigurationError
from app.main import create_app
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tests.unit.test_day0_service import CCC, HOOK, _mock_ccc, _pnp_state, _setup
from tests.unit.test_dayn_service import _prepare, _run_day0, _store_dayn_mapping

CLAIM_URL = f"{CCC}/dna/intent/api/v1/onboarding/pnp-device/site-claim"
FAST = {"poll_interval": 0, "timeout": 5}


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "BACKOFF_BASE_SECONDS", 0)


def _set_status(job_id: int, status: str) -> None:
    with open_session() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.status = status


@pytest.mark.parametrize(
    "status", ["day0_running", "dayn_running", "dayn_failed", "completed", "partial_success"]
)
def test_claim_rejected_while_running_or_after_dayn_started(
    client: TestClient, status: str
) -> None:
    job_id = _setup(client)
    _set_status(job_id, status)
    response = client.post(f"/api/wizard/jobs/{job_id}/claim", json={"config_id": "t", **FAST})
    assert response.status_code == 409


def test_reclaim_only_retries_devices_that_did_not_succeed(client: TestClient) -> None:
    job_id = _setup(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Error", "errorMessage": "boom"})
        respx_mock.post(HOOK).respond(200)
        client.post(f"/api/wizard/jobs/{job_id}/claim", json={"config_id": "t", **FAST})
    assert client.get(f"/api/wizard/jobs/{job_id}").json()["status"] == "day0_partial"

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        claims = respx_mock.post(CLAIM_URL).respond(200, json={"response": "Device Claimed"})
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioned"})
        respx_mock.post(HOOK).respond(200)
        response = client.post(f"/api/wizard/jobs/{job_id}/claim", json={"config_id": "t", **FAST})
        claimed = [json.loads(call.request.content)["deviceId"] for call in claims.calls]
    assert response.status_code == 200, response.text
    assert claimed == ["pnp-2"]  # the provisioned sibling is never claimed again
    assert client.get(f"/api/wizard/jobs/{job_id}").json()["status"] == "day0_complete"


def test_reclaim_with_everything_succeeded_is_rejected(client: TestClient) -> None:
    job_id = _run_day0(client)
    _set_status(job_id, "day0_complete")
    response = client.post(f"/api/wizard/jobs/{job_id}/claim", json={"config_id": "t", **FAST})
    assert response.status_code == 422


def test_phase_start_is_decided_by_the_database_row(client: TestClient) -> None:
    """A request holding a stale 'idle' job object must still lose the race."""
    from app.api.wizard import _start_phase

    job_id = _setup(client)
    with open_session() as stale:
        job = stale.get(Job, job_id)
        assert job is not None and not job.status.endswith("_running")
        _set_status(job_id, "day0_running")  # another request won in the meantime
        with pytest.raises(HTTPException) as exc:
            _start_phase(stale, job, "day0_running")
        assert exc.value.status_code == 409


def test_restart_closes_out_interrupted_jobs(client: TestClient) -> None:
    day0_job = _setup(client)
    dayn_job = _setup(client)
    with open_session() as db:
        for job_id, status, state in (
            (day0_job, "day0_running", "claiming"),
            (dayn_job, "dayn_running", "dayn_deploying"),
        ):
            job = db.get(Job, job_id)
            assert job is not None
            job.status = status
            for device in job.devices:
                device.state = state

    with TestClient(create_app()) as restarted:  # same DB: simulates a container restart
        first = restarted.get(f"/api/wizard/jobs/{day0_job}").json()
        second = restarted.get(f"/api/wizard/jobs/{dayn_job}").json()

    assert first["status"] == "day0_failed"
    assert all(d["state"] == "failed" for d in first["devices"])
    assert all("Interrupted" in (d["error"] or "") for d in first["devices"])
    assert second["status"] == "dayn_failed"
    assert all(d["state"] == "dayn_failed" for d in second["devices"])
    assert all("NetBox was not changed" in (d["error"] or "") for d in second["devices"])


async def test_early_failure_in_background_run_never_strands_the_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _setup(client)
    with open_session() as db:  # as the claim endpoint leaves it for the background task
        job = db.get(Job, job_id)
        assert job is not None
        job.status = "day0_running"
        for device in job.devices:
            device.state = "queued"

    def broken(*_args: object) -> str:
        raise ConfigurationError("Stored secret cannot be decrypted with the current key.")

    monkeypatch.setattr(day0_module.settings_store, "decrypt_secret", broken)
    await day0_module.run_day0(job_id, config_id="t", image_id=None, poll_interval=0)

    with open_session() as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == "day0_failed"
        assert all(d.state == "failed" for d in job.devices)
        assert all("cannot be decrypted" in (d.error or "") for d in job.devices)


def test_deploy_rejects_a_template_other_than_the_resolved_one(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)  # resolves tmpl-N
    response = client.post(
        f"/api/wizard/jobs/{job_id}/dayn/deploy",
        json={"template_id": "tmpl-OTHER", "manual": {}, "poll_interval": 0},
    )
    assert response.status_code == 422
    assert "Resolve variables" in response.json()["detail"]


def test_deploy_without_resolving_first_is_rejected(client: TestClient) -> None:
    job_id = _run_day0(client)
    response = client.post(
        f"/api/wizard/jobs/{job_id}/dayn/deploy",
        json={"template_id": "tmpl-N", "manual": {}, "poll_interval": 0},
    )
    assert response.status_code == 422
    assert "Resolve variables" in response.json()["detail"]


def test_job_exposes_the_resolved_template_ids(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    job = _prepare(client, job_id)
    assert job["dayn_template_id"] == "tmpl-N"
    assert job["dayn2_template_id"] is None
