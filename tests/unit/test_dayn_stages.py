"""Two-stage Day-N: a finished base stage is not a failure (code review, package 3).

Stage 1 with deferred activation parks devices at `dayn_complete`; the job used to
end as `dayn_failed`, which the wizard treats as done - the ports stage was
unreachable and the stats counted a successful deploy as failed.
"""

import pytest
import respx
from app.db.models import Job
from app.db.session import open_session
from app.services.dayn import _finish
from fastapi.testclient import TestClient
from tests.unit.test_day0_service import NETBOX, _mock_ccc, _setup
from tests.unit.test_dayn_service import (
    DEPLOY_URL,
    TASK_URL,
    _manual_for_all,
    _mock_template,
    _prepare,
    _run_day0,
    _store_dayn_mapping,
)


def test_stage_one_with_deferred_activation_waits_for_the_ports_stage(
    client: TestClient,
) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_template(respx_mock)
        respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(TASK_URL).respond(200, json={"response": {"isError": False, "endTime": 1}})
        respx_mock.patch(url__regex=rf"{NETBOX}/api/dcim/devices/.*").respond(200, json={})
        client.post(
            f"/api/wizard/jobs/{job_id}/dayn/deploy",
            json={
                "template_id": "tmpl-N",
                "manual": _manual_for_all(client, job_id),
                "poll_interval": 0,
                "task_timeout": 5,
                "activate": False,
            },
        )
    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert all(d["state"] == "dayn_complete" for d in job["devices"])
    assert job["status"] == "dayn_stage1_complete"


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["dayn_complete", "dayn_complete"], "dayn_stage1_complete"),
        (["dayn_complete", "dayn_failed"], "dayn_stage1_partial"),
        (["dayn_failed", "dayn_failed"], "dayn_failed"),
        (["completed", "completed"], "completed"),  # after the ports stage
        (["completed", "dayn_failed"], "partial_success"),
    ],
)
def test_job_status_after_a_day_n_stage(
    client: TestClient, states: list[str], expected: str
) -> None:
    job_id = _setup(client)
    with open_session() as db:
        job = db.get(Job, job_id)
        assert job is not None
        for device, state in zip(job.devices, states, strict=True):
            device.state = state
    _finish(job_id)
    assert client.get(f"/api/wizard/jobs/{job_id}").json()["status"] == expected


def test_day0_cannot_be_rerun_once_stage_one_finished(client: TestClient) -> None:
    job_id = _setup(client)
    with open_session() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.status = "dayn_stage1_complete"
    response = client.post(f"/api/wizard/jobs/{job_id}/claim", json={"config_id": "t"})
    assert response.status_code == 409
