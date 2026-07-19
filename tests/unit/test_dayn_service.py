"""Day-N flow: prepare (introspection + resolution), deploy, activate in NetBox."""

from typing import Any

import app.clients.webhook as webhook_module
import pytest
import respx
from fastapi.testclient import TestClient
from tests.unit.test_day0_service import CCC, HOOK, NETBOX, _mock_ccc, _pnp_state, _setup

TEMPLATE_URL = f"{CCC}/dna/intent/api/v1/template-programmer/template/tmpl-N"
DEPLOY_URL = f"{CCC}/dna/intent/api/v1/template-programmer/template/deploy/v2"
TASK_URL = f"{CCC}/dna/intent/api/v1/task/task-1"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "BACKOFF_BASE_SECONDS", 0)


def _run_day0(client: TestClient) -> int:
    """Day-0 both devices to success so they are Day-N eligible."""
    job_id = _setup(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioned"})
        respx_mock.post(HOOK).respond(200)
        client.post(
            f"/api/wizard/jobs/{job_id}/claim",
            json={"config_id": "tmpl-0", "poll_interval": 0, "timeout": 5},
        )
    return job_id


def _nb_detail(device_id: int) -> dict[str, Any]:
    return {
        "id": device_id,
        "name": f"sw-ffm-0{device_id}",
        "custom_fields": {"snmp_location": f"Rack {device_id}"},
        "config_context": {"ntp": {"servers": ["10.0.0.1"]}},
    }


def _mock_prepare(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(TEMPLATE_URL).respond(
        200,
        json={
            "templateId": "tmpl-N",
            "templateParams": [
                {"parameterName": "SNMP_LOCATION", "required": True},
                {"parameterName": "CONTACT", "required": True},
            ],
        },
    )
    respx_mock.get(f"{NETBOX}/api/dcim/devices/1/").respond(200, json=_nb_detail(1))
    respx_mock.get(f"{NETBOX}/api/dcim/interfaces/").respond(
        200, json={"results": [], "next": None}
    )
    respx_mock.get(f"{NETBOX}/api/dcim/devices/2/").respond(200, json=_nb_detail(2))


def _store_dayn_mapping(client: TestClient) -> None:
    client.put(
        "/api/settings/dayn",
        json={
            "mappings": [
                {"variable": "SNMP_LOCATION", "source_path": "device.custom_fields.snmp_location"}
            ]
        },
    )


def _prepare(client: TestClient, job_id: int) -> dict[str, Any]:
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_prepare(respx_mock)
        response = client.post(
            f"/api/wizard/jobs/{job_id}/dayn/prepare", json={"template_id": "tmpl-N"}
        )
    assert response.status_code == 200
    return dict(response.json())


def test_dayn_settings_roundtrip_and_duplicate_rejection(client: TestClient) -> None:
    _store_dayn_mapping(client)
    body = client.get("/api/settings/dayn").json()
    assert body["mappings"] == [
        {"variable": "SNMP_LOCATION", "source_path": "device.custom_fields.snmp_location"}
    ]
    dup = client.put(
        "/api/settings/dayn",
        json={
            "mappings": [
                {"variable": "X", "source_path": "a"},
                {"variable": "X", "source_path": "b"},
            ]
        },
    )
    assert dup.status_code == 422


def test_prepare_resolves_mapped_and_flags_manual(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    job = _prepare(client, job_id)
    device = job["devices"][0]
    assert device["dayn_variables"]["SNMP_LOCATION"] == {"value": "Rack 1", "source": "mapped"}
    assert device["dayn_variables"]["CONTACT"] == {"value": None, "source": "manual"}


def test_deploy_rejects_missing_manual_values(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    response = client.post(
        f"/api/wizard/jobs/{job_id}/dayn/deploy", json={"template_id": "tmpl-N", "manual": {}}
    )
    assert response.status_code == 422
    assert "CONTACT" in response.json()["detail"]


def _manual_for_all(client: TestClient, job_id: int) -> dict[str, dict[str, str]]:
    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    return {str(d["id"]): {"CONTACT": "noc@example.com"} for d in job["devices"]}


def _deploy(client: TestClient, job_id: int, respx_mock: respx.MockRouter) -> None:
    response = client.post(
        f"/api/wizard/jobs/{job_id}/dayn/deploy",
        json={
            "template_id": "tmpl-N",
            "manual": _manual_for_all(client, job_id),
            "poll_interval": 0,
            "task_timeout": 5,
        },
    )
    assert response.status_code == 200


def test_full_dayn_success_activates_netbox(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        deploy = respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(TASK_URL).respond(
            200, json={"response": {"isError": False, "endTime": 1752680000000}}
        )
        patch1 = respx_mock.patch(f"{NETBOX}/api/dcim/devices/1/").respond(
            200, json={"id": 1, "status": {"value": "active"}}
        )
        patch2 = respx_mock.patch(f"{NETBOX}/api/dcim/devices/2/").respond(
            200, json={"id": 2, "status": {"value": "active"}}
        )
        _deploy(client, job_id, respx_mock)

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["current_step"] == 5
    assert all(d["state"] == "completed" for d in job["devices"])
    assert patch1.called and patch2.called
    # deploy payload carried both resolved and manual params
    body = deploy.calls[0].request.content.decode()
    assert "noc@example.com" in body
    assert "Rack" in body


def test_task_error_with_empty_reason_drills_task_tree(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(TASK_URL).respond(200, json={"response": {"isError": True}})
        respx_mock.get(f"{TASK_URL}/tree").respond(
            200,
            json={
                "response": [
                    {"isError": False},
                    {"isError": True, "failureReason": "CLI apply failed on Gi1/0/1"},
                ]
            },
        )
        patch = respx_mock.patch(f"{NETBOX}/api/dcim/devices/1/").respond(200, json={})
        _deploy(client, job_id, respx_mock)

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "dayn_failed"
    assert all(d["state"] == "dayn_failed" for d in job["devices"])
    assert "CLI apply failed on Gi1/0/1" in job["devices"][0]["error"]
    assert not patch.called  # never activate NetBox on failure


def test_netbox_patch_failure_after_success_is_partial_success(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(TASK_URL).respond(
            200, json={"response": {"isError": False, "endTime": 1752680000000}}
        )
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/1/").respond(
            200, json={"id": 1, "status": {"value": "active"}}
        )
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/2/").respond(500)
        _deploy(client, job_id, respx_mock)

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "partial_success"
    by_serial = {d["serial"]: d for d in job["devices"]}
    assert by_serial["FCW1111AAAA"]["state"] == "completed"
    assert by_serial["FCW2222BBBB"]["state"] == "activate_failed"
