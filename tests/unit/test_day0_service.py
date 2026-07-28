"""Day-0 orchestration against mocked CCC/NetBox/webhook endpoints."""

from typing import Any

import app.clients.webhook as webhook_module
import pytest
import respx
from app.db.models import WebhookDelivery
from app.db.session import open_session
from app.services.day0 import run_day0
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "BACKOFF_BASE_SECONDS", 0)


CCC = "https://ccc.example.com"
NETBOX = "https://netbox.example.com"
HOOK = "https://ise-helper.example.com/hook"


def _setup(client: TestClient, *, webhook_enabled: bool = True) -> int:
    """Store settings + mapping, create a 2-device job, run matching."""
    client.put(
        "/api/settings/credentials",
        json={
            "catalyst": {"base_url": CCC, "username": "admin", "secret": "pw"},
            "netbox": {"base_url": NETBOX, "secret": "tok"},
            "webhook": {
                "base_url": HOOK,
                "secret": "shared-secret",
                "enabled": webhook_enabled,
            },
        },
    )
    client.put(
        "/api/mappings/sites",
        json={
            "mappings": [
                {
                    "netbox_site_id": 10,
                    "netbox_site_name": "FFM-DC1",
                    "ccc_site_id": "uuid-ffm",
                    "ccc_site_name": "Global/Germany/Frankfurt/DC1",
                }
            ]
        },
    )
    job_id: int = client.post(
        "/api/wizard/jobs",
        json={
            "devices": [
                {"serial": "FCW1111AAAA", "pid": "C9300-48P", "ccc_device_id": "pnp-1"},
                {"serial": "FCW2222BBBB", "pid": "C9300-24T", "ccc_device_id": "pnp-2"},
            ]
        },
    ).json()["id"]

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.get(f"{NETBOX}/api/dcim/devices/").respond(
            200,
            json={
                "results": [
                    _nb_device(1, "FCW1111AAAA", "sw-ffm-01"),
                    _nb_device(2, "FCW2222BBBB", "sw-ffm-02"),
                ],
                "next": None,
            },
        )
        respx_mock.get(f"{NETBOX}/api/ipam/vlans/").respond(
            200, json={"results": [{"id": 5, "vid": 110, "name": "MGMT"}], "next": None}
        )
        assert client.post(f"/api/wizard/jobs/{job_id}/match").status_code == 200
    return job_id


def _nb_device(device_id: int, serial: str, name: str) -> dict[str, Any]:
    return {
        "id": device_id,
        "name": name,
        "serial": serial,
        "site": {"id": 10, "name": "FFM-DC1"},
        "primary_ip4": {"address": f"172.20.10.{device_id}/24"},
        "status": {"value": "planned"},
    }


def _mock_ccc(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "tok"})
    respx_mock.post(f"{CCC}/dna/intent/api/v1/onboarding/pnp-device/site-claim").respond(
        200, json={"response": "Device Claimed"}
    )
    _mock_inventory_and_provision(respx_mock)


def _mock_inventory_and_provision(respx_mock: respx.MockRouter) -> None:
    """Inventory lookup + role/tag + provision-to-site, all post-claim."""
    respx_mock.get(url__regex=rf"{CCC}/dna/intent/api/v1/network-device/ip-address/.*").respond(
        200, json={"response": {"id": "ccc-uuid-1"}}
    )
    respx_mock.put(f"{CCC}/dna/intent/api/v1/network-device/brief").respond(200, json={})
    respx_mock.get(f"{CCC}/dna/intent/api/v1/tag").respond(
        200, json={"response": [{"id": "tag-1", "name": "Access"}]}
    )
    respx_mock.post(url__regex=rf"{CCC}/dna/intent/api/v1/tag/.*/member").respond(200, json={})
    respx_mock.post(f"{CCC}/dna/intent/api/v1/sda/provisionDevices").respond(
        200, json={"response": {"taskId": "prov-task-1"}}
    )
    respx_mock.get(f"{CCC}/dna/intent/api/v1/task/prov-task-1").respond(
        200, json={"response": {"isError": False, "endTime": 123}}
    )


def _pnp_state(respx_mock: respx.MockRouter, device_id: str, info: dict[str, Any]) -> None:
    respx_mock.get(f"{CCC}/dna/intent/api/v1/onboarding/pnp-device/{device_id}").respond(
        200, json={"id": device_id, "deviceInfo": info}
    )


