"""Location-aware site resolution + enriched Day-N device context."""

import respx
from app.services.dayn import build_device_context, resolve_path
from app.services.matching import resolve_ccc_site
from app.services.suggest import candidate_paths
from fastapi.testclient import TestClient

# --- location-walk resolution ------------------------------------------------

MAPPINGS = {
    (10, None): ("uuid-site", "Global/FFM"),
    (10, 100): ("uuid-building", "Global/FFM/BuildingA"),
}
PARENTS = {100: None, 101: 100, 102: 101}


def test_device_location_walks_up_to_mapped_building() -> None:
    # device on Floor 1 (101) -> Building A (100) is the most specific mapping
    assert resolve_ccc_site(10, 101, MAPPINGS, PARENTS) == ("uuid-building", "Global/FFM/BuildingA")


def test_exact_location_mapping_wins_over_parent() -> None:
    mappings = dict(MAPPINGS)
    mappings[(10, 101)] = ("uuid-floor", "Global/FFM/BuildingA/Floor1")
    assert resolve_ccc_site(10, 101, mappings, PARENTS) == (
        "uuid-floor",
        "Global/FFM/BuildingA/Floor1",
    )


def test_device_without_location_falls_back_to_site() -> None:
    assert resolve_ccc_site(10, None, MAPPINGS, PARENTS) == ("uuid-site", "Global/FFM")


def test_unmapped_site_returns_none() -> None:
    assert resolve_ccc_site(99, 101, MAPPINGS, PARENTS) is None


# --- device context ----------------------------------------------------------

DEVICE = {
    "id": 1,
    "name": "sw-1",
    "primary_ip4": {"address": "172.20.10.5/24"},
}
INTERFACES = [
    {
        "name": "Te1/1/1",
        "type": {"value": "10gbase-x-sfpp"},
        "mgmt_only": False,
        "description": "uplink",
        "cable": {"id": 7},
        "connected_endpoints": [{"name": "Te1/0/24", "device": {"name": "dist-01"}}],
    },
    {"name": "Gi1/0/1", "type": {"value": "1000base-t"}, "mgmt_only": False, "cable": None},
    {"name": "Gi0/0", "mgmt_only": True, "cable": {"id": 8}},
]


def test_context_exposes_uplinks_and_mgmt_network() -> None:
    context = build_device_context(DEVICE, INTERFACES)
    assert resolve_path(context, "device.uplinks.0.name") == "Te1/1/1"
    assert resolve_path(context, "device.uplinks.0.peer_device") == "dist-01"
    assert resolve_path(context, "device.uplinks.0.peer_interface") == "Te1/0/24"
    assert resolve_path(context, "device.mgmt.ip") == "172.20.10.5"
    assert resolve_path(context, "device.mgmt.netmask") == "255.255.255.0"
    assert resolve_path(context, "device.mgmt.network") == "172.20.10.0"
    assert resolve_path(context, "device.mgmt.prefix_length") == "24"
    # unconnected access port and mgmt-only interface are not uplinks
    assert resolve_path(context, "device.uplinks.1.name") is None


def test_candidate_paths_cover_uplinks_and_mgmt() -> None:
    paths = candidate_paths(build_device_context(DEVICE, INTERFACES)["device"])
    assert "device.uplinks.0.name" in paths
    assert "device.uplinks.0.peer_device" in paths
    assert "device.mgmt.ip" in paths
    assert "device.mgmt.netmask" in paths


# --- sources endpoint with locations ----------------------------------------

NETBOX = "https://netbox.example.com"


def test_netbox_sources_include_location_paths(client: TestClient) -> None:
    client.put(
        "/api/settings/credentials",
        json={"netbox": {"base_url": NETBOX, "secret": "tok"}},
    )
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.get(f"{NETBOX}/api/dcim/sites/").respond(
            200,
            json={"results": [{"id": 10, "name": "FFM-DC1", "slug": "ffm-dc1"}], "next": None},
        )
        respx_mock.get(f"{NETBOX}/api/dcim/locations/").respond(
            200,
            json={
                "results": [
                    {"id": 100, "name": "Building A", "site": {"id": 10}, "parent": None},
                    {
                        "id": 101,
                        "name": "Floor 1",
                        "site": {"id": 10},
                        "parent": {"id": 100},
                    },
                ],
                "next": None,
            },
        )
        response = client.get("/api/mappings/sources/netbox")
    assert response.status_code == 200, response.text
    names = {e["name"]: e for e in response.json()}
    assert "FFM-DC1" in names
    assert names["FFM-DC1 / Building A"]["location_id"] == 100
    floor = names["FFM-DC1 / Building A / Floor 1"]
    assert floor["site_id"] == 10 and floor["location_id"] == 101


def test_location_mapping_roundtrip_and_duplicate_rejection(client: TestClient) -> None:
    item = {
        "netbox_site_id": 10,
        "netbox_site_name": "FFM-DC1",
        "netbox_location_id": 100,
        "netbox_location_name": "FFM-DC1 / Building A",
        "ccc_site_id": "uuid-building",
        "ccc_site_name": "Global/FFM/BuildingA",
    }
    saved = client.put("/api/mappings/sites", json={"mappings": [item]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["mappings"][0]["netbox_location_id"] == 100

    # same site twice is fine when the locations differ
    site_level = {**item, "netbox_location_id": None, "netbox_location_name": None}
    both = client.put("/api/mappings/sites", json={"mappings": [item, site_level]})
    assert both.status_code == 200
    assert len(both.json()["mappings"]) == 2

    duplicate = client.put("/api/mappings/sites", json={"mappings": [item, item]})
    assert duplicate.status_code == 422
