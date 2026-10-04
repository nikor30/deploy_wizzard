"""Code review, package 4: variable heuristic and duplicate NetBox serials.

- the "leaked password" heuristic must not swallow real CamelCase variables
  (they vanished from the wizard and were never sent - no gateway, no key)
- two planned NetBox devices with the same serial must not be matched silently
  (the last one won: wrong hostname/IP/site, and later the wrong device set active)
"""

import app.clients.base as base
import pytest
import respx
from app.clients.netbox import NetBoxClient
from app.services.day0 import resolve_day0_variables
from app.services.dayn import looks_like_junk_var, resolve_variables
from app.services.matching import match_serials
from tests.unit.test_day0_variables import _device
from tests.unit.test_matching import BASE, MAPPING, mock_planned, mock_vlans, nb_device

REAL_CAMELCASE = [
    "DefaultGatewayIp",
    "RadiusServerKey1",
    "EnableSecretPassword",
    "MgmtInterfaceName",
    "IPv4AddressMgmtVLANId",
    "UplinkPortChannelNo2",
    "TacacsServerAddress",
    "SnmpCommunityString",
]
LEAKED_TOKENS = ["pPYzdaRZdKO5gppL7ddKhk3iF", "OaMGKyQBNwDjxFcagpT", "aB3dE5fG7hI9jK1lMn"]


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "BACKOFF_BASE_SECONDS", 0)


@pytest.mark.parametrize("name", REAL_CAMELCASE)
def test_real_camelcase_variables_are_not_junk(name: str) -> None:
    assert not looks_like_junk_var(name)


@pytest.mark.parametrize("name", LEAKED_TOKENS)
def test_leaked_password_tokens_are_still_hidden(name: str) -> None:
    assert looks_like_junk_var(name)


def test_real_camelcase_variables_reach_day0_and_dayn() -> None:
    day0 = resolve_day0_variables(REAL_CAMELCASE, _device(), {"device": {}}, {})
    dayn = resolve_variables(REAL_CAMELCASE, {}, {"device": {}})
    assert set(day0) == set(REAL_CAMELCASE)
    assert set(dayn) == set(REAL_CAMELCASE)


@respx.mock
async def test_duplicate_planned_serial_is_ambiguous_not_matched() -> None:
    # "fcw1 " and "FCW1" normalise to the same serial
    mock_planned([nb_device(1, "FCW1"), nb_device(2, "fcw1 "), nb_device(3, "FCW3")])
    mock_vlans([{"id": 5, "vid": 110, "name": "MGMT"}])
    async with NetBoxClient(BASE, "tok") as client:
        results = await match_serials(["FCW1", "FCW3"], client, MAPPING)
    by_serial = {r.serial: r for r in results}
    ambiguous = by_serial["FCW1"]
    assert ambiguous.match_status == "ambiguous"
    assert ambiguous.netbox_device_id is None  # never pick one of them
    assert ambiguous.ccc_site_id is None
    assert by_serial["FCW3"].match_status == "matched"
    assert by_serial["FCW3"].netbox_device_id == 3
