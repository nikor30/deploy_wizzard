"""Serial-based matching of CCC PnP devices against NetBox planned devices.

Pitfalls honored (CLAUDE.md §11): serials are normalized with strip().upper()
on both sides and both raw values are logged when a match fails; planned
devices frequently lack primary_ip4, so the mgmt-interface fallback is
mandatory; unmapped sites block a device without hiding its NetBox data.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from app.clients.netbox import NetBoxClient

logger = logging.getLogger(__name__)

# (netbox_site_id, netbox_location_id | None) -> (ccc_site_id, ccc_site_name)
# location_id None = mapping for the whole site.
SiteMappingLookup = dict[tuple[int, int | None], tuple[str, str]]

# netbox location_id -> parent location_id (None = directly below the site)
LocationParents = dict[int, int | None]


def resolve_ccc_site(
    site_id: int,
    location_id: int | None,
    mappings: SiteMappingLookup,
    location_parents: LocationParents,
) -> tuple[str, str] | None:
    """Most specific mapped level wins: the device's location, then its parent
    locations (floor -> building -> …), then the site itself."""
    current = location_id
    seen: set[int] = set()
    while current is not None and current not in seen:
        seen.add(current)
        mapping = mappings.get((site_id, current))
        if mapping:
            return mapping
        current = location_parents.get(current)
    return mappings.get((site_id, None))


# Interface-name prefixes that qualify as management interfaces for the
# mgmt-IP fallback lookup (case-insensitive; configurable in a later phase).
MGMT_INTERFACE_PREFIXES = ("mgmt", "vlan")

MATCHED = "matched"
UNMATCHED = "unmatched"
UNMAPPED_SITE = "unmapped_site"


def normalize_serial(serial: str) -> str:
    return serial.strip().upper()


@dataclass
class MatchResult:
    serial: str
    match_status: str
    netbox_device_id: int | None = None
    netbox_name: str | None = None
    netbox_site_id: int | None = None
    netbox_site_name: str | None = None
    ccc_site_id: str | None = None
    ccc_site_name: str | None = None
    mgmt_ip: str | None = None
    vlan_options: list[dict[str, Any]] = field(default_factory=list)


async def _resolve_mgmt_ip(client: NetBoxClient, device: dict[str, Any]) -> str | None:
    primary = device.get("primary_ip4")
    if primary and primary.get("address"):
        return str(primary["address"])
    addresses = await client.get_ip_addresses(int(device["id"]))
    for entry in addresses:
        interface = (entry.get("assigned_object") or {}).get("name", "")
        if interface.lower().startswith(MGMT_INTERFACE_PREFIXES):
            address = entry.get("address")
            if address:
                return str(address)
    logger.warning(
        "No primary_ip4 and no mgmt-interface IP found in NetBox",
        extra={"netbox_device_id": device["id"], "serial": device.get("serial")},
    )
    return None


async def match_serials(
    serials: list[str],
    netbox: NetBoxClient,
    site_mappings: SiteMappingLookup,
    location_parents: LocationParents | None = None,
) -> list[MatchResult]:
    """Match the given CCC serials against NetBox devices in status `planned`."""
    planned = await netbox.get_devices(status="planned")
    by_serial: dict[str, dict[str, Any]] = {}
    for candidate in planned:
        raw = candidate.get("serial") or ""
        if raw.strip():
            by_serial[normalize_serial(raw)] = candidate

    vlan_cache: dict[int, list[dict[str, Any]]] = {}
    results: list[MatchResult] = []
    for serial in serials:
        device = by_serial.get(normalize_serial(serial))
        if device is None:
            logger.warning(
                "No planned NetBox device for serial",
                extra={
                    "ccc_serial_raw": serial,
                    "netbox_serials_raw": [d.get("serial") for d in planned][:50],
                },
            )
            results.append(MatchResult(serial=serial, match_status=UNMATCHED))
            continue

        site = device.get("site") or {}
        site_id = site.get("id")
        mgmt_ip = await _resolve_mgmt_ip(netbox, device)

        vlan_options: list[dict[str, Any]] = []
        if site_id is not None:
            if site_id not in vlan_cache:
                vlan_cache[site_id] = [
                    {"id": v["id"], "vid": v["vid"], "name": v.get("name")}
                    for v in await netbox.get_vlans(int(site_id))
                ]
            vlan_options = vlan_cache[site_id]

        mapping = None
        if site_id is not None:
            location = device.get("location") or {}
            mapping = resolve_ccc_site(
                int(site_id), location.get("id"), site_mappings, location_parents or {}
            )
        results.append(
            MatchResult(
                serial=serial,
                match_status=MATCHED if mapping else UNMAPPED_SITE,
                netbox_device_id=device["id"],
                netbox_name=device.get("name"),
                netbox_site_id=site_id,
                netbox_site_name=site.get("name"),
                ccc_site_id=mapping[0] if mapping else None,
                ccc_site_name=mapping[1] if mapping else None,
                mgmt_ip=mgmt_ip,
                vlan_options=vlan_options,
            )
        )
    return results
