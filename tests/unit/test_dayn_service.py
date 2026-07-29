"""Day-N flow: prepare (introspection + resolution), deploy, activate in NetBox."""

import json
from typing import Any

import app.clients.webhook as webhook_module
import httpx
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


def _mock_template(respx_mock: respx.MockRouter) -> None:
    """The deploy resolves the template first (composite -> members)."""
    respx_mock.get(TEMPLATE_URL).respond(
        200,
        json={
            "templateId": "tmpl-N",
            "templateParams": [
                {"parameterName": "SNMP_LOCATION"},
                {"parameterName": "CONTACT"},
                {"parameterName": "PVLAN"},
            ],
        },
    )


def _mock_prepare(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(TEMPLATE_URL).respond(
        200,
        json={
            "templateId": "tmpl-N",
            "templateParams": [
                {"parameterName": "SNMP_LOCATION", "required": True},
                {"parameterName": "CONTACT", "required": True},
                {"parameterName": "PVLAN"},  # private-VLAN config: optional
            ],
        },
    )
    respx_mock.get(f"{NETBOX}/api/dcim/devices/1/").respond(200, json=_nb_detail(1))
    respx_mock.get(f"{NETBOX}/api/dcim/interfaces/").respond(
        200, json={"results": [], "next": None}
    )
    respx_mock.get(f"{NETBOX}/api/tenancy/contact-assignments/").respond(
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
    detail = response.json()["detail"]
    assert "CONTACT" in detail
    assert "PVLAN" not in detail  # optional: a blank PVLAN never blocks a deploy


def test_prepare_marks_private_vlan_optional(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    job = _prepare(client, job_id)
    variables = job["devices"][0]["dayn_variables"]
    assert variables["PVLAN"]["optional"] is True
    assert "optional" not in variables["CONTACT"]


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
        _mock_template(respx_mock)
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
    # the blank optional PVLAN is omitted so the template's own default applies
    assert "PVLAN" not in body


def test_task_error_with_empty_reason_drills_task_tree(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_template(respx_mock)
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
        _mock_template(respx_mock)
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


def test_composite_template_deploys_each_member_not_the_container(client: TestClient) -> None:
    """Deploying the composite itself makes CCC push its member JSON to the
    device as CLI text; each member must be deployed on its own instead."""
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        respx_mock.get(TEMPLATE_URL).respond(
            200,
            json={
                "templateId": "tmpl-N",
                "composite": True,
                "containingTemplates": [
                    {"id": "banner", "templateParams": [{"parameterName": "CONTACT"}]},
                    {"id": "ports", "templateParams": [{"parameterName": "SNMP_LOCATION"}]},
                ],
            },
        )
        deploy = respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(TASK_URL).respond(
            200, json={"response": {"isError": False, "endTime": 1752680000000}}
        )
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/1/").respond(200, json={"id": 1})
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/2/").respond(200, json={"id": 2})
        _deploy(client, job_id, respx_mock)

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert all(d["state"] == "completed" for d in job["devices"])

    bodies = [json.loads(call.request.content) for call in deploy.calls]
    # two members x two devices; the container id is never deployed
    assert {b["templateId"] for b in bodies} == {"banner", "ports"}
    assert "tmpl-N" not in {b["templateId"] for b in bodies}
    # each member only receives the parameters it declares
    for body in bodies:
        keys = set(body["targetInfo"][0]["params"])
        assert keys == ({"CONTACT"} if body["templateId"] == "banner" else {"SNMP_LOCATION"})


# --- deploy tracking: deployment id vs task id -------------------------------

DEPLOY_SENTENCE = (
    "Deployment of Template: cffd63b1-2e02-45b9-812e-2147176ea3be."
    "ApplicableTargets: [172.20.10.145]"
    "Template Deploymemnt Id: cf46d06a-a007-4275-b73b-519953693f29"
)


def test_deploy_handle_extracts_the_uuid_from_ccc_deployment_sentence() -> None:
    """deploy/v2 answers with a sentence, not an id. Polling it as a task gives
    HTTP 400 ("172.20.10 is not a valid UUID")."""
    from app.services.dayn import _deploy_handle

    assert _deploy_handle({"deploymentId": DEPLOY_SENTENCE}) == (
        "deployment",
        "cf46d06a-a007-4275-b73b-519953693f29",
    )
    # a genuine taskId is still tracked as a task
    assert _deploy_handle({"response": {"taskId": "task-1"}}) == ("task", "task-1")
    assert _deploy_handle({}) == ("", "")


def test_deploy_polls_the_deployment_status_endpoint(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    status_url = (
        f"{CCC}/dna/intent/api/v1/template-programmer/template/deploy/status/"
        "cf46d06a-a007-4275-b73b-519953693f29"
    )
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_template(respx_mock)
        respx_mock.post(DEPLOY_URL).respond(200, json={"deploymentId": DEPLOY_SENTENCE})
        status = respx_mock.get(status_url).respond(200, json={"status": "SUCCESS"})
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/1/").respond(200, json={"id": 1})
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/2/").respond(200, json={"id": 2})
        _deploy(client, job_id, respx_mock)

    assert status.called
    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert all(d["state"] == "completed" for d in job["devices"])


def test_deployment_failure_surfaces_the_device_level_reason(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    status_url = (
        f"{CCC}/dna/intent/api/v1/template-programmer/template/deploy/status/"
        "cf46d06a-a007-4275-b73b-519953693f29"
    )
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_template(respx_mock)
        respx_mock.post(DEPLOY_URL).respond(200, json={"deploymentId": DEPLOY_SENTENCE})
        respx_mock.get(status_url).respond(
            200,
            json={
                "status": "FAILURE",
                "devices": [
                    {
                        "status": "FAILURE",
                        "detailedStatusMessage": "Invalid input detected at Vlan900",
                    }
                ],
            },
        )
        _deploy(client, job_id, respx_mock)

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert all(d["state"] == "dayn_failed" for d in job["devices"])
    assert "Invalid input detected at Vlan900" in job["devices"][0]["error"]


def test_overall_success_with_a_failed_device_is_not_a_success(client: TestClient) -> None:
    """CCC reports the *deployment* SUCCESS while the single target was skipped
    or failed. Trusting the top-level status marks NetBox active for a switch
    that never received the config (§11: a half-updated source of truth)."""
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    status_url = (
        f"{CCC}/dna/intent/api/v1/template-programmer/template/deploy/status/"
        "cf46d06a-a007-4275-b73b-519953693f29"
    )
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_template(respx_mock)
        respx_mock.post(DEPLOY_URL).respond(200, json={"deploymentId": DEPLOY_SENTENCE})
        respx_mock.get(status_url).respond(
            200,
            json={
                "status": "SUCCESS",
                "devices": [
                    {
                        "status": "NOT_APPLICABLE",
                        "detailedStatusMessage": "Template not applicable to device",
                    }
                ],
            },
        )
        _deploy(client, job_id, respx_mock)

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert all(d["state"] == "dayn_failed" for d in job["devices"])
    assert "NOT_APPLICABLE" in job["devices"][0]["error"]
    assert "Template not applicable to device" in job["devices"][0]["error"]


def test_interactive_prompt_failure_gets_an_actionable_hint() -> None:
    """CCC only auto-answers the prompts it knows; a bare "Do you wish to
    continue? [yes]:" makes it reject the whole push as invalid CLI."""
    from app.services.dayn import interactive_prompt_hint

    reason = (
        "Unable to push the invalid CLI to the device 172.20.10.145 using protocol ssh2. "
        "Invalid CLI - Current output : class-map type control subscriber match-all "
        "AAA_SVR_DOWN_AUTHD_HOST ... Do you wish to continue? [yes]: ... [confirm] (Interactive)"
    )
    hint = interactive_prompt_hint(reason)
    assert "#INTERACTIVE" in hint
    assert "<IQ>" in hint and "<R>" in hint
    # answer the prompt on the control class that raises it; a separate
    # conversion command fails (display = EXEC-only, convert-to = not in EXEC)
    assert "class-map type control subscriber" in hint
    assert "#ENDS_INTERACTIVE" in hint
    # an ordinary CLI rejection gets no interactive hint
    assert interactive_prompt_hint("Invalid CLI - Current output : bogus command") == ""
    assert interactive_prompt_hint("Device unreachable") == ""


# --- staged Day-N: base templates first, ports/uplinks second -----------------


def test_a_failing_member_no_longer_takes_the_other_templates_down(
    client: TestClient,
) -> None:
    """The port template tripping over an interface used to abort the composite,
    so the VLAN and banner members never ran and the switch got neither."""
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        respx_mock.get(TEMPLATE_URL).respond(
            200,
            json={
                "templateId": "tmpl-N",
                "composite": True,
                "containingTemplates": [
                    {"id": "ports", "templateParams": [{"parameterName": "SNMP_LOCATION"}]},
                    {"id": "vlans", "templateParams": [{"parameterName": "CONTACT"}]},
                    {"id": "banner", "templateParams": [{"parameterName": "CONTACT"}]},
                ],
            },
        )
        deploy = respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        # the first member fails, the rest succeed
        respx_mock.get(TASK_URL).mock(
            side_effect=[
                httpx.Response(200, json={"response": {"isError": True, "failureReason": "boom"}}),
                httpx.Response(200, json={"response": {"isError": False, "endTime": 1}}),
                httpx.Response(200, json={"response": {"isError": False, "endTime": 1}}),
            ]
            * 2
        )
        _deploy(client, job_id, respx_mock)

    # all three members were attempted for the first device, not just the first
    assert deploy.call_count >= 3
    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    error = job["devices"][0]["error"]
    assert "1 of 3 templates failed" in error
    assert "the others were applied" in error


def test_stage_one_can_defer_netbox_activation_for_a_ports_stage(
    client: TestClient,
) -> None:
    """§11: the source of truth is only touched when the device is finished, so
    a base stage that will be followed by ports must not activate."""
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_template(respx_mock)
        respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(TASK_URL).respond(200, json={"response": {"isError": False, "endTime": 1}})
        patch = respx_mock.patch(f"{NETBOX}/api/dcim/devices/1/").respond(200, json={"id": 1})
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

    assert not patch.called, "NetBox must not be activated while a stage is outstanding"
    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert all(d["state"] == "dayn_complete" for d in job["devices"])


def test_ports_stage_deploys_its_own_template_and_activates(client: TestClient) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_template(respx_mock)
        respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(TASK_URL).respond(200, json={"response": {"isError": False, "endTime": 1}})
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/1/").respond(200, json={"id": 1})
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/2/").respond(200, json={"id": 2})
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

    # stage 2: resolve against its own template, then deploy
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_prepare(respx_mock)
        prepared = client.post(
            f"/api/wizard/jobs/{job_id}/dayn2/prepare", json={"template_id": "tmpl-N"}
        )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["devices"][0]["dayn2_variables"]

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _mock_template(respx_mock)
        deploy = respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(TASK_URL).respond(200, json={"response": {"isError": False, "endTime": 1}})
        patch = respx_mock.patch(f"{NETBOX}/api/dcim/devices/1/").respond(200, json={"id": 1})
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/2/").respond(200, json={"id": 2})
        response = client.post(
            f"/api/wizard/jobs/{job_id}/dayn2/deploy",
            json={
                "template_id": "tmpl-N",
                "manual": _manual_for_all(client, job_id),
                "poll_interval": 0,
                "task_timeout": 5,
            },
        )
    assert response.status_code == 200, response.text
    assert deploy.called
    assert patch.called, "the last stage activates NetBox"
    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert all(d["state"] == "completed" for d in job["devices"])
