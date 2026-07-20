"""Day-0 template variable introspection: prefilled vs open, gateway, debug flag."""

import respx
from app.db.models import JobDevice
from app.services.day0 import build_claim_payload, day0_builtins, resolve_day0_variables
from app.services.matching import MATCHED
from fastapi.testclient import TestClient

CCC = "https://ccc.example.com"
NETBOX = "https://netbox.example.com"


def _device(**kw: object) -> JobDevice:
    base: dict[str, object] = {
        "serial": "SN1",
        "ccc_device_id": "pnp-1",
        "match_status": MATCHED,
        "ccc_site_id": "uuid-ffm",
        "netbox_name": "sw-ffm-01",
        "mgmt_ip": "172.20.10.5/24",
        "mgmt_vlan": 900,
    }
    base.update(kw)
    return JobDevice(**base)


def test_builtins_include_gateway_guess_first_host() -> None:
    values = day0_builtins(_device())
    assert values["hostname"] == "sw-ffm-01"
    assert values["mgmt_ip"] == "172.20.10.5"
    assert values["mgmt_mask"] == "255.255.255.0"
    assert values["mgmt_prefix"] == "24"
    assert values["mgmt_vlan"] == "900"
    assert values["gateway"] == "172.20.10.1"  # first host of the /24


def test_builtins_include_subnet_and_vlan_name() -> None:
    device = _device(vlan_options=[{"id": 5, "vid": 900, "name": "MGMT"}])
    values = day0_builtins(device)
    assert values["mgmt_subnet"] == "172.20.10.0/24"
    assert values["mgmt_vlan_name"] == "MGMT"


def test_resolve_webasto_template_only_leaves_aes_open() -> None:
    """The real 00_Webasto_OnBoarding variables all resolve from NetBox +
    globals; only the AES key stays open (as a global set once)."""
    device = _device(vlan_options=[{"id": 5, "vid": 900, "name": "MGMT"}])
    context = {"device": {"role": {"name": "access"}}}
    variables = [
        "MGMT_IP",
        "MGMT_SUBNET",
        "MGMT_VLAN",
        "MGMT_VLAN_NAME",
        "DEFAULT_GATEWAY",
        "SWITCHTYPE",
        "CAMPUSSWITCH",
        "AES_ENCRYPTION_KEY",
        "PASSWORD_ENCRYPTION_KEY",
    ]
    resolved = resolve_day0_variables(
        variables,
        device,
        context,
        {},
        secret_names=["AES_ENCRYPTION_KEY", "PASSWORD_ENCRYPTION_KEY"],
    )
    assert resolved["MGMT_IP"] == {"value": "172.20.10.5", "source": "netbox"}
    assert resolved["MGMT_SUBNET"] == {"value": "172.20.10.0/24", "source": "netbox"}
    assert resolved["MGMT_VLAN"] == {"value": "900", "source": "netbox"}
    assert resolved["MGMT_VLAN_NAME"] == {"value": "MGMT", "source": "netbox"}
    assert resolved["SWITCHTYPE"] == {"value": "access", "source": "netbox"}
    assert resolved["CAMPUSSWITCH"] == {"value": "access", "source": "netbox"}
    # gateway is a guess, editable
    assert resolved["DEFAULT_GATEWAY"] == {"value": "172.20.10.1", "source": "manual"}
    # the AES/password keys come from the global variables (set once), masked
    assert resolved["AES_ENCRYPTION_KEY"] == {
        "value": "****",
        "source": "secret",
        "secret": "AES_ENCRYPTION_KEY",
    }
    assert resolved["PASSWORD_ENCRYPTION_KEY"]["source"] == "secret"
    # nothing left as open manual entry
    assert not [v for v in resolved.values() if v["source"] == "manual" and v["value"] == ""]


def test_claim_payload_decrypts_global_secret_values() -> None:
    device = _device()
    device.day0_variables = {
        "HOSTNAME": {"value": "sw-ffm-01", "source": "netbox"},
        "AES_KEY": {"value": "****", "source": "secret", "secret": "AES_KEY"},
    }
    payload = build_claim_payload(
        device,
        config_id="tpl-0",
        image_id=None,
        secret_values={"AES_KEY": "the-real-aes-key"},
    )
    params = {p["key"]: p["value"] for p in payload["configInfo"]["configParameters"]}
    assert params["AES_KEY"] == "the-real-aes-key"
    assert params["HOSTNAME"] == "sw-ffm-01"


