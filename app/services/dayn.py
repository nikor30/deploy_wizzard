"""Day-N provisioning: variable resolution, template deploy, NetBox activation.

Rules honored (CLAUDE.md §11): a NetBox device is set `active` only when the
Day-N task is verifiably successful; site-claim style task errors are often
buried in the task tree, so child tasks are drilled when `failureReason` is
empty; batches stay per-device isolated.
"""

import asyncio
import ipaddress
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from app.clients.catalyst import CatalystCenterClient
from app.clients.netbox import NetBoxClient
from app.db.models import Job, JobDevice, ServiceSettings
from app.db.session import open_session
from app.errors import PnPBridgeError, TaskTimeout
from app.services import settings_store

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0
TASK_TIMEOUT_SECONDS = 30 * 60

# Terminal states of a template deployment (deploy/v2 status endpoint).
DEPLOYMENT_SUCCEEDED = frozenset({"SUCCESS", "COMPLETED"})
DEPLOYMENT_FAILED = frozenset({"FAILURE", "FAILED", "ERROR"})

MANUAL = "manual"
MAPPED = "mapped"
NETBOX = "netbox"
SECRET = "secret"
SECRET_PREFIX = "secret."
SECRET_MASK = "****"

# NetBox interface types that are not switch ports and can never be access
# ports: virtual SVIs, LAG bundles and bridges.
NON_PHYSICAL_INTERFACE_TYPES: frozenset[str] = frozenset({"virtual", "lag", "bridge"})

# NetBox interface 802.1Q modes that mean "trunk". Pushing `switchport mode
# access` at one of these is what broke a Day-N run part-way through: the switch
# answers with a confirmation prompt (or a rejection) instead of the config
# prompt Catalyst Center waits for, and CCC aborts the whole push as invalid
# CLI — leaving the ports it had already done configured and the rest not.
TRUNK_INTERFACE_MODES: frozenset[str] = frozenset({"tagged", "tagged-all"})


def _is_access_port(iface: dict[str, Any], uplink_names: set[str]) -> bool:
    """True for a port the ISE access-port config may safely be pushed to.

    Excluded, in order of how badly each one bites:
    * port-channel members (`lag` set) — IOS rejects `switchport mode access`
      until the port leaves the channel;
    * trunks (`mode` tagged/tagged-all) — same problem, and they are uplinks or
      inter-switch links by definition;
    * anything cabled to another device (already collected as an uplink);
    * management-only ports and non-physical types (SVI/LAG/bridge).
    """
    name = iface.get("name")
    if not name or iface.get("mgmt_only") or iface.get("lag"):
        return False
    if str(name) in uplink_names:
        return False
    if (iface.get("type") or {}).get("value") in NON_PHYSICAL_INTERFACE_TYPES:
        return False
    mode = iface.get("mode")
    mode_value = mode.get("value") if isinstance(mode, dict) else mode
    return mode_value not in TRUNK_INTERFACE_MODES


# Uplink port-channel description convention: UPL:<far-end switch name>.
UPLINK_DESCRIPTION_PREFIX = "UPL:"
# Access switches always use port-channel 1 for their uplink.
PO_ID_ACCESS = "1"

