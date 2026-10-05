"""Day-0 and Day-N derive the management values the same way (code review, #17).

Day-N resolved MGMT_IP & co. only from NetBox's primary_ip4. Planned devices
often lack one, so matching falls back to the mgmt-interface IP - Day-0 used
that address, Day-N left the variables open although it deploys to exactly
that address. And MGMT_SUBNET went to CCC as a dotted mask on Day-0 (IOS
rejects CIDR in `ip address`) but as CIDR on Day-N.
"""

import json
from typing import Any

import respx
from app.services.dayn import build_device_context, resolve_path, resolve_variables
from fastapi.testclient import TestClient
from tests.unit.test_day0_service import NETBOX, _mock_ccc
from tests.unit.test_dayn_service import (
    DEPLOY_URL,
    TASK_URL,
    TEMPLATE_URL,
    _nb_detail,
    _run_day0,
)

NO_PRIMARY = {"id": 1, "name": "sw-1", "primary_ip4": None}


def test_context_uses_the_job_mgmt_address_without_primary_ip() -> None:
    context = build_device_context(NO_PRIMARY, mgmt_address="172.20.11.7/24")
    assert resolve_path(context, "device.mgmt.ip") == "172.20.11.7"
    assert resolve_path(context, "device.mgmt.netmask") == "255.255.255.0"
    assert resolve_path(context, "device.mgmt.cidr") == "172.20.11.0/24"


def test_job_mgmt_address_wins_over_primary_ip() -> None:
    # Day-N deploys to the address the device was claimed with - the variables
    # must describe that same address, not a primary IP changed since.
    device = {"id": 1, "name": "sw-1", "primary_ip4": {"address": "172.20.10.5/24"}}
    context = build_device_context(device, mgmt_address="172.20.11.7/24")
    assert resolve_path(context, "device.mgmt.ip") == "172.20.11.7"


def test_invalid_mgmt_address_leaves_the_values_open() -> None:
    context = build_device_context(NO_PRIMARY, mgmt_address="not-an-ip")
    assert resolve_path(context, "device.mgmt.ip") is None


def test_mgmt_subnet_alias_is_shown_as_cidr_and_sent_as_mask() -> None:
    context = build_device_context(NO_PRIMARY, mgmt_address="172.20.11.7/22")
    resolved = resolve_variables(["MGMT_SUBNET"], {}, context)
    assert resolved["MGMT_SUBNET"] == {
        "value": "172.20.8.0/22",
        "source": "netbox",
        "claim_value": "255.255.252.0",
    }


def test_explicit_mapping_to_the_cidr_is_the_operators_choice() -> None:
    context = build_device_context(NO_PRIMARY, mgmt_address="172.20.11.7/24")
    resolved = resolve_variables(["MGMT_SUBNET"], {"MGMT_SUBNET": "device.mgmt.cidr"}, context)
    assert resolved["MGMT_SUBNET"] == {"value": "172.20.11.0/24", "source": "mapped"}


def _template(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(TEMPLATE_URL).respond(
        200,
        json={
            "templateId": "tmpl-N",
            "templateParams": [
                {"parameterName": "MGMT_IP"},
                {"parameterName": "MGMT_SUBNET"},
            ],
        },
    )


def _prepare_without_primary_ip(client: TestClient, job_id: int) -> dict[str, Any]:
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _template(respx_mock)
        # the NetBox detail carries no primary_ip4 (see _nb_detail)
        for device_id in (1, 2):
            respx_mock.get(f"{NETBOX}/api/dcim/devices/{device_id}/").respond(
                200, json=_nb_detail(device_id)
            )
        respx_mock.get(f"{NETBOX}/api/dcim/interfaces/").respond(
            200, json={"results": [], "next": None}
        )
        respx_mock.get(f"{NETBOX}/api/tenancy/contact-assignments/").respond(
            200, json={"results": [], "next": None}
        )
        response = client.post(
            f"/api/wizard/jobs/{job_id}/dayn/prepare", json={"template_id": "tmpl-N"}
        )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_dayn_resolves_and_deploys_the_mgmt_values_day0_used(client: TestClient) -> None:
    job_id = _run_day0(client)
    job = _prepare_without_primary_ip(client, job_id)
    first = next(d for d in job["devices"] if d["serial"] == "FCW1111AAAA")
    assert first["mgmt_ip"] == "172.20.10.1/24"  # from matching, used by Day-0
    assert first["dayn_variables"]["MGMT_IP"] == {"value": "172.20.10.1", "source": "netbox"}

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _template(respx_mock)
        deploy = respx_mock.post(DEPLOY_URL).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(TASK_URL).respond(200, json={"response": {"isError": False, "endTime": 1}})
        respx_mock.patch(url__regex=rf"{NETBOX}/api/dcim/devices/.*").respond(200, json={})
        response = client.post(
            f"/api/wizard/jobs/{job_id}/dayn/deploy",
            json={"template_id": "tmpl-N", "poll_interval": 0, "task_timeout": 5},
        )
    assert response.status_code == 200, response.text
    targets = [json.loads(call.request.content)["targetInfo"][0] for call in deploy.calls]
    first_target = next(t for t in targets if t["id"] == "172.20.10.1")
    assert first_target["params"] == {"MGMT_IP": "172.20.10.1", "MGMT_SUBNET": "255.255.255.0"}