def test_resolve_prefills_known_and_flags_gateway_manual() -> None:
    variables = ["HOSTNAME", "MGMT_IP", "MGMT_MASK", "GATEWAY", "MGMT_VLAN", "SNMP_LOCATION"]
    resolved = resolve_day0_variables(
        variables,
        _device(),
        {"device": {"custom_fields": {"snmp_location": "Rack 4"}}},
        {"SNMP_LOCATION": "device.custom_fields.snmp_location"},
    )
    assert resolved["HOSTNAME"] == {"value": "sw-ffm-01", "source": "netbox"}
    assert resolved["MGMT_IP"]["value"] == "172.20.10.5"
    # gateway is a guess -> editable manual field, prefilled with the suggestion
    assert resolved["GATEWAY"] == {"value": "172.20.10.1", "source": "manual"}
    # custom variable resolved via the Day-N mapping table
    assert resolved["SNMP_LOCATION"] == {"value": "Rack 4", "source": "mapped"}


def test_resolve_unmapped_variable_is_open_manual() -> None:
    resolved = resolve_day0_variables(["SITE_CODE"], _device(), {"device": {}}, {})
    assert resolved["SITE_CODE"] == {"value": "", "source": "manual"}


def test_claim_payload_uses_resolved_day0_variables_with_overrides() -> None:
    device = _device()
    device.day0_variables = {
        "HOSTNAME": {"value": "sw-ffm-01", "source": "netbox"},
        "GATEWAY": {"value": "172.20.10.1", "source": "manual"},
        "EMPTY": {"value": "", "source": "manual"},
    }
    payload = build_claim_payload(
        device, config_id="tpl-0", image_id=None, overrides={"GATEWAY": "172.20.10.254"}
    )
    params = {p["key"]: p["value"] for p in payload["configInfo"]["configParameters"]}
    assert params["HOSTNAME"] == "sw-ffm-01"
    assert params["GATEWAY"] == "172.20.10.254"  # operator override wins
    assert "EMPTY" not in params  # empty open fields are omitted


def _store_credentials(client: TestClient) -> None:
    client.put(
        "/api/settings/credentials",
        json={
            "catalyst": {"base_url": CCC, "username": "admin", "secret": "pw"},
            "netbox": {"base_url": NETBOX, "secret": "tok"},
        },
    )


def _matched_job(client: TestClient) -> int:
    from app.db.session import open_session

    job_id = client.post(
        "/api/wizard/jobs",
        json={"devices": [{"serial": "SN1", "pid": None, "ccc_device_id": "pnp-1"}]},
    ).json()["id"]
    device_id = client.get(f"/api/wizard/jobs/{job_id}").json()["devices"][0]["id"]
    with open_session() as db:
        device = db.get(JobDevice, device_id)
        assert device is not None
        device.match_status = MATCHED
        device.ccc_site_id = "uuid-ffm"
        device.netbox_name = "sw-ffm-01"
        device.netbox_device_id = 1001
        device.mgmt_ip = "172.20.10.5/24"
        device.mgmt_vlan = 900
    return int(job_id)


def test_day0_prepare_endpoint_previews_variables(client: TestClient) -> None:
    _store_credentials(client)
    job_id = _matched_job(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "t"})
        respx_mock.get(f"{CCC}/dna/intent/api/v1/template-programmer/template/tpl-0").respond(
            200,
            json={
                "id": "tpl-0",
                "templateParams": [
                    {"parameterName": "HOSTNAME"},
                    {"parameterName": "GATEWAY"},
                    {"parameterName": "SITE_CODE"},
                ],
            },
        )
        respx_mock.get(f"{NETBOX}/api/dcim/devices/1001/").respond(
            200, json={"id": 1001, "name": "sw-ffm-01", "site": {"id": 10}}
        )
        respx_mock.get(f"{NETBOX}/api/dcim/interfaces/").respond(
            200, json={"results": [], "next": None}
        )
        respx_mock.get(f"{NETBOX}/api/ipam/vlans/").respond(200, json={"results": [], "next": None})
        respx_mock.get(f"{NETBOX}/api/tenancy/contact-assignments/").respond(
            200, json={"results": [], "next": None}
        )
        response = client.post(
            f"/api/wizard/jobs/{job_id}/day0/prepare", json={"config_id": "tpl-0"}
        )
    assert response.status_code == 200, response.text
    variables = response.json()["devices"][0]["day0_variables"]
    assert variables["HOSTNAME"] == {"value": "sw-ffm-01", "source": "netbox"}
    assert variables["GATEWAY"] == {"value": "172.20.10.1", "source": "manual"}
    assert variables["SITE_CODE"] == {"value": "", "source": "manual"}


def test_debug_flag_roundtrip(client: TestClient) -> None:
    assert client.get("/api/settings/flags").json() == {"debug": False}
    assert client.put("/api/settings/flags", json={"debug": True}).json() == {"debug": True}
    assert client.get("/api/settings/flags").json() == {"debug": True}
