from typing import Any

import app.clients.base as base
import pytest
import respx
from app.clients.netbox import NetBoxClient
from app.services.matching import SiteMappingLookup, match_serials, normalize_serial

BASE = "https://netbox.example.com"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "BACKOFF_BASE_SECONDS", 0)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("fcw1234abcd", "FCW1234ABCD"), ("  FCW1234ABCD  ", "FCW1234ABCD"), ("a b", "A B")],
)
def test_normalize_serial(raw: str, normalized: str) -> None:
    assert normalize_serial(raw) == normalized


def nb_device(
    device_id: int,
    serial: str,
    site_id: int = 10,
    site_name: str = "FFM-DC1",
    primary_ip: str | None = "172.20.10.5/24",
) -> dict[str, Any]:
    return {
        "id": device_id,
        "name": f"sw-{device_id}",
        "serial": serial,
        "site": {"id": site_id, "name": site_name},
        "primary_ip4": {"address": primary_ip} if primary_ip else None,
        "status": {"value": "planned"},
    }


MAPPING: SiteMappingLookup = {(10, None): ("uuid-ffm", "Global/Germany/Frankfurt/DC1")}


def mock_planned(devices: list[dict[str, Any]]) -> None:
    respx.get(f"{BASE}/api/dcim/devices/").respond(200, json={"results": devices, "next": None})


def mock_vlans(vlans: list[dict[str, Any]]) -> None:
    respx.get(f"{BASE}/api/ipam/vlans/").respond(200, json={"results": vlans, "next": None})


@respx.mock
async def test_matched_device_with_messy_serial_and_primary_ip() -> None:
    mock_planned([nb_device(1, "  fcw1234abcd ")])
    mock_vlans([{"id": 5, "vid": 110, "name": "MGMT"}])
    async with NetBoxClient(BASE, "tok") as client:
        results = await match_serials(["FCW1234ABCD"], client, MAPPING)
    (result,) = results
    assert result.match_status == "matched"
    assert result.netbox_device_id == 1
    assert result.netbox_name == "sw-1"
    assert result.ccc_site_id == "uuid-ffm"
    assert result.mgmt_ip == "172.20.10.5/24"
    assert result.vlan_options == [{"id": 5, "vid": 110, "name": "MGMT"}]


@respx.mock
async def test_unmatched_serial() -> None:
    mock_planned([nb_device(1, "OTHER123")])
    async with NetBoxClient(BASE, "tok") as client:
        (result,) = await match_serials(["FCW1234ABCD"], client, MAPPING)
    assert result.match_status == "unmatched"
    assert result.netbox_device_id is None


@respx.mock
async def test_unmapped_site_blocks_but_keeps_netbox_data() -> None:
    mock_planned([nb_device(1, "FCW1234ABCD", site_id=99, site_name="Unmapped-Site")])
    mock_vlans([])
    async with NetBoxClient(BASE, "tok") as client:
        (result,) = await match_serials(["FCW1234ABCD"], client, MAPPING)
    assert result.match_status == "unmapped_site"
    assert result.netbox_site_name == "Unmapped-Site"
    assert result.ccc_site_id is None


@respx.mock
async def test_mgmt_ip_fallback_to_mgmt_interface() -> None:
    mock_planned([nb_device(1, "FCW1234ABCD", primary_ip=None)])
    mock_vlans([])
    respx.get(f"{BASE}/api/ipam/ip-addresses/").respond(
        200,
        json={
            "results": [
                {"address": "10.9.9.9/24", "assigned_object": {"name": "GigabitEthernet1/0/1"}},
                {"address": "172.20.10.7/24", "assigned_object": {"name": "Vlan110"}},
            ],
            "next": None,
        },
    )
    async with NetBoxClient(BASE, "tok") as client:
        (result,) = await match_serials(["FCW1234ABCD"], client, MAPPING)
    assert result.match_status == "matched"
    assert result.mgmt_ip == "172.20.10.7/24"


@respx.mock
async def test_missing_mgmt_ip_still_matches_with_none() -> None:
    mock_planned([nb_device(1, "FCW1234ABCD", primary_ip=None)])
    mock_vlans([])
    respx.get(f"{BASE}/api/ipam/ip-addresses/").respond(200, json={"results": [], "next": None})
    async with NetBoxClient(BASE, "tok") as client:
        (result,) = await match_serials(["FCW1234ABCD"], client, MAPPING)
    assert result.match_status == "matched"
    assert result.mgmt_ip is None


# --- management VLAN preselection -------------------------------------------


def test_preselects_the_site_management_vlan_by_name() -> None:
    from app.services.matching import preselect_mgmt_vlan

    vlans = [{"vid": 200, "name": "Data"}, {"vid": 900, "name": "MGMT"}]
    assert preselect_mgmt_vlan(vlans) == 900
    # spelled-out variant
    assert preselect_mgmt_vlan([{"vid": 901, "name": "Management"}]) == 901


def test_mgmt_vlan_preselect_skips_ambiguous_or_absent_names() -> None:
    from app.services.matching import preselect_mgmt_vlan

    # picking the wrong management VLAN strands the device -> operator decides
    ambiguous = [{"vid": 900, "name": "MGMT-A"}, {"vid": 901, "name": "MGMT-B"}]
    assert preselect_mgmt_vlan(ambiguous) is None
    assert preselect_mgmt_vlan([{"vid": 200, "name": "Data"}]) is None
    assert preselect_mgmt_vlan([]) is None


# --- VLAN gateway from NetBox IPAM -------------------------------------------


class _FakeNetBox:
    def __init__(self, addresses: list[dict[str, object]]) -> None:
        self._addresses = addresses

    async def get_vlan_ip_addresses(self, vlan_id: int) -> list[dict[str, object]]:
        return self._addresses


async def test_gateway_taken_from_the_ip_named_gateway() -> None:
    from app.services.matching import resolve_vlan_gateway

    client = _FakeNetBox(
        [
            {"address": "172.20.10.145/24", "dns_name": "ssto145cis"},
            {"address": "172.20.10.1/24", "dns_name": "sto-gateway.web-int.net"},
        ]
    )
    assert await resolve_vlan_gateway(client, 510) == "172.20.10.1"  # type: ignore[arg-type]


async def test_gateway_also_matched_from_the_description() -> None:
    from app.services.matching import resolve_vlan_gateway

    client = _FakeNetBox([{"address": "10.0.0.254/24", "description": "Default Gateway"}])
    assert await resolve_vlan_gateway(client, 510) == "10.0.0.254"  # type: ignore[arg-type]


async def test_no_or_ambiguous_gateway_falls_back_to_the_subnet_guess() -> None:
    """A wrong default gateway strands the device, so two candidates mean the
    caller keeps its first-host guess instead."""
    from app.services.matching import resolve_vlan_gateway

    none_named = _FakeNetBox([{"address": "10.0.0.5/24", "dns_name": "host"}])
    assert await resolve_vlan_gateway(none_named, 510) is None  # type: ignore[arg-type]

    ambiguous = _FakeNetBox(
        [
            {"address": "10.0.0.1/24", "dns_name": "gateway-a"},
            {"address": "10.0.0.2/24", "dns_name": "gateway-b"},
        ]
    )
    assert await resolve_vlan_gateway(ambiguous, 510) is None  # type: ignore[arg-type]
