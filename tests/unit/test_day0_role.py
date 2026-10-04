"""The CCC inventory role/tag comes from the resolved Day-0 variables."""

from app.services.day0 import _with_overrides, ccc_role, ccc_tag_name, device_role_name


def test_role_name_reads_resolved_variable_entries() -> None:
    # Regression: day0_variables hold {"value", "source"} entries. str() of the
    # entry became the role name and a junk CCC tag "_value_Access_source_netbox_".
    resolved = {
        "HOSTNAME": {"value": "sw-1", "source": "netbox"},
        "switchType": {"value": "Access", "source": "netbox"},
    }
    role = device_role_name(resolved)
    assert role == "Access"
    assert ccc_role(role) == "ACCESS"
    assert ccc_tag_name(role) == "Access"


def test_role_name_skips_empty_entries_and_falls_back_to_netbox() -> None:
    resolved = {"switchType": {"value": "", "source": "manual"}}
    assert device_role_name(resolved, "Distribution") == "Distribution"


def test_operator_entry_wins_for_the_role_without_touching_the_record() -> None:
    resolved = {"switchType": {"value": "", "source": "manual"}}
    applied = _with_overrides(resolved, {"switchType": "Core"})
    assert device_role_name(applied) == "Core"
    assert resolved["switchType"]["value"] == ""  # the stored entry is unchanged
