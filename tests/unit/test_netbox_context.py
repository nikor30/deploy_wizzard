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


def test_context_computes_catalyst_center_dayn_values() -> None:
    """Flat CC values match the netbox_cc_dayn resolvers + the All_templates.csv
    example: uplink_ports/uplink_switch from cabling, site_vlans as
    (vid,name);…, support_contact from the site contact."""
    device = {
        "id": 1,
        "name": "SVEL051CIS",
        "tenant": {"name": "IT Operations"},
    }
    interfaces = [
        {
            "name": "Te1/1/3",
            "mgmt_only": False,
            "cable": {"id": 1},
            "connected_endpoints": [{"name": "Gi1/0/1", "device": {"name": "svel001cis_swv"}}],
        },
        {
            "name": "Te1/1/4",
            "mgmt_only": False,
            "cable": {"id": 2},
            "connected_endpoints": [{"name": "Gi1/0/2", "device": {"name": "svel001cis_swv"}}],
        },
    ]
    site_vlans = [
        {"vid": 99, "name": "Quarantine"},
        {"vid": 100, "name": "Medientechnik"},
        {"vid": 1010, "name": "G1_Data"},
    ]
    contacts = [{"contact": {"name": "Ladislav Fekete"}, "role": {"name": "Local IT"}}]
    context = build_device_context(device, interfaces, site_vlans, contacts)

    assert resolve_path(context, "device.uplink_ports") == "Te1/1/3,Te1/1/4"
    assert resolve_path(context, "device.uplink_switch") == "svel001cis_swv"
    assert (
        resolve_path(context, "device.site_vlans")
        == "(99,Quarantine);(100,Medientechnik);(1010,G1_Data)"
    )
    assert resolve_path(context, "device.support_contact") == "Ladislav Fekete"


def test_uplink_switch_none_when_ambiguous_contact_falls_back_to_tenant() -> None:
    device = {"id": 1, "name": "sw", "tenant": {"name": "IT Operations"}}
    interfaces = [
        {
            "name": "Te1/1/3",
            "cable": {"id": 1},
            "connected_endpoints": [{"name": "a", "device": {"name": "dist-a"}}],
        },
        {
            "name": "Te1/1/4",
            "cable": {"id": 2},
            "connected_endpoints": [{"name": "b", "device": {"name": "dist-b"}}],
        },
    ]
    context = build_device_context(device, interfaces, [], [])
    # two different far-end switches -> ambiguous -> unset
    assert resolve_path(context, "device.uplink_switch") is None
    # no contacts -> tenant fallback
    assert resolve_path(context, "device.support_contact") == "IT Operations"


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


# --- Day-N preview by serial (verify real data) ------------------------------

CCC = "https://ccc.example.com"


def _store_creds(client: TestClient) -> None:
    client.put(
        "/api/settings/credentials",
        json={
            "catalyst": {"base_url": CCC, "username": "admin", "secret": "pw"},
            "netbox": {"base_url": NETBOX, "secret": "tok"},
        },
    )


def _mock_netbox_device(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{NETBOX}/api/dcim/devices/").respond(
        200,
        json={
            "results": [
                {
                    "id": 1001,
                    "name": "SVEL051CIS.global.web-int.net",
                    "serial": "FOC21262B0R",
                    "site": {"id": 10, "name": "Velky Meder (VEL)"},
                    "location": {"id": 100, "name": "Warhouse Building 1"},
                    "rack": {"name": "01"},
                    "role": {"name": "access"},
                    "asset_tag": "FOC21262B0R",
                }
            ],
            "next": None,
        },
    )
    respx_mock.get(f"{NETBOX}/api/dcim/interfaces/").respond(
        200,
        json={
            "results": [
                {
                    "name": "Te1/1/3",
                    "cable": {"id": 1},
                    "connected_endpoints": [
                        {"name": "Gi1/0/1", "device": {"name": "svel001cis_swv"}}
                    ],
                }
            ],
            "next": None,
        },
    )
    respx_mock.get(f"{NETBOX}/api/ipam/vlans/").respond(
        200,
        json={"results": [{"vid": 1010, "name": "G1_Data"}], "next": None},
    )
    respx_mock.get(f"{NETBOX}/api/tenancy/contact-assignments/").respond(
        200,
        json={"results": [{"contact": {"name": "Ladislav Fekete"}}], "next": None},
    )