# Built-in Day-N variable → NetBox dot-path map, ported from the netbox_cc_dayn
# project's mappings.yaml (the field choices are proven against the live
# templates). Without these every Day-N variable had to be mapped by hand in
# Settings before it would fill. An explicit mapping still wins over an alias,
# so operators can override any of these per deployment.
DAYN_ALIASES: dict[str, str] = {
    # identity / placement
    "HOSTNAME": "device.name",
    "DEVICENAME": "device.name",
    "DEVICEIP": "device.mgmt.ip",
    "MGMTIP": "device.mgmt.ip",
    "MANAGEMENTIP": "device.mgmt.ip",
    "DEVICEMANAGEMENTIP": "device.mgmt.ip",
    "MGMTMASK": "device.mgmt.netmask",
    "MGMTPREFIX": "device.mgmt.prefix_length",
    "MGMTSUBNET": "device.mgmt.cidr",
    "SERIAL": "device.serial",
    "SERIALNUMBER": "device.serial",
    "ASSETID": "device.asset_tag",
    "ASSETTAG": "device.asset_tag",
    "SITEFULLNAME": "device.site.name",
    "SITE": "device.site.name",
    "SITENAME": "device.site.name",
    "BUILDINGROOM": "device.location.name",
    "LOCATION": "device.location.name",
    "BASICLOCATIONINFORMATION": "device.location.name",
    "RACKID": "device.rack.name",
    "RACK": "device.rack.name",
    "RACKNAME": "device.rack.name",
    "RACKPOSITION": "device.position",
    "POSITION": "device.position",
    "DEVICEROLE": "device.role.name",
    "ROLE": "device.role.name",
    "SWITCHTYPE": "device.role.name",
    "PLATFORM": "device.platform.name",
    "DEVICETYPE": "device.device_type.model",
    "TENANT": "device.tenant.name",
    # derived values built in build_device_context()
    "SUPPORTCONTACT": "device.support_contact",
    "UPLINKSWITCH": "device.uplink_switch",
    "UPLINKPORTS": "device.uplink_ports",
    "ACCESSPORTS": "device.access_ports",
    "ACCESSPORTLIST": "device.access_ports",
    "CLIENTPORTS": "device.access_ports",
    "ACCESSPORTCOUNT": "device.access_port_count",
    "ARRVLANS": "device.site_vlans",
    "SITEVLANS": "device.site_vlans",
    # uplink / VLAN conventions (see build_device_context)
    "POID": "device.po_id",
    "PORTCHANNELID": "device.po_id",
    "PORTCHANNEL": "device.po_id",
    "UPLINKDESCRIPTION": "device.uplink_description",
    "UPLINKDESC": "device.uplink_description",
    "UPLINKCONFIGURATIONINFORMATION": "device.uplink_description",
    "ACCESSVLAN": "device.access_vlan",
    "ACCESSVLANID": "device.access_vlan",
    "CRITICALVLAN": "device.critical_vlan",
    "CRITICALVLANID": "device.critical_vlan",
    # the uplink/port trunk carries the access VLAN untagged
    "NATIVEVLAN": "device.access_vlan",
    "NATIVEVLANID": "device.access_vlan",
}

# Values the tool can only *suggest* (several NetBox candidates matched). They
# render as editable manual fields prefilled with the suggestion — the operator
# confirms — rather than as a read-only value that might be the wrong VLAN.
DAYN_SUGGESTIONS: dict[str, str] = {
    "ACCESSVLAN": "device.access_vlan_suggested",
    "ACCESSVLANID": "device.access_vlan_suggested",
    "CRITICALVLAN": "device.critical_vlan_suggested",
    "CRITICALVLANID": "device.critical_vlan_suggested",
    "NATIVEVLAN": "device.access_vlan_suggested",
    "NATIVEVLANID": "device.access_vlan_suggested",
}


# Variables the operator may leave blank. Private-VLAN config only applies to
# switches that actually use PVLANs, so requiring it would block every ordinary
# deploy. A blank optional variable is simply omitted from the deploy payload —
# the template's own default applies.
OPTIONAL_VARS: frozenset[str] = frozenset(
    {
        "PVLAN",
        "PRIVATEVLAN",
        "PRIMARYVLAN",
        "SECONDARYVLAN",
        "PVLANPRIMARY",
        "PVLANSECONDARY",
    }
)


def normalize_var(name: str) -> str:
    return "".join(c for c in name.upper() if c.isalnum())


def is_optional_var(name: str) -> bool:
    """True for variables the wizard must not require (private-VLAN config)."""
    return normalize_var(name) in OPTIONAL_VARS


def is_internal_var(name: str) -> bool:
    """CCC binding variables (`__device`, `__interface`) are filled by Catalyst
    Center itself at deploy time — they are not operator input, so asking for
    them would block the wizard on values nobody can supply."""
    return name.startswith("__")


def looks_like_junk_var(name: str) -> bool:
    """Detect the garbled variable names Catalyst Center generates from password
    values (e.g. ``pPYzdaRZdKO5gppL7ddKhk3iF``, ``OaMGKyQBNwDjxFcagpT``). These
    are noise leaked into the template's parameter list and must never be shown
    to the operator or sent in a claim/deploy. Operates on the ORIGINAL
    mixed-case name (the case pattern is the tell), never the normalized form.

    A name is junk when it is a single opaque token: no separators, long, mixed
    upper/lower case, and either contains digits or flips case many times — the
    fingerprint of a random secret, not a human-authored variable name."""
    if any(sep in name for sep in "_-. /:"):
        return False
    if len(name) < 16:
        return False
    has_upper = any(c.isupper() for c in name)
    has_lower = any(c.islower() for c in name)
    if not (has_upper and has_lower):
        return False
    has_digit = any(c.isdigit() for c in name)
    letters = [c for c in name if c.isalpha()]
    case_transitions = sum(1 for a, b in pairwise(letters) if a.isupper() != b.isupper())
    return has_digit or case_transitions >= 5


def hidden_variable(name: str) -> bool:
    """Variables the operator must never be asked for: CCC-internal bindings
    and Catalyst Center's garbled password-derived names."""
    return is_internal_var(name) or looks_like_junk_var(name)


