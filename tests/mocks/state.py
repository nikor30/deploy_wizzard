"""Shared in-memory state for the mock CCC/NetBox/ISE stack.

The shapes mirror what live CCC 2.3.7 and NetBox 4.x actually return
(bare-array 0-based PnP list, task/task-tree, paginated NetBox results) —
keep them in sync with the regression tests in tests/unit/.
"""

from dataclasses import dataclass, field
from typing import Any

CCC_SITE_ID = "uuid-ffm"
CCC_SITE_NAME = "Global/Germany/Frankfurt/DC1"
NETBOX_SITE_ID = 10
NETBOX_SITE_NAME = "FFM-DC1"

DAY0_TEMPLATE_ID = "tpl-day0"
DAYN_TEMPLATE_ID = "tpl-dayn"
DAYN_VARIABLES = ["SNMP_LOCATION", "NTP_SERVER", "CONTACT"]


@dataclass
class MockState:
    # CCC
    pnp_devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    # serial -> polls remaining before the device reports Provisioned
    provision_polls: dict[str, int] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_counter: int = 0
    token_counter: int = 0
    # NetBox
    netbox_devices: dict[int, dict[str, Any]] = field(default_factory=dict)
    # ISE
    deliveries: list[dict[str, Any]] = field(default_factory=list)
    # failure injection knobs (set via POST /__mock__/config)
    auth_fail: bool = False
    fail_next_ccc_gets: int = 0
    fail_onboarding_serials: list[str] = field(default_factory=list)
    dayn_task_fail: bool = False
    netbox_patch_fail: bool = False
    ise_fail: bool = False
    claim_polls: int = 1
    task_polls: int = 1
    # stats
    ccc_in_flight: int = 0
    ccc_max_in_flight: int = 0
    ccc_requests: int = 0


def seed(state: MockState, devices: int = 2) -> None:
    """(Re)seed paired CCC-PnP + NetBox devices; serials SN000001..N."""
    state.pnp_devices = {}
    state.netbox_devices = {}
    state.provision_polls = {}
    state.claims = []
    state.tasks = {}
    state.deliveries = []
    for i in range(1, devices + 1):
        serial = f"SN{i:06d}"
        state.pnp_devices[f"pnp-{i}"] = {
            "id": f"pnp-{i}",
            "deviceInfo": {
                "serialNumber": serial,
                "pid": "C9300-48P",
                "state": "Unclaimed",
                "onbState": "Not Contacted",
                "lastContact": 1752675000000,
                "httpHeaders": [{"key": "clientAddress", "value": f"172.20.99.{i}"}],
            },
        }
        state.netbox_devices[1000 + i] = {
            "id": 1000 + i,
            "name": f"sw-ffm-{i:02d}",
            "serial": serial,
            "status": {"value": "planned"},
            "site": {"id": NETBOX_SITE_ID, "name": NETBOX_SITE_NAME},
            "primary_ip4": {"address": f"172.20.10.{i}/24"},
            "location": {"id": 101, "name": "Floor 1"},
            "rack": {"id": 5, "name": "R01"},
            "role": {"id": 3, "name": "access"},
            "asset_tag": f"FOC2126{i:04d}",
            "tenant": {"id": 2, "name": "IT Operations"},
            "custom_fields": {"snmp_location": "FFM DC1 / Rack 4"},
            "config_context": {"ntp_server": "10.0.0.1"},
        }


STATE = MockState()
seed(STATE)