def test_preview_resolves_current_mappings_against_a_real_serial(client: TestClient) -> None:
    _store_creds(client)
    client.put(
        "/api/settings/dayn",
        json={
            "mappings": [
                {"variable": "site_full_name", "source_path": "device.site.name"},
                {"variable": "building_room", "source_path": "device.location.name"},
                {"variable": "uplink_ports", "source_path": "device.uplink_ports"},
                {"variable": "uplink_switch", "source_path": "device.uplink_switch"},
                {"variable": "support_contact", "source_path": "device.support_contact"},
                {"variable": "patch_field", "source_path": ""},
            ]
        },
    )
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_netbox_device(respx_mock)
        response = client.post("/api/settings/dayn/preview", json={"serial": "FOC21262B0R"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["netbox_name"] == "SVEL051CIS.global.web-int.net"
    assert body["netbox_site"] == "Velky Meder (VEL)"
    values = {v["variable"]: v["value"] for v in body["variables"]}
    assert values["site_full_name"] == "Velky Meder (VEL)"
    assert values["building_room"] == "Warhouse Building 1"
    assert values["uplink_ports"] == "Te1/1/3"
    assert values["uplink_switch"] == "svel001cis_swv"
    assert values["support_contact"] == "Ladislav Fekete"
    # unmapped variable is flagged manual, not resolved
    manual = {v["variable"]: v["source"] for v in body["variables"]}
    assert manual["patch_field"] == "manual"


def test_preview_survives_contact_assignments_400(client: TestClient) -> None:
    """Regression: some NetBox versions 400 on /tenancy/contact-assignments/.
    That must not break the whole preview — support_contact falls back to the
    tenant, everything else still resolves."""
    _store_creds(client)
    client.put(
        "/api/settings/dayn",
        json={
            "mappings": [
                {"variable": "site_full_name", "source_path": "device.site.name"},
                {"variable": "support_contact", "source_path": "device.support_contact"},
            ]
        },
    )
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.get(f"{NETBOX}/api/dcim/devices/").respond(
            200,
            json={
                "results": [
                    {
                        "id": 1001,
                        "name": "sw-1",
                        "serial": "SN1",
                        "site": {"id": 10, "name": "FFM-DC1"},
                        "tenant": {"name": "IT Operations"},
                    }
                ],
                "next": None,
            },
        )
        respx_mock.get(f"{NETBOX}/api/dcim/interfaces/").respond(
            200, json={"results": [], "next": None}
        )
        respx_mock.get(f"{NETBOX}/api/ipam/vlans/").respond(200, json={"results": [], "next": None})
        respx_mock.get(f"{NETBOX}/api/tenancy/contact-assignments/").respond(400, json={})
        response = client.post("/api/settings/dayn/preview", json={"serial": "SN1"})
    assert response.status_code == 200, response.text
    values = {v["variable"]: v["value"] for v in response.json()["variables"]}
    assert values["site_full_name"] == "FFM-DC1"
    # degraded gracefully to the tenant name instead of erroring
    assert values["support_contact"] == "IT Operations"


def test_preview_unknown_serial_is_404(client: TestClient) -> None:
    _store_creds(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.get(f"{NETBOX}/api/dcim/devices/").respond(
            200, json={"results": [], "next": None}
        )
        response = client.post("/api/settings/dayn/preview", json={"serial": "NOPE"})
    assert response.status_code == 404
    assert "NOPE" in response.json()["detail"]


# --- uplink / VLAN conventions ----------------------------------------------

ACCESS_DEVICE = {
    "id": 42,
    "name": "ssto145cis",
    "role": {"name": "access"},
    "site": {"id": 4, "name": "STO"},
}
CABLED = [
    {
        "name": "Te1/1/3",
        "mgmt_only": False,
        "connected_endpoints": [{"name": "Gi1/0/1", "device": {"name": "ssto199cis"}}],
    }
]
SITE_VLANS = [
    {"vid": 110, "name": "MGMT"},
    {"vid": 200, "name": "Access-Data"},
    {"vid": 999, "name": "CRITICAL_AUTH"},
]


def test_uplink_description_is_upl_plus_far_end_switch() -> None:
    ctx = build_device_context(ACCESS_DEVICE, CABLED, SITE_VLANS, [])
    assert ctx["device"]["uplink_switch"] == "ssto199cis"
    assert ctx["device"]["uplink_description"] == "UPL:ssto199cis"


def test_uplink_description_unset_without_a_unique_far_end() -> None:
    ctx = build_device_context(ACCESS_DEVICE, [], SITE_VLANS, [])
    assert ctx["device"]["uplink_description"] is None


def test_access_switch_gets_port_channel_1() -> None:
    ctx = build_device_context(ACCESS_DEVICE, CABLED, SITE_VLANS, [])
    assert ctx["device"]["po_id"] == "1"


def test_non_access_switch_leaves_port_channel_unset() -> None:
    device = {**ACCESS_DEVICE, "role": {"name": "distribution"}}
    ctx = build_device_context(device, CABLED, SITE_VLANS, [])
    assert ctx["device"]["po_id"] is None


def test_access_and_critical_vlans_matched_by_name() -> None:
    ctx = build_device_context(ACCESS_DEVICE, CABLED, SITE_VLANS, [])
    assert ctx["device"]["access_vlan"] == "200"
    assert ctx["device"]["critical_vlan"] == "999"


def test_ambiguous_vlan_name_stays_unset() -> None:
    """Two VLANs matching the keyword: the operator picks, we do not guess."""
    vlans = [{"vid": 200, "name": "Access-Data"}, {"vid": 201, "name": "Access-Voice"}]
    ctx = build_device_context(ACCESS_DEVICE, CABLED, vlans, [])
    assert ctx["device"]["access_vlan"] is None


def test_ambiguous_vlan_keyword_yields_a_suggestion_not_a_value() -> None:
    vlans = [
        {"vid": 201, "name": "Access-Voice"},
        {"vid": 200, "name": "Access-Data"},
        {"vid": 999, "name": "CRITICAL_AUTH"},
    ]
    ctx = build_device_context(ACCESS_DEVICE, CABLED, vlans, [])["device"]
    assert ctx["access_vlan"] is None
    assert ctx["access_vlan_suggested"] == "200"  # lowest VID of the matches
    # the unique critical match stays a confident value
    assert ctx["critical_vlan"] == "999"
    assert ctx["critical_vlan_suggested"] is None


# The real STO VLAN list: an exact "access" VLAN alongside names that merely
# contain the word, plus an exact "critical" VLAN.
STO_SITE_VLANS = [
    {"vid": 299, "name": "access"},
    {"vid": 1570, "name": "Time_Access"},
    {"vid": 1010, "name": "critical"},
    {"vid": 900, "name": "LAN_MGMT"},
]


def test_exact_vlan_name_wins_over_a_partial_one() -> None:
    """VLAN 299 is named exactly "access"; 1570 "Time_Access" merely contains
    the word and must not make the match ambiguous."""
    ctx = build_device_context(ACCESS_DEVICE, CABLED, STO_SITE_VLANS, [])["device"]
    assert ctx["access_vlan"] == "299"
    assert ctx["access_vlan_suggested"] is None
    assert ctx["critical_vlan"] == "1010"


def test_partial_matches_still_used_when_no_exact_name_exists() -> None:
    vlans = [{"vid": 1570, "name": "Time_Access"}, {"vid": 200, "name": "Access-Data"}]
    ctx = build_device_context(ACCESS_DEVICE, CABLED, vlans, [])["device"]
    assert ctx["access_vlan"] is None  # ambiguous
    assert ctx["access_vlan_suggested"] == "200"  # lowest VID, editable


def test_access_ports_are_the_uncabled_physical_ports() -> None:
    """The port template used to guess access ports from the "x/0/y" name
    pattern — which catches an uplink sitting on a front-panel port and misses
    a front-panel port on a module. NetBox knows what is cabled."""
    from app.services.dayn import build_device_context

    interfaces = [
        # cabled to another switch -> uplink, never an access port
        {
            "name": "TwoGigabitEthernet1/0/47",
            "type": {"value": "2.5gbase-t"},
            "connected_endpoints": [{"device": {"name": "core-01"}, "name": "Gi1/0/1"}],
        },
        {"name": "GigabitEthernet1/0/1", "type": {"value": "1000base-t"}},
        {"name": "GigabitEthernet1/0/2", "type": {"value": "1000base-t"}},
        # management port, SVI and the port-channel bundle are not access ports
        {"name": "GigabitEthernet0/0", "type": {"value": "1000base-t"}, "mgmt_only": True},
        {"name": "Vlan510", "type": {"value": "virtual"}},
        {"name": "Port-channel1", "type": {"value": "lag"}},
    ]
    ctx = build_device_context({"id": 1, "name": "sw-1"}, interfaces=interfaces)["device"]

    assert ctx["access_ports"] == "GigabitEthernet1/0/1,GigabitEthernet1/0/2"
    assert ctx["access_port_count"] == "2"
    assert ctx["uplink_ports"] == "TwoGigabitEthernet1/0/47"