def resolve_path(context: dict[str, Any], path: str) -> str | None:
    """Walk a dot-path (e.g. device.custom_fields.snmp_location) through dicts
    and lists (numeric segments index lists). Returns None when unresolvable."""
    if not path:
        return None
    current: Any = context
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            return None
        if current is None:
            return None
    if isinstance(current, dict | list):
        return None
    return str(current)


def _is_access_role(device: dict[str, Any]) -> bool:
    role = (device.get("role") or device.get("device_role") or {}).get("name") or ""
    return "access" in str(role).lower()


def _vlans_by_name(site_vlans: list[dict[str, Any]] | None, keyword: str) -> list[str]:
    """VIDs of the site VLANs matching `keyword`, lowest VID first.

    A VLAN named exactly `keyword` wins outright: a real site has both an
    `access` VLAN and unrelated names that merely contain the word (e.g.
    `Time_Access`), and only the exact one is the access VLAN. Substring
    matches are used solely when no VLAN carries the bare name.
    """
    exact: list[int] = []
    partial: list[int] = []
    for vlan in site_vlans or []:
        if vlan.get("vid") is None:
            continue
        name = str(vlan.get("name") or "").lower()
        if name == keyword:
            exact.append(int(vlan["vid"]))
        elif keyword in name:
            partial.append(int(vlan["vid"]))
    return [str(vid) for vid in sorted(exact or partial)]


def _vlan_by_name(site_vlans: list[dict[str, Any]] | None, keyword: str) -> str | None:
    """VID of the site VLAN whose name contains `keyword` — only when it is the
    single match, so a confident value is never a guess."""
    hits = _vlans_by_name(site_vlans, keyword)
    return hits[0] if len(hits) == 1 else None


def _vlan_suggestion(site_vlans: list[dict[str, Any]] | None, keyword: str) -> str | None:
    """Best candidate when several site VLANs match `keyword` (lowest VID).

    Offered as a *suggestion* in an editable field rather than a read-only
    value: picking the wrong VLAN would misconfigure the port, so the operator
    confirms it — the same treatment Day-0 gives its gateway guess.
    """
    hits = _vlans_by_name(site_vlans, keyword)
    return hits[0] if len(hits) > 1 else None


def _manual(variable: str) -> dict[str, Any]:
    """An open field; optional ones never block the deploy button."""
    info: dict[str, Any] = {"value": None, "source": MANUAL}
    if is_optional_var(variable):
        info["optional"] = True
    return info


