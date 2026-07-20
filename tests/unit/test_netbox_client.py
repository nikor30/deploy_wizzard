import app.clients.base as base
import httpx
import pytest
import respx
from app.clients.netbox import NetBoxClient
from app.errors import NetBoxAuthError, NetBoxError, NetBoxNotFound

BASE = "https://netbox.example.com"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "BACKOFF_BASE_SECONDS", 0)


@respx.mock
async def test_token_header_and_status() -> None:
    route = respx.get(f"{BASE}/api/status/").respond(200, json={"netbox-version": "4.1.3"})
    async with NetBoxClient(BASE, "nb-token") as client:
        assert await client.test_connection() == "4.1.3"
    assert route.calls[0].request.headers["Authorization"] == "Token nb-token"


@respx.mock
async def test_invalid_token_raises_auth_error() -> None:
    respx.get(f"{BASE}/api/status/").respond(403)
    async with NetBoxClient(BASE, "bad") as client:
        with pytest.raises(NetBoxAuthError, match="token"):
            await client.test_connection()


@respx.mock
async def test_pagination_follows_next_links() -> None:
    devices = respx.get(f"{BASE}/api/dcim/devices/")
    devices.side_effect = [
        httpx.Response(
            200,
            json={
                "results": [{"id": 1}, {"id": 2}],
                "next": f"{BASE}/api/dcim/devices/?limit=2&offset=2",
            },
        ),
        httpx.Response(200, json={"results": [{"id": 3}], "next": None}),
    ]
    async with NetBoxClient(BASE, "tok") as client:
        result = await client.get_devices(status="planned")
    assert [d["id"] for d in result] == [1, 2, 3]
    assert devices.calls[0].request.url.params["status"] == "planned"
    # The next link already carries the query string; params must not be re-sent.
    assert "status" not in devices.calls[1].request.url.params


@respx.mock
async def test_404_maps_to_not_found() -> None:
    respx.patch(f"{BASE}/api/dcim/devices/999/").respond(404)
    async with NetBoxClient(BASE, "tok") as client:
        with pytest.raises(NetBoxNotFound, match="999"):
            await client.patch_device_status(999, "active")


@respx.mock
async def test_patch_device_status_sends_payload() -> None:
    route = respx.patch(f"{BASE}/api/dcim/devices/42/").respond(
        200, json={"id": 42, "status": {"value": "active"}}
    )
    async with NetBoxClient(BASE, "tok") as client:
        result = await client.patch_device_status(42, "active")
    assert result["id"] == 42
    assert route.calls[0].request.content == b'{"status":"active"}'


@respx.mock
async def test_get_retries_on_5xx_then_succeeds() -> None:
    route = respx.get(f"{BASE}/api/status/")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(200, json={"netbox-version": "4.1.3"}),
    ]
    async with NetBoxClient(BASE, "tok") as client:
        assert await client.test_connection() == "4.1.3"
    assert route.call_count == 2


@respx.mock
async def test_unreachable_host_raises_netbox_error() -> None:
    respx.get(f"{BASE}/api/status/").side_effect = httpx.ConnectError("refused")
    async with NetBoxClient(BASE, "tok") as client:
        with pytest.raises(NetBoxError, match="Cannot reach"):
            await client.test_connection()


@respx.mock
async def test_contact_assignments_falls_back_content_type_then_empty() -> None:
    """object_type/content_type filter name differs by NetBox version; a 400
    on one is retried with the other, and total failure degrades to []."""
    route = respx.get(f"{BASE}/api/tenancy/contact-assignments/")
    route.side_effect = [
        httpx.Response(400),  # object_type rejected
        httpx.Response(
            200,
            json={"results": [{"contact": {"name": "Local Admin"}}], "next": None},
        ),
    ]
    async with NetBoxClient(BASE, "tok") as client:
        result = await client.get_contact_assignments("dcim.site", 10)
    assert [c["contact"]["name"] for c in result] == ["Local Admin"]
    first, second = (call.request.url.params for call in route.calls)
    assert "object_type" in first
    assert "content_type" in second


@respx.mock
async def test_contact_assignments_both_400_returns_empty() -> None:
    respx.get(f"{BASE}/api/tenancy/contact-assignments/").respond(400)
    async with NetBoxClient(BASE, "tok") as client:
        assert await client.get_contact_assignments("dcim.site", 10) == []
