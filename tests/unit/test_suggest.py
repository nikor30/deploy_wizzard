"""Auto-suggest matching: NetBox↔CCC sites and Day-N variable dot-paths."""

from app.services.suggest import (
    candidate_paths,
    suggest_site_mappings,
    suggest_variable_mappings,
)

CCC_SITES = [
    {"id": "uuid-global", "name_hierarchy": "Global"},
    {"id": "uuid-ffm", "name_hierarchy": "Global/Germany/Frankfurt/DC1"},
    {"id": "uuid-muc", "name_hierarchy": "Global/Germany/Munich/Campus"},
    {"id": "uuid-ber", "name_hierarchy": "Global/Germany/Berlin/BER-Office"},
]


def _by_netbox(suggestions: list[dict]) -> dict[str, dict]:
    return {s["netbox_site_name"]: s for s in suggestions}


def test_exact_leaf_name_wins_with_high_confidence() -> None:
    netbox = [{"id": 1, "name": "DC1", "slug": "dc1"}]
    (suggestion,) = suggest_site_mappings(netbox, CCC_SITES)
    assert suggestion["ccc_site_id"] == "uuid-ffm"
    assert suggestion["confidence"] >= 0.9


def test_token_and_abbreviation_matching() -> None:
    netbox = [
        {"id": 1, "name": "FFM-DC1", "slug": "ffm-dc1"},
        {"id": 2, "name": "Munich Campus", "slug": "munich-campus"},
        {"id": 3, "name": "BER Office", "slug": "ber-office"},
    ]
    by_name = _by_netbox(suggest_site_mappings(netbox, CCC_SITES))
    assert by_name["FFM-DC1"]["ccc_site_id"] == "uuid-ffm"
    assert by_name["Munich Campus"]["ccc_site_id"] == "uuid-muc"
    assert by_name["BER Office"]["ccc_site_id"] == "uuid-ber"


def test_each_ccc_site_assigned_at_most_once_best_first() -> None:
    netbox = [
        {"id": 1, "name": "Munich Campus", "slug": None},
        {"id": 2, "name": "Munich", "slug": None},
    ]
    suggestions = suggest_site_mappings(netbox, CCC_SITES)
    targets = [s["ccc_site_id"] for s in suggestions]
    assert len(targets) == len(set(targets)), "one CCC site suggested twice"
    # the better (more specific) match keeps the site
    assert _by_netbox(suggestions)["Munich Campus"]["ccc_site_id"] == "uuid-muc"


def test_no_suggestion_below_threshold() -> None:
    netbox = [{"id": 1, "name": "Tokyo-Warehouse", "slug": "tokyo-warehouse"}]
    assert suggest_site_mappings(netbox, CCC_SITES) == []


DEVICE = {
    "id": 1001,
    "name": "sw-ffm-01",
    "serial": "SN000001",
    "status": {"value": "planned"},
    "site": {"id": 10, "name": "FFM-DC1"},
    "primary_ip4": {"address": "172.20.10.1/24"},
    "custom_fields": {"snmp_location": "FFM DC1 / Rack 4", "contact_email": "noc@x.de"},
    "config_context": {"ntp_server": "10.0.0.1", "syslog": {"host": "10.0.0.9"}},
}


def test_candidate_paths_cover_fields_custom_fields_and_config_context() -> None:
    paths = candidate_paths(DEVICE)
    assert "device.name" in paths
    assert "device.serial" in paths
    assert "device.site.name" in paths
    assert "device.primary_ip4.address" in paths
    assert "device.custom_fields.snmp_location" in paths
    assert "device.config_context.ntp_server" in paths
    assert "device.config_context.syslog.host" in paths
    # non-scalars are never suggested as a value source
    assert "device.custom_fields" not in paths
    assert "device.status" not in paths


def test_variable_suggestions_use_synonyms_and_token_match() -> None:
    variables = ["HOSTNAME", "SNMP_LOCATION", "NTP_SERVER", "MGMT_IP", "RADIUS_KEY"]
    result = suggest_variable_mappings(variables, DEVICE)
    assert result["HOSTNAME"]["source_path"] == "device.name"
    assert result["SNMP_LOCATION"]["source_path"] == "device.custom_fields.snmp_location"
    assert result["NTP_SERVER"]["source_path"] == "device.config_context.ntp_server"
    assert result["MGMT_IP"]["source_path"] == "device.primary_ip4.address"
    for variable in ("HOSTNAME", "SNMP_LOCATION", "NTP_SERVER"):
        assert result[variable]["confidence"] > 0.5
    # nothing plausible in the device data -> no guess, manual entry stays
    assert result["RADIUS_KEY"]["source_path"] is None


