import pytest
from app.db.models import JobDevice
from app.errors import ConfigurationError
from app.services.day0 import build_claim_payload


def device(**overrides: object) -> JobDevice:
    defaults: dict[str, object] = {
        "serial": "FCW1234ABCD",
        "pid": "C9300-48P",
        "ccc_device_id": "pnp-1",
        "match_status": "matched",
        "netbox_device_id": 1,
        "netbox_name": "sw-ffm-01",
        "netbox_site_id": 10,
        "netbox_site_name": "FFM-DC1",
        "ccc_site_id": "uuid-ffm",
        "ccc_site_name": "Global/Germany/Frankfurt/DC1",
        "mgmt_ip": "172.20.10.5/24",
        "mgmt_vlan": 110,
    }
    defaults.update(overrides)
    return JobDevice(**defaults)  # type: ignore[arg-type]


def test_payload_shape_and_prefilled_variables() -> None:
    payload = build_claim_payload(device(), config_id="tmpl-1", image_id="img-1")
    assert payload["deviceId"] == "pnp-1"
    assert payload["siteId"] == "uuid-ffm"
    assert payload["type"] == "Default"
    assert payload["imageInfo"] == {"imageId": "img-1", "skip": False}
    assert payload["configInfo"]["configId"] == "tmpl-1"
    params = {p["key"]: p["value"] for p in payload["configInfo"]["configParameters"]}
    assert params["HOSTNAME"] == "sw-ffm-01"
    assert params["MGMT_IP"] == "172.20.10.5"
    assert params["MGMT_MASK"] == "255.255.255.0"
    assert params["MGMT_VLAN"] == "110"


def test_no_image_skips_image_install() -> None:
    payload = build_claim_payload(device(), config_id="tmpl-1", image_id=None)
    assert payload["imageInfo"] == {"imageId": "", "skip": True}


def test_missing_mgmt_ip_omits_ip_parameters() -> None:
    payload = build_claim_payload(device(mgmt_ip=None), config_id="tmpl-1", image_id=None)
    params = {p["key"] for p in payload["configInfo"]["configParameters"]}
    assert "MGMT_IP" not in params
    assert "MGMT_MASK" not in params
    assert "HOSTNAME" in params


def test_unmatched_device_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="matched"):
        build_claim_payload(device(match_status="unmatched"), config_id="t", image_id=None)


def test_missing_site_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="site"):
        build_claim_payload(device(ccc_site_id=None), config_id="t", image_id=None)