async def test_full_day0_success_fires_webhooks(client: TestClient) -> None:
    job_id = _setup(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioned"})
        hook = respx_mock.post(HOOK).respond(200)
        await run_day0(job_id, config_id="tmpl-1", image_id=None, poll_interval=0, device_timeout=5)

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "day0_complete"
    assert [d["state"] for d in job["devices"]] == ["success", "success"]
    assert hook.call_count == 2
    body = hook.calls[0].request
    assert body.headers["X-PnPB-Signature"]
    with open_session() as db:
        deliveries = db.scalars(select(WebhookDelivery)).all()
        assert len(deliveries) == 2
        assert {d.status for d in deliveries} == {"delivered"}


async def test_one_failed_device_never_aborts_siblings(client: TestClient) -> None:
    job_id = _setup(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Error", "errorMessage": "image download failed"})
        hook = respx_mock.post(HOOK).respond(200)
        await run_day0(job_id, config_id="tmpl-1", image_id=None, poll_interval=0, device_timeout=5)

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "day0_partial"
    by_serial = {d["serial"]: d for d in job["devices"]}
    assert by_serial["FCW1111AAAA"]["state"] == "success"
    assert by_serial["FCW2222BBBB"]["state"] == "failed"
    assert "image download failed" in by_serial["FCW2222BBBB"]["error"]
    assert hook.call_count == 1  # webhook only for the successful device


async def test_poll_timeout_marks_device_failed(client: TestClient) -> None:
    job_id = _setup(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioning"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioning"})
        respx_mock.post(HOOK).respond(200)
        await run_day0(
            job_id, config_id="tmpl-1", image_id=None, poll_interval=0, device_timeout=0.05
        )

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "day0_failed"
    assert all("did not reach" in d["error"] for d in job["devices"])


async def test_webhook_failure_does_not_roll_back_claim(client: TestClient) -> None:
    job_id = _setup(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioned"})
        respx_mock.post(HOOK).respond(500)
        await run_day0(job_id, config_id="tmpl-1", image_id=None, poll_interval=0, device_timeout=5)

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "day0_complete"  # claims stay successful
    with open_session() as db:
        deliveries = db.scalars(select(WebhookDelivery)).all()
        assert {d.status for d in deliveries} == {"failed"}
        assert all(d.attempts == 4 for d in deliveries)


# --- CCC inventory metadata: role + tag --------------------------------------


def test_ccc_role_maps_netbox_role_names_onto_the_ccc_enum() -> None:
    """CCC only accepts a fixed role enum; NetBox role names are free text."""
    from app.services.day0 import ccc_role

    assert ccc_role("Access") == "ACCESS"
    assert ccc_role("campus access switch") == "ACCESS"
    assert ccc_role("Distribution") == "DISTRIBUTION"
    assert ccc_role("Core") == "CORE"
    assert ccc_role("Border Router") == "BORDER ROUTER"
    # nothing recognisable -> leave the role alone rather than guess
    assert ccc_role("Wireless AP") is None
    assert ccc_role(None) is None


def test_device_role_name_reads_the_wizard_selection() -> None:
    from app.services.day0 import device_role_name

    assert device_role_name({"switchType": "Access"}) == "Access"
    assert device_role_name({"HOSTNAME": "sw-1"}) is None
    assert device_role_name(None) is None


async def test_inventory_role_and_tag_failure_never_fails_day0(client: TestClient) -> None:
    """The switch is already onboarded when this runs — a 404 from the tag or
    role endpoint must not turn a good Day-0 into a failed one."""
    from app.clients.catalyst import CatalystCenterClient
    from app.db.models import JobDevice
    from app.services.day0 import _apply_inventory_metadata

    with open_session() as db:
        device = JobDevice(
            job_id=1,
            serial="FOC1",
            ccc_device_id="ccc-1",
            mgmt_ip="172.20.10.5/24",
            day0_variables={"switchType": "Access"},
        )
        db.add(device)
        db.flush()
        device_id = device.id

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "t"})
        respx_mock.get(f"{CCC}/dna/intent/api/v1/network-device/ip-address/172.20.10.5").respond(
            404, json={"message": "not found"}
        )
        async with CatalystCenterClient(CCC, "admin", "pw") as ccc:
            await _apply_inventory_metadata(ccc, 1, device_id)  # must not raise
