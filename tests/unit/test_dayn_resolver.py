"""Dot-path resolver for Day-N template variables."""

from app.services.dayn import resolve_path, resolve_variables

DEVICE = {
    "id": 1,
    "name": "sw-ffm-01",
    "serial": "FCW1234ABCD",
    "site": {"id": 10, "name": "FFM-DC1", "slug": "ffm-dc1"},
    "primary_ip4": {"address": "172.20.10.5/24"},
    "custom_fields": {"snmp_location": "FFM DC1 Rack 12", "empty_field": None},
    "config_context": {"ntp": {"servers": ["10.0.0.1", "10.0.0.2"]}},
}


def test_resolves_plain_and_nested_fields() -> None:
    assert resolve_path({"device": DEVICE}, "device.name") == "sw-ffm-01"
    assert resolve_path({"device": DEVICE}, "device.site.name") == "FFM-DC1"
    assert resolve_path({"device": DEVICE}, "device.custom_fields.snmp_location") == (
        "FFM DC1 Rack 12"
    )
    assert resolve_path({"device": DEVICE}, "device.config_context.ntp.servers.0") == "10.0.0.1"


def test_missing_or_null_paths_resolve_to_none() -> None:
    assert resolve_path({"device": DEVICE}, "device.nonexistent") is None
    assert resolve_path({"device": DEVICE}, "device.custom_fields.empty_field") is None
    assert resolve_path({"device": DEVICE}, "device.site.name.too.deep") is None
    assert resolve_path({"device": DEVICE}, "") is None


def test_resolve_variables_marks_unmapped_and_unresolvable_as_manual() -> None:
    mappings = {
        "SNMP_LOCATION": "device.custom_fields.snmp_location",
        "NTP_SERVER": "device.config_context.ntp.servers.0",
        "BROKEN": "device.does.not.exist",
    }
    variables = ["SNMP_LOCATION", "NTP_SERVER", "BROKEN", "UNMAPPED"]
    resolved = resolve_variables(variables, mappings, {"device": DEVICE})
    assert resolved["SNMP_LOCATION"] == {"value": "FFM DC1 Rack 12", "source": "mapped"}
    assert resolved["NTP_SERVER"] == {"value": "10.0.0.1", "source": "mapped"}
    assert resolved["BROKEN"] == {"value": None, "source": "manual"}
    assert resolved["UNMAPPED"] == {"value": None, "source": "manual"}


def test_non_string_values_are_stringified() -> None:
    resolved = resolve_variables(["SITE_ID"], {"SITE_ID": "device.site.id"}, {"device": DEVICE})
    assert resolved["SITE_ID"] == {"value": "10", "source": "mapped"}
