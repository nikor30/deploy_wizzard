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


# --- built-in NetBox aliases (ported from netbox_cc_dayn mappings.yaml) -------

STO_DEVICE = {
    "id": 42,
    "name": "ssto145cis",
    "serial": "FOC2335U0FT",
    "asset_tag": "AST-0099",
    "site": {"id": 4, "name": "STO"},
    "location": {"id": 63, "name": "IT-Lager"},
    "rack": {"id": 1, "name": "Rack001"},
    "position": 34,
    "role": {"name": "access"},
    "tenant": {"name": "Webasto"},
    # values build_device_context() derives from interfaces/VLANs/contacts
    "support_contact": "Holger Jahl",
    "uplink_switch": "ssto199cis",
    "uplink_ports": "Te1/1/3,Te1/1/4",
    "site_vlans": "(110,MGMT);(900,DATA)",
}


def test_dayn_aliases_fill_the_real_it_dayn_variables() -> None:
    """The IT-DayN template's fields must prefill from NetBox without the
    operator having to map every one of them by hand first."""
    variables = [
        "SITE_FULL_NAME",
        "BASIC LOCATION INFORMATION",
        "BUILDING_ROOM",
        "DEVICE_ROLE",
        "ASSET_ID",
        "RACK_ID",
        "RACK_POSITION",
        "SUPPORT_CONTACT",
        "UPLINK_SWITCH",
        "UPLINK_PORTS",
        "ARRVLANS",
    ]
    resolved = resolve_variables(variables, {}, {"device": STO_DEVICE})
    values = {name: info["value"] for name, info in resolved.items()}
    assert values == {
        "SITE_FULL_NAME": "STO",
        "BASIC LOCATION INFORMATION": "IT-Lager",
        "BUILDING_ROOM": "IT-Lager",
        "DEVICE_ROLE": "access",
        "ASSET_ID": "AST-0099",
        "RACK_ID": "Rack001",
        "RACK_POSITION": "34",
        "SUPPORT_CONTACT": "Holger Jahl",
        "UPLINK_SWITCH": "ssto199cis",
        "UPLINK_PORTS": "Te1/1/3,Te1/1/4",
        "ARRVLANS": "(110,MGMT);(900,DATA)",
    }
    assert {info["source"] for info in resolved.values()} == {"netbox"}


def test_explicit_mapping_overrides_the_builtin_alias() -> None:
    resolved = resolve_variables(
        ["RACK_ID"], {"RACK_ID": "device.site.name"}, {"device": STO_DEVICE}
    )
    assert resolved["RACK_ID"] == {"value": "STO", "source": "mapped"}


def test_variables_without_a_netbox_source_stay_manual() -> None:
    """Design choices (VLAN ids, port-channel) have no NetBox source and must
    remain open fields rather than being guessed."""
    variables = ["PRIMARYVLAN", "SECONDARYVLAN", "NATIVE_VLAN_ID", "PO_ID", "PVLAN"]
    resolved = resolve_variables(variables, {}, {"device": STO_DEVICE})
    assert all(info["source"] == "manual" for info in resolved.values())
    assert set(resolved) == set(variables)


def test_ccc_internal_and_junk_variables_are_hidden() -> None:
    """__device/__interface are CCC bindings it fills itself, and
    OaMGKyQBNwDjxFcagpT is a leaked password value — neither is operator input."""
    variables = ["HOSTNAME", "__device", "__interface", "OaMGKyQBNwDjxFcagpT"]
    resolved = resolve_variables(variables, {}, {"device": STO_DEVICE})
    assert set(resolved) == {"HOSTNAME"}


def test_alias_missing_in_netbox_falls_back_to_manual() -> None:
    """A device without an asset tag leaves ASSET_ID open instead of empty."""
    device = {k: v for k, v in STO_DEVICE.items() if k != "asset_tag"}
    resolved = resolve_variables(["ASSET_ID"], {}, {"device": device})
    assert resolved["ASSET_ID"] == {"value": None, "source": "manual"}


def test_private_vlan_variables_are_optional() -> None:
    """PVLAN config only applies to switches that use private VLANs, so it must
    never gate a deploy."""
    variables = ["PVLAN", "PRIMARYVLAN", "SECONDARYVLAN"]
    resolved = resolve_variables(variables, {}, {"device": STO_DEVICE})
    for name in variables:
        assert resolved[name] == {"value": None, "source": "manual", "optional": True}


def test_other_manual_variables_stay_required() -> None:
    resolved = resolve_variables(["PO_ID", "NATIVE_VLAN_ID"], {}, {"device": STO_DEVICE})
    assert all("optional" not in info for info in resolved.values())
