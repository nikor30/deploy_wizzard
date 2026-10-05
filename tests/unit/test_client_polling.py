"""Code review, package 5: clients and polling.

- NetBox pagination must never send the API token to another host or over plain
  HTTP just because the `next` link says so (proxy without X-Forwarded-Proto)
- a single 429 or a short CCC outage during a 30-minute poll must not fail a
  device that Catalyst Center is still onboarding
- the first poll after a claim must not take the device's *previous* terminal
  state (Error/Provisioned from an earlier attempt) as the new result
"""

import httpx
import pytest
import respx
from app.clients import base
from app.clients.catalyst import CatalystCenterClient
from app.clients.netbox import NetBoxClient
from app.services.day0 import _claim_one
from app.services.dayn import poll_deployment, poll_task
from fastapi.testclient import TestClient
from tests.unit.test_catalyst_client import BASE, SITE_URL, TOKEN_URL, sites
from tests.unit.test_day0_service import _setup

PNP_URL = f"{BASE}/dna/intent/api/v1/onboarding/pnp-device/pnp-1"
CLAIM_URL = f"{BASE}/dna/intent/api/v1/onboarding/pnp-device/site-claim"
TASK_URL = f"{BASE}/dna/intent/api/v1/task/task-1"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "BACKOFF_BASE_SECONDS", 0)


def _pnp(state: str, error: str | None = None) -> httpx.Response:
    info: dict[str, str] = {"state": state}
    if error:
        info["errorMessage"] = error
    return httpx.Response(200, json={"id": "pnp-1", "deviceInfo": info})


# --- NetBox pagination ----------------------------------------------------------


@respx.mock
async def test_netbox_next_link_is_followed_on_the_configured_host_only() -> None:
    respx.get("https://netbox.example.com/api/dcim/devices/").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "results": [{"id": 1}],
                    "next": "http://netbox-internal:8080/api/dcim/devices/?limit=1&offset=1",
                },
            ),
            httpx.Response(200, json={"results": [{"id": 2}], "next": None}),
        ]
    )
    leaked = respx.route(host="netbox-internal").respond(200, json={"results": [], "next": None})
    async with NetBoxClient("https://netbox.example.com", "tok") as client:
        devices = await client.get_devices(status="planned")
    assert [d["id"] for d in devices] == [1, 2]
    assert not leaked.called  # the token never left the configured https host


# --- 429 / transient errors -------------------------------------------------------


@respx.mock
async def test_get_retries_after_429_honouring_retry_after() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    route = respx.get(SITE_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=sites(1)),
    ]
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        assert len(await client.get_sites()) == 1
    assert route.call_count == 2


@respx.mock
async def test_task_poll_survives_a_short_ccc_outage() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    outage = [httpx.Response(503)] * (base.GET_RETRIES + 1)  # one exhausted GET
    done = httpx.Response(200, json={"response": {"isError": False, "endTime": 1}})
    respx.get(TASK_URL).mock(side_effect=[*outage, done])
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        await poll_task(client, "task-1", poll_interval=0, task_timeout=5)


@respx.mock
async def test_deployment_poll_survives_rate_limiting() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    limited = [httpx.Response(429)] * (base.GET_RETRIES + 1)
    status_url = f"{BASE}/dna/intent/api/v1/template-programmer/template/deploy/status/dep-1"
    respx.get(status_url).mock(
        side_effect=[
            *limited,
            httpx.Response(200, json={"status": "SUCCESS", "devices": [{"status": "SUCCESS"}]}),
        ]
    )
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        await poll_deployment(client, "dep-1", poll_interval=0, task_timeout=5)


# --- Day-0 PnP polling ------------------------------------------------------------


def _device_id(client: TestClient) -> tuple[int, int]:
    job_id = _setup(client)
    device = client.get(f"/api/wizard/jobs/{job_id}").json()["devices"][0]
    return job_id, int(device["id"])


def _state(client: TestClient, job_id: int) -> tuple[str, str | None]:
    device = client.get(f"/api/wizard/jobs/{job_id}").json()["devices"][0]
    return device["state"], device["error"]


async def _claim(job_id: int, device_id: int) -> None:
    async with CatalystCenterClient(BASE, "admin", "pw") as ccc:
        await _claim_one(
            ccc,
            job_id,
            device_id,
            {"deviceId": "pnp-1"},
            poll_interval=0,
            device_timeout=5,
            provision=False,
        )


async def test_pnp_poll_survives_a_short_ccc_outage(client: TestClient) -> None:
    job_id, device_id = _device_id(client)
    outage = [httpx.Response(503)] * (base.GET_RETRIES + 1)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(TOKEN_URL).respond(200, json={"Token": "tok"})
        respx_mock.post(CLAIM_URL).respond(200, json={"response": "Device Claimed"})
        respx_mock.get(url__regex=rf"{BASE}/dna/intent/api/v1/network-device/.*").respond(
            200, json={"response": {}}
        )
        respx_mock.get(PNP_URL).mock(
            side_effect=[_pnp("Unclaimed"), *outage, _pnp("Onboarding"), _pnp("Provisioned")]
        )
        respx_mock.post(url__regex=r"https://ise-helper.example.com/.*").respond(200)
        await _claim(job_id, device_id)
    assert _state(client, job_id)[0] == "success"


async def test_previous_error_state_is_not_taken_as_the_new_result(client: TestClient) -> None:
    job_id, device_id = _device_id(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(TOKEN_URL).respond(200, json={"Token": "tok"})
        respx_mock.post(CLAIM_URL).respond(200, json={"response": "Device Claimed"})
        respx_mock.get(url__regex=rf"{BASE}/dna/intent/api/v1/network-device/.*").respond(
            200, json={"response": {}}
        )
        respx_mock.get(PNP_URL).mock(
            side_effect=[
                _pnp("Error", "old attempt"),  # before the claim
                _pnp("Error", "old attempt"),  # CCC has not updated the record yet
                _pnp("Planned"),
                _pnp("Provisioned"),
            ]
        )
        respx_mock.post(url__regex=r"https://ise-helper.example.com/.*").respond(200)
        await _claim(job_id, device_id)
    assert _state(client, job_id)[0] == "success"


async def test_previous_provisioned_state_is_not_a_success(client: TestClient) -> None:
    job_id, device_id = _device_id(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(TOKEN_URL).respond(200, json={"Token": "tok"})
        respx_mock.post(CLAIM_URL).respond(200, json={"response": "Device Claimed"})
        respx_mock.get(PNP_URL).mock(
            side_effect=[
                _pnp("Provisioned"),  # before the claim
                _pnp("Provisioned"),  # stale
                _pnp("Error", "claim rejected"),
            ]
        )
        await _claim(job_id, device_id)
    state, error = _state(client, job_id)
    assert state == "failed"
    assert "claim rejected" in (error or "")


async def test_a_state_that_never_changes_is_accepted_after_a_few_polls(
    client: TestClient,
) -> None:
    """The stale guard is bounded: a device that really fails the same way again
    is reported, not polled until the timeout."""
    job_id, device_id = _device_id(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(TOKEN_URL).respond(200, json={"Token": "tok"})
        respx_mock.post(CLAIM_URL).respond(200, json={"response": "Device Claimed"})
        pnp = respx_mock.get(PNP_URL).mock(return_value=_pnp("Error", "still broken"))
        await _claim(job_id, device_id)
    state, error = _state(client, job_id)
    assert state == "failed"
    assert "still broken" in (error or "")
    assert pnp.call_count <= 6
