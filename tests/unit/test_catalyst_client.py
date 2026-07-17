from typing import Any

import app.clients.base as base
import httpx
import pytest
import respx
from app.clients.catalyst import PAGE_SIZE, CatalystCenterClient
from app.errors import CatalystAuthError, CatalystError

BASE = "https://ccc.example.com"
TOKEN_URL = f"{BASE}/dna/system/api/v1/auth/token"
SITE_URL = f"{BASE}/dna/intent/api/v1/site"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "BACKOFF_BASE_SECONDS", 0)


def sites(count: int) -> dict[str, Any]:
    return {"response": [{"id": f"site-{i}", "siteName": f"Site {i}"} for i in range(count)]}


@respx.mock
async def test_token_fetch_and_header() -> None:
    route = respx.post(TOKEN_URL).respond(200, json={"Token": "tok-1"})
    site_route = respx.get(SITE_URL).respond(200, json=sites(1))
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        await client.get_sites()
    assert route.called
    assert site_route.calls[0].request.headers["X-Auth-Token"] == "tok-1"


@respx.mock
async def test_bad_credentials_raise_auth_error() -> None:
    respx.post(TOKEN_URL).respond(401)
    async with CatalystCenterClient(BASE, "admin", "wrong") as client:
        with pytest.raises(CatalystAuthError, match="credentials"):
            await client.get_sites()


@respx.mock
async def test_401_refreshes_token_exactly_once_then_fails_loudly() -> None:
    token_route = respx.post(TOKEN_URL)
    token_route.side_effect = [
        httpx.Response(200, json={"Token": "tok-old"}),
        httpx.Response(200, json={"Token": "tok-new"}),
    ]
    site_route = respx.get(SITE_URL)
    site_route.side_effect = [httpx.Response(401), httpx.Response(200, json=sites(1))]

    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        result = await client.get_sites()
    assert len(result) == 1
    assert token_route.call_count == 2
    assert site_route.calls[1].request.headers["X-Auth-Token"] == "tok-new"


@respx.mock
async def test_persistent_401_after_refresh_raises_auth_error() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(SITE_URL).respond(401)
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        with pytest.raises(CatalystAuthError, match="after a token refresh"):
            await client.get_sites()


@respx.mock
async def test_expired_token_is_refreshed_proactively() -> None:
    token_route = respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(SITE_URL).respond(200, json=sites(1))
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        await client.get_sites()
        client._token_fetched_at -= 56 * 60  # age the token past the 55-min window
        await client.get_sites()
    assert token_route.call_count == 2


@respx.mock
async def test_get_retries_on_5xx_then_succeeds() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    site_route = respx.get(SITE_URL)
    site_route.side_effect = [
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json=sites(2)),
    ]
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        assert len(await client.get_sites()) == 2
    assert site_route.call_count == 3


@respx.mock
async def test_persistent_5xx_raises_catalyst_error() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(SITE_URL).respond(503)
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        with pytest.raises(CatalystError, match="503"):
            await client.get_sites()


@respx.mock
async def test_pagination_collects_all_pages() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    site_route = respx.get(SITE_URL)
    site_route.side_effect = [
        httpx.Response(200, json=sites(PAGE_SIZE)),
        httpx.Response(200, json=sites(3)),
    ]
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        result = await client.get_sites()
    assert len(result) == PAGE_SIZE + 3
    first, second = (call.request.url.params for call in site_route.calls)
    assert first["offset"] == "1"
    assert second["offset"] == str(1 + PAGE_SIZE)


@respx.mock
async def test_pnp_devices_passes_state_param() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    pnp_route = respx.get(f"{BASE}/dna/intent/api/v1/onboarding/pnp-device").respond(
        200, json={"response": []}
    )
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        await client.get_pnp_devices()
    assert pnp_route.calls[0].request.url.params["state"] == "Unclaimed"


@respx.mock
async def test_pnp_devices_accepts_bare_array_response() -> None:
    """Live CCC 2.3.7 returns the PnP list as a bare JSON array (no 'response'
    wrapper) — regression for the wizard 500 on real Catalyst Center."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    pnp_route = respx.get(f"{BASE}/dna/intent/api/v1/onboarding/pnp-device")
    pnp_route.side_effect = [
        httpx.Response(
            200,
            json=[
                {"id": f"pnp-{i}", "deviceInfo": {"serialNumber": f"SN{i}"}}
                for i in range(PAGE_SIZE)
            ],
        ),
        httpx.Response(200, json=[{"id": "pnp-x", "deviceInfo": {"serialNumber": "SNX"}}]),
    ]
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        devices = await client.get_pnp_devices()
    assert len(devices) == PAGE_SIZE + 1
    assert pnp_route.call_count == 2


@respx.mock
async def test_unexpected_pagination_shape_raises_typed_error() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(SITE_URL).respond(200, json={"response": "not-a-list"})
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        with pytest.raises(CatalystError, match="Unexpected response shape"):
            await client.get_sites()