def test_variable_suggestions_more_synonyms() -> None:
    result = suggest_variable_mappings(["DEVICE_SERIAL", "SITE", "SYSLOG_HOST", "CONTACT"], DEVICE)
    assert result["DEVICE_SERIAL"]["source_path"] == "device.serial"
    assert result["SITE"]["source_path"] == "device.site.name"
    assert result["SYSLOG_HOST"]["source_path"] == "device.config_context.syslog.host"
    assert result["CONTACT"]["source_path"] == "device.custom_fields.contact_email"


# --- API endpoints -----------------------------------------------------------

import respx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CCC = "https://ccc.example.com"
NETBOX = "https://netbox.example.com"


def _store_credentials(client: TestClient) -> None:
    client.put(
        "/api/settings/credentials",
        json={
            "catalyst": {"base_url": CCC, "username": "admin", "secret": "pw"},
            "netbox": {"base_url": NETBOX, "secret": "tok"},
        },
    )


def test_site_suggestions_endpoint_skips_already_mapped(client: TestClient) -> None:
    _store_credentials(client)
    client.put(
        "/api/mappings/sites",
        json={
            "mappings": [
                {
                    "netbox_site_id": 20,
                    "netbox_site_name": "Munich Campus",
                    "ccc_site_id": "uuid-muc",
                    "ccc_site_name": "Global/Germany/Munich/Campus",
                }
            ]
        },
    )
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "t"})
        respx_mock.get(f"{CCC}/dna/intent/api/v1/site").respond(
            200,
            json={
                "response": [
                    {"id": "uuid-ffm", "siteNameHierarchy": "Global/Germany/Frankfurt/DC1"},
                    {"id": "uuid-muc", "siteNameHierarchy": "Global/Germany/Munich/Campus"},
                ]
            },
        )
        respx_mock.get(f"{NETBOX}/api/dcim/locations/").respond(
            200, json={"results": [], "next": None}
        )
        respx_mock.get(f"{NETBOX}/api/dcim/sites/").respond(
            200,
            json={
                "results": [
                    {"id": 10, "name": "FFM-DC1", "slug": "ffm-dc1"},
                    {"id": 20, "name": "Munich Campus", "slug": "munich-campus"},
                ],
                "next": None,
            },
        )
        response = client.get("/api/mappings/sites/suggest")
    assert response.status_code == 200
    (suggestion,) = response.json()
    assert suggestion["netbox_site_id"] == 10
    assert suggestion["ccc_site_id"] == "uuid-ffm"
    assert 0 < suggestion["confidence"] <= 1


def test_dayn_suggestions_endpoint(client: TestClient) -> None:
    _store_credentials(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "t"})
        respx_mock.get(f"{CCC}/dna/intent/api/v1/template-programmer/template/tpl-1").respond(
            200,
            json={
                "id": "tpl-1",
                "templateParams": [
                    {"parameterName": "HOSTNAME"},
                    {"parameterName": "SNMP_LOCATION"},
                    {"parameterName": "RADIUS_KEY"},
                ],
            },
        )
        respx_mock.get(f"{NETBOX}/api/dcim/devices/").respond(
            200,
            json={
                "results": [
                    {
                        "id": 1,
                        "name": "sw-1",
                        "serial": "SN1",
                        "site": {"id": 10, "name": "FFM-DC1"},
                        "primary_ip4": {"address": "172.20.10.5/24"},
                        "status": {"value": "planned"},
                        "custom_fields": {"snmp_location": "Rack 4"},
                    }
                ],
                "next": None,
            },
        )
        respx_mock.get(f"{NETBOX}/api/dcim/interfaces/").respond(
            200, json={"results": [], "next": None}
        )
        respx_mock.get(f"{NETBOX}/api/ipam/vlans/").respond(200, json={"results": [], "next": None})
        respx_mock.get(f"{NETBOX}/api/tenancy/contact-assignments/").respond(
            200, json={"results": [], "next": None}
        )
        response = client.post("/api/settings/dayn/suggest", json={"template_id": "tpl-1"})
    assert response.status_code == 200
    by_variable = {s["variable"]: s for s in response.json()}
    assert by_variable["HOSTNAME"]["source_path"] == "device.name"
    assert by_variable["SNMP_LOCATION"]["source_path"] == "device.custom_fields.snmp_location"
    assert by_variable["RADIUS_KEY"]["source_path"] is None


def test_dayn_suggestions_need_a_netbox_device(client: TestClient) -> None:
    _store_credentials(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "t"})
        respx_mock.get(f"{CCC}/dna/intent/api/v1/template-programmer/template/tpl-1").respond(
            200, json={"id": "tpl-1", "templateParams": [{"parameterName": "X"}]}
        )
        respx_mock.get(f"{NETBOX}/api/dcim/devices/").respond(
            200, json={"results": [], "next": None}
        )
        response = client.post("/api/settings/dayn/suggest", json={"template_id": "tpl-1"})
    assert response.status_code == 422
    assert "NetBox" in response.json()["detail"]