def resolve_variables(
    variables: list[str],
    mappings: dict[str, str],
    context: dict[str, Any],
    secret_names: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Resolve each template variable via its mapping; anything that cannot be
    resolved is flagged for manual entry in the wizard.

    Resolution order per variable: hidden names (CCC bindings / garbled
    password leaks) are dropped → explicit mapping (operator config always
    wins) → built-in NetBox alias (`DAYN_ALIASES`) → global variable / secret
    matched by name → open for manual entry.

    `secret.<NAME>` paths resolve to a masked placeholder — the plaintext is
    decrypted just-in-time when building the deploy payload, never stored on
    the job or returned by the API. A secret whose name matches a template
    variable also auto-fills it (a "global variable" set once, used everywhere)
    without an explicit mapping."""
    known_secrets = set(secret_names)
    by_norm = {normalize_var(n): n for n in known_secrets}
    result: dict[str, dict[str, Any]] = {}
    for variable in variables:
        if hidden_variable(variable):
            continue
        path = mappings.get(variable)
        if path and path.startswith(SECRET_PREFIX):
            name = path.removeprefix(SECRET_PREFIX)
            if name in known_secrets:
                result[variable] = {"value": SECRET_MASK, "source": SECRET, "secret": name}
            else:
                result[variable] = _manual(variable)
            continue
        value = resolve_path(context, path) if path else None
        if value is not None:
            result[variable] = {"value": value, "source": MAPPED}
            continue
        norm = normalize_var(variable)
        # built-in NetBox alias — only when the operator has no mapping for it
        if not path:
            alias = DAYN_ALIASES.get(norm)
            value = resolve_path(context, alias) if alias else None
            if value is not None:
                result[variable] = {"value": value, "source": NETBOX}
                continue
            # ambiguous match -> prefilled but editable, operator confirms
            suggestion = DAYN_SUGGESTIONS.get(norm)
            value = resolve_path(context, suggestion) if suggestion else None
            if value is not None:
                result[variable] = {"value": value, "source": MANUAL}
                continue
        if norm in by_norm:
            result[variable] = {"value": SECRET_MASK, "source": SECRET, "secret": by_norm[norm]}
        else:
            result[variable] = _manual(variable)
    return result


def build_device_context(
    device: dict[str, Any],
    interfaces: list[dict[str, Any]] | None = None,
    site_vlans: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Variable-resolution context: the NetBox device enriched with uplink/
    port details, computed management-network facts, and the flat Catalyst
    Center Day-N values (uplink_ports, uplink_switch, site_vlans,
    support_contact) so dot-paths like `device.uplink_ports` or
    `device.mgmt.netmask` resolve.

    The derivations mirror the netbox_cc_dayn mappings.yaml resolvers:
    uplinks are cabled, non-management interfaces; peer data comes from
    NetBox `connected_endpoints`; site_vlans is `(vid,name);(vid,name);…`;
    support_contact is the first site/device contact, falling back to tenant.
    Verify against live fixtures via the mapping page's serial preview.
    """
    ctx = dict(device)
    ctx["interfaces"] = interfaces or []
    uplinks: list[dict[str, Any]] = []
    for iface in interfaces or []:
        if iface.get("mgmt_only"):
            continue
        endpoints = iface.get("connected_endpoints") or []
        if not endpoints and not iface.get("cable"):
            continue
        peer = endpoints[0] if endpoints else {}
        uplinks.append(
            {
                "name": iface.get("name"),
                "type": (iface.get("type") or {}).get("value"),
                "description": iface.get("description"),
                "peer_device": (peer.get("device") or {}).get("name"),
                "peer_interface": peer.get("name"),
            }
        )
    ctx["uplinks"] = uplinks

    # Access ports: the physical, non-management, non-uplink interfaces. The
    # port template used to guess these from the "x/0/y" name pattern, which
    # catches an uplink that happens to sit on a front-panel port and misses a
    # front-panel port on a module. NetBox knows which ports are cabled to
    # another device, so drive it from there instead.
    uplink_names = {str(u["name"]) for u in uplinks if u.get("name")}
    access_ports = [
        str(iface["name"]) for iface in interfaces or [] if _is_access_port(iface, uplink_names)
    ]
    ctx["access_ports"] = ",".join(access_ports)
    ctx["access_port_count"] = str(len(access_ports))

    # Flat Catalyst-Center Day-N values (match netbox_cc_dayn resolvers).
    port_names = [str(u["name"]) for u in uplinks if u.get("name")]
    ctx["uplink_ports"] = ",".join(port_names)
    peers = {str(u["peer_device"]) for u in uplinks if u.get("peer_device")}
    # unique far-end device only; ambiguous (multiple) or none -> stays unset
    ctx["uplink_switch"] = peers.pop() if len(peers) == 1 else None
    ctx["site_vlans"] = ";".join(
        f"({v.get('vid')},{v.get('name', '')})" for v in (site_vlans or []) if v.get("vid")
    )
    # Uplink port-channel description: always "UPL:<far-end switch>".
    ctx["uplink_description"] = (
        f"{UPLINK_DESCRIPTION_PREFIX}{ctx['uplink_switch']}" if ctx["uplink_switch"] else None
    )
    # Port-channel id: access switches always use 1 (their single uplink PO);
    # any other role is a design decision and stays manual.
    ctx["po_id"] = PO_ID_ACCESS if _is_access_role(device) else None
    # VLANs picked out of the site's VLAN list by name.
    ctx["access_vlan"] = _vlan_by_name(site_vlans, "access")
    ctx["critical_vlan"] = _vlan_by_name(site_vlans, "critical")
    # several VLANs match the keyword -> offer the lowest VID as an editable
    # suggestion instead of silently committing to one of them
    ctx["access_vlan_suggested"] = _vlan_suggestion(site_vlans, "access")
    ctx["critical_vlan_suggested"] = _vlan_suggestion(site_vlans, "critical")
    ctx["support_contact"] = _resolve_contact(device, contacts)

    address = (device.get("primary_ip4") or {}).get("address")
    if address:
        try:
            interface = ipaddress.ip_interface(str(address))
        except ValueError:
            pass
        else:
            ctx["mgmt"] = {
                "address": str(address),
                "ip": str(interface.ip),
                "netmask": str(interface.network.netmask),
                "prefix_length": interface.network.prefixlen,
                "network": str(interface.network.network_address),
                "cidr": str(interface.network),
            }
    return {"device": ctx}


SUPPORT_CONTACT_ROLE = "Local IT"


async def _safe(coro: Any, what: str) -> list[dict[str, Any]]:
    """Run an optional NetBox enrichment call; a NetBox error degrades to an
    empty list so one unsupported endpoint never breaks variable resolution."""
    try:
        return list(await coro)
    except PnPBridgeError as exc:
        logger.warning("Skipping %s enrichment: %s", what, exc)
        return []


# Where the access-port list comes from. "netbox" resolves ACCESSPORTS from the
# NetBox interface data (physical, non-management, not cabled to another
# device). "device" leaves it empty so the template falls back to its own
# `$__interface` loop — the escape hatch for when Catalyst Center gains a native
# way to apply port config, or when NetBox cabling is not maintained.
ACCESS_PORT_SOURCES: tuple[str, ...] = ("netbox", "device")


async def load_device_context(
    netbox: NetBoxClient, device: dict[str, Any], access_port_source: str = "netbox"
) -> dict[str, Any]:
    """Fetch the extra NetBox data a device's Day-N variables need — interfaces
    (uplinks), the site's VLANs, and support contacts — and build the full
    resolution context. Every piece degrades to empty on error, never raises,
    so preview/suggest keep working even if an endpoint is unavailable."""
    device_id = device.get("id")
    interfaces: list[dict[str, Any]] = []
    site_vlans: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    if device_id is not None:
        interfaces = await _safe(netbox.get_interfaces(int(device_id)), "interfaces")
    site_id = (device.get("site") or {}).get("id")
    if site_id is not None:
        site_vlans = await _safe(netbox.get_vlans(int(site_id)), "site VLANs")
        site_contacts = await _safe(
            netbox.get_contact_assignments("dcim.site", int(site_id)), "site contacts"
        )
        # site contact with role "Local IT" (see netbox_cc_dayn mappings.yaml)
        contacts = [c for c in site_contacts if _contact_role(c) == SUPPORT_CONTACT_ROLE]
    if not contacts and device_id is not None:
        # fall back to a device-level contact (any role)
        contacts = await _safe(
            netbox.get_contact_assignments("dcim.device", int(device_id)), "device contacts"
        )
    context = build_device_context(device, interfaces, site_vlans, contacts)
    if access_port_source != "netbox":
        # blank, not absent: the template's `#if($ACCESS_PORTS != "")` picks its
        # own loop, and the wizard shows the variable as unresolved rather than
        # silently pushing a NetBox-derived list the operator opted out of.
        context["device"]["access_ports"] = ""
        context["device"]["access_port_count"] = ""
    return context


def _contact_role(assignment: dict[str, Any]) -> str | None:
    role = assignment.get("role")
    if isinstance(role, dict):
        return role.get("name")
    return role if isinstance(role, str) else None


def _resolve_contact(device: dict[str, Any], contacts: list[dict[str, Any]] | None) -> str | None:
    """First contact name from NetBox contact assignments, else the device's
    tenant name — mirrors the netbox_cc_dayn support_contact fallback chain."""
    for assignment in contacts or []:
        name = (assignment.get("contact") or {}).get("name")
        if name:
            return str(name)
    tenant_name = (device.get("tenant") or {}).get("name")
    return str(tenant_name) if tenant_name else None


def build_deploy_payload(
    template_id: str, device: JobDevice, params: dict[str, str]
) -> dict[str, Any]:
    """deploy/v2 payload (§6.1 baseline + common CCC shape — verify fixtures).

    The device joined the CCC inventory during Day-0; it is targeted by its
    management IP."""
    if not device.mgmt_ip:
        raise PnPBridgeError(f"Device {device.serial} has no mgmt IP to target for Day-N.")
    ip = device.mgmt_ip.split("/")[0]
    return {
        "templateId": template_id,
        "forcePushTemplate": True,
        "targetInfo": [{"id": ip, "type": "MANAGED_DEVICE_IP", "params": params}],
    }


async def poll_task(
    client: CatalystCenterClient,
    task_id: str,
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    task_timeout: float = TASK_TIMEOUT_SECONDS,
    label: str = "Day-N",
    always_drill: bool = False,
) -> None:
    """Poll a CCC task until it ends; raise with the real reason on failure.

    `always_drill` also fetches the task tree when `failureReason` is already
    set. Provisioning needs that: its top-level reason is a generic
    `NCSP11001 … user intent validation failed`, and the CFS validation that
    actually objected only names itself in a child task.
    """
    deadline = asyncio.get_event_loop().time() + task_timeout
    while True:
        task = await client.get_task(task_id)
        if task.get("isError"):
            reason = str(task.get("failureReason") or "")
            if not reason or always_drill or _points_at_the_task_tree(reason):
                # §11: errors are often buried in the task tree
                children = await client.get_task_tree(task_id)
                extra = [d for d in (_task_detail(c) for c in children) if d and d != reason]
                if extra:
                    reason = f"{reason} | {' ; '.join(extra)}" if reason else " ; ".join(extra)
                else:
                    # The tree carried nothing we recognise. Dump it verbatim to
                    # the Logs page rather than leaving the operator with CCC's
                    # "submit a GET task tree request" and no way to act on it.
                    logger.warning(
                        "%s task %s failed and its task tree carried no recognised reason",
                        label,
                        task_id,
                        extra={"task_id": task_id, "task": task, "task_tree": children},
                    )
                reason = reason or "no failureReason from CCC"
            raise PnPBridgeError(f"{label} task failed: {reason}")
        if task.get("endTime"):
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise TaskTimeout(f"{label} task {task_id} did not finish within {int(task_timeout)}s.")
        await asyncio.sleep(poll_interval)


# CCC's way of saying "the reason is in the child tasks, go look".
TASK_TREE_HINTS = ("task tree", "child operations", "not all child")


def _points_at_the_task_tree(reason: str) -> bool:
    lowered = reason.lower()
    return any(hint in lowered for hint in TASK_TREE_HINTS)


def _task_detail(task: dict[str, Any]) -> str:
    """The most specific error text a (child) task carries.

    `failureReason` is only one of the places CCC puts it: a batch child often
    leaves that empty and describes itself in `progress`, or carries a code in
    `errorCode` with the text in `data`. Taking only `failureReason` is why a
    "Batch Operation failed. Not all child operations succeeded." told the
    operator nothing.
    """
    if not task.get("isError") and not task.get("failureReason"):
        return ""
    parts: list[str] = []
    for key in ("failureReason", "progress", "errorCode", "data"):
        value = task.get(key)
        if not value:
            continue
        text = str(value).strip()
        # `progress` is often just a status word, or the task id echoed back
        if text and text not in parts and text != str(task.get("id")):
            parts.append(text)
    return " / ".join(parts)


def _set_device(device_id: int, state: str, error: str | None = None) -> None:
    with open_session() as db:
        device = db.get(JobDevice, device_id)
        if device is not None:
            device.state = state
            device.error = error
            if state == "dayn_deploying":
                device.dayn_started_at = datetime.now(tz=UTC)
            if state in ("dayn_failed", "activate_failed", "completed"):
                device.dayn_finished_at = datetime.now(tz=UTC)


UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _deploy_handle(response: dict[str, Any]) -> tuple[str, str]:
    """How to track a deploy: `("task", id)` or `("deployment", id)`.

    deploy/v2 answers with a `deploymentId` that is a *sentence*, not an id:
    ``Deployment of Template: <template-uuid>.ApplicableTargets: [10.0.0.1]
    Template Deploymemnt Id: <deployment-uuid>`` (CCC's own typo). Polling that
    string as a task id gives HTTP 400 ("… is not a valid UUID"), so the real
    deployment UUID — the **last** one in the sentence — is extracted and
    tracked via the deployment-status endpoint instead. A plain `taskId` is
    still polled as a task.
    """
    inner = response.get("response")
    inner = inner if isinstance(inner, dict) else {}
    for source in (inner, response):
        task_id = source.get("taskId")
        if task_id:
            return "task", str(task_id)
    for source in (inner, response):
        for key in ("deploymentId", "deploymentJobId"):
            raw = source.get(key)
            if not raw:
                continue
            uuids = UUID_RE.findall(str(raw))
            if uuids:
                return "deployment", uuids[-1]
            return "deployment", str(raw)
    return "", ""


INTERACTIVE_HINT = (
    " — the switch asked an interactive question and Catalyst Center could not answer it, so it "
    "rejected the whole push as 'invalid CLI'. This is template-side: answer the prompt ON the "
    "command that raises it, wrapped in an #INTERACTIVE block. For the AAA legacy→C3PL "
    "conversion that command is the FIRST control class, e.g.\n"
    "#INTERACTIVE\n"
    "class-map type control subscriber match-all AAA_SVR_DOWN_AUTHD_HOST<IQ>continue<R>yes\n"
    "#ENDS_INTERACTIVE\n"
    "Do NOT try to convert with a separate command first: `authentication display` is "
    "privileged-EXEC only and `authentication convert-to` is rejected in EXEC mode — both fail "
    "with '% Invalid input'. See the Catalyst Center user guide, 'Create Templates to Automate "
    "Device Configuration Changes'."
)


def interactive_prompt_hint(reason: str) -> str:
    """Actionable hint when CCC rejected a push because the device prompted.

    CCC only auto-answers the prompts it knows ([y/n], [confirm], ACCEPT?); a
    plain `Do you wish to continue? [yes]:` is not one of them, and the failure
    text alone gives the operator nothing to act on.
    """
    lowered = reason.lower()
    if "invalid cli" not in lowered:
        return ""
    if "(interactive)" in lowered or "do you wish to continue" in lowered:
        return INTERACTIVE_HINT
    return ""


async def poll_deployment(
    client: CatalystCenterClient,
    deployment_id: str,
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    task_timeout: float = TASK_TIMEOUT_SECONDS,
) -> None:
    """Poll a template deployment until it ends; raise with the device-level
    reason on failure (that is where CCC puts the CLI error)."""
    deadline = asyncio.get_event_loop().time() + task_timeout
    while True:
        status_body = await client.get_deployment_status(deployment_id)
        status = str(status_body.get("status") or "").upper()
        if status in DEPLOYMENT_FAILED:
            reasons = [
                str(device.get("detailedStatusMessage") or "").strip()
                for device in status_body.get("devices") or []
                if str(device.get("status") or "").upper() in DEPLOYMENT_FAILED
            ]
            reason = next((r for r in reasons if r), "") or str(
                status_body.get("statusMessage") or ""
            )
            raise PnPBridgeError(
                f"Catalyst Center template deployment failed: {reason or 'no reason given'}"
                f"{interactive_prompt_hint(reason)}"
            )
        if status in DEPLOYMENT_SUCCEEDED:
            # The overall status is about the *deployment request*, not about
            # what reached the switch: CCC happily reports SUCCESS while the
            # only target sits at NOT_APPLICABLE/SKIPPED and never received a
            # line of config. Marking NetBox active on that is exactly the
            # half-updated source of truth §11 warns about, so every target
            # must have succeeded too.
            not_ok = [
                device
                for device in status_body.get("devices") or []
                if str(device.get("status") or "").upper() not in DEPLOYMENT_SUCCEEDED
            ]
            if not_ok:
                details = "; ".join(
                    f"{str(device.get('status') or 'UNKNOWN').upper()}: "
                    f"{str(device.get('detailedStatusMessage') or '').strip() or 'no detail'}"
                    for device in not_ok
                )
                raise PnPBridgeError(
                    f"Catalyst Center reported the deployment as {status}, but the target device "
                    f"did not apply the template ({details}). Nothing was pushed to the switch, "
                    "so NetBox was left untouched."
                )
            if not status_body.get("devices"):
                logger.warning(
                    "Deployment %s reported %s but listed no target devices — "
                    "Catalyst Center may not have pushed anything.",
                    deployment_id,
                    status,
                )
            return
        if asyncio.get_event_loop().time() > deadline:
            raise TaskTimeout(
                f"Template deployment {deployment_id} did not finish within "
                f"{task_timeout / 60:.0f} minutes (last status: {status or 'unknown'})."
            )
        await asyncio.sleep(poll_interval)


async def _deploy_one(
    client: CatalystCenterClient,
    netbox_settings: tuple[str, str, bool] | None,
    job_id: int,
    device_id: int,
    template_id: str,
    params: dict[str, str],
    poll_interval: float,
    task_timeout: float,
    activate: bool = True,
) -> None:
    with open_session() as db:
        device = db.get(JobDevice, device_id)
        if device is None:
            return
        serial = device.serial
        netbox_device_id = device.netbox_device_id
        try:
            # validates the device is deployable (mgmt IP) before any API call
            build_deploy_payload(template_id, device, params)
        except PnPBridgeError as exc:
            device.state = "dayn_failed"
            device.error = exc.message
            return

    _set_device(device_id, "dayn_deploying")
    failures: list[str] = []
    try:
        # A composite template must be deployed member by member, in order —
        # deploying the container itself pushes its member JSON to the device.
        targets = await client.get_deployable_templates(template_id)
    except PnPBridgeError as exc:
        logger.error("Day-N failed for device", extra={"job_id": job_id, "serial": serial})
        _set_device(device_id, "dayn_failed", error=exc.message)
        return
    except Exception as exc:  # per-device isolation
        logger.exception("Unexpected Day-N error", extra={"job_id": job_id})
        _set_device(device_id, "dayn_failed", error=str(exc))
        return

    for member_id, member_variables in targets:
        # Members are independent config blocks, so one failing must not cancel
        # the rest: a broken port template used to take the VLAN and banner
        # members down with it and leave the switch with neither. Every member
        # is attempted; the failures are reported together at the end.
        try:
            member_params = (
                {k: v for k, v in params.items() if k in member_variables}
                if member_variables
                else params
            )
            with open_session() as db:
                device = db.get(JobDevice, device_id)
                if device is None:
                    return
                payload = build_deploy_payload(member_id, device, member_params)
            response = await client.deploy_template(payload)
            kind, handle = _deploy_handle(response)
            if not handle:
                raise PnPBridgeError(
                    f"Catalyst Center accepted the deploy of template {member_id} but returned "
                    "neither a taskId nor a deploymentId, so it cannot be tracked. Check the "
                    "template in CCC (is it committed/versioned?) and the Logs page for the "
                    "full response."
                )
            if kind == "deployment":
                await poll_deployment(
                    client, handle, poll_interval=poll_interval, task_timeout=task_timeout
                )
            else:
                await poll_task(
                    client, handle, poll_interval=poll_interval, task_timeout=task_timeout
                )
        except PnPBridgeError as exc:
            logger.error(
                "Day-N template failed for device",
                extra={"job_id": job_id, "serial": serial, "template_id": member_id},
            )
            failures.append(f"{member_id}: {exc.message}")
        except Exception as exc:  # per-device, per-member isolation
            logger.exception("Unexpected Day-N error", extra={"job_id": job_id})
            failures.append(f"{member_id}: {exc}")

    if failures:
        prefix = (
            f"{len(failures)} of {len(targets)} templates failed; the others were applied. "
            if len(targets) > 1
            else ""
        )
        _set_device(device_id, "dayn_failed", error=prefix + " | ".join(failures))
        return

    # Day-N verifiably succeeded — only now touch the source of truth.
    if not activate:
        # a further stage will follow; leave NetBox alone until it has run
        _set_device(device_id, "dayn_complete")
        return
    if netbox_settings is None or netbox_device_id is None:
        _set_device(device_id, "activate_failed", error="NetBox not configured.")
        return
    base_url, token, tls_verify = netbox_settings
    try:
        async with NetBoxClient(base_url, token, tls_verify=tls_verify) as netbox:
            await netbox.patch_device_status(netbox_device_id, "active")
    except PnPBridgeError as exc:
        logger.error(
            "Day-N succeeded but NetBox activation failed",
            extra={"job_id": job_id, "serial": serial, "netbox_device_id": netbox_device_id},
        )
        _set_device(device_id, "activate_failed", error=exc.message)
        return
    _set_device(device_id, "completed")


async def run_dayn(
    job_id: int,
    *,
    template_id: str,
    device_params: dict[int, dict[str, str]],
    poll_interval: float = POLL_INTERVAL_SECONDS,
    task_timeout: float = TASK_TIMEOUT_SECONDS,
    stage: int = 1,
    activate: bool = True,
) -> None:
    """Deploy the Day-N template to every eligible device, isolated per device.

    `stage` 2 is the optional port/uplink pass; `activate` decides whether a
    success also patches NetBox to `active`. Stage 1 runs with `activate=False`
    when a stage 2 will follow, so the source of truth is only touched once the
    device is really finished (§11).
    """
    with open_session() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = "dayn_running"
        job.current_step = 4 if stage == 1 else 5
        if stage == 1:
            job.dayn_template_id = template_id
        else:
            job.dayn2_template_id = template_id
        catalyst_row = settings_store.get_service_settings(db, "catalyst")
        catalyst_secret = settings_store.decrypt_secret(catalyst_row)
        netbox_row = settings_store.get_service_settings(db, "netbox")
        netbox_secret = settings_store.decrypt_secret(netbox_row)
        for device in job.devices:
            if device.id in device_params:
                device.state = "dayn_queued"
                device.error = None

    netbox_settings: tuple[str, str, bool] | None = None
    if netbox_row is not None and netbox_row.base_url and netbox_secret:
        netbox_settings = (netbox_row.base_url, netbox_secret, netbox_row.tls_verify)

    if not _catalyst_ok(catalyst_row, catalyst_secret):
        _finish(job_id, error="Catalyst Center is not configured.")
        return

    assert catalyst_row is not None and catalyst_secret is not None
    async with CatalystCenterClient(
        catalyst_row.base_url or "",
        catalyst_row.username or "",
        catalyst_secret,
        tls_verify=catalyst_row.tls_verify,
    ) as client:
        await asyncio.gather(
            *(
                _deploy_one(
                    client,
                    netbox_settings,
                    job_id,
                    device_id,
                    template_id,
                    params,
                    poll_interval,
                    task_timeout,
                    activate,
                )
                for device_id, params in device_params.items()
            ),
            return_exceptions=True,
        )
    _finish(job_id)


def _catalyst_ok(row: ServiceSettings | None, secret: str | None) -> bool:
    return row is not None and bool(row.base_url) and bool(row.username) and bool(secret)


def _finish(job_id: int, error: str | None = None) -> None:
    with open_session() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        if error:
            for device in job.devices:
                if device.state in ("dayn_queued", "dayn_deploying"):
                    device.state = "dayn_failed"
                    device.error = error
        states = {
            d.state for d in job.devices if d.state.startswith(("dayn_", "completed", "activate_"))
        }
        job.current_step = 5
        if states <= {"completed"} and states:
            job.status = "completed"
        elif "completed" in states or "activate_failed" in states:
            # §8: NetBox PATCH failure after successful Day-N ⇒ partial_success
            job.status = "partial_success"
        else:
            job.status = "dayn_failed"
