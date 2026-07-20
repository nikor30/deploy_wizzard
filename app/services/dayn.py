"""Day-N provisioning: variable resolution, template deploy, NetBox activation.

Rules honored (CLAUDE.md §11): a NetBox device is set `active` only when the
Day-N task is verifiably successful; site-claim style task errors are often
buried in the task tree, so child tasks are drilled when `failureReason` is
empty; batches stay per-device isolated.
"""

import asyncio
import ipaddress
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
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

MANUAL = "manual"
MAPPED = "mapped"
SECRET = "secret"
SECRET_PREFIX = "secret."
SECRET_MASK = "****"


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


def resolve_variables(
    variables: list[str],
    mappings: dict[str, str],
    context: dict[str, Any],
    secret_names: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Resolve each template variable via its mapping; anything that cannot be
    resolved is flagged for manual entry in the wizard.

    `secret.<NAME>` paths resolve to a masked placeholder — the plaintext is
    decrypted just-in-time when building the deploy payload, never stored on
    the job or returned by the API."""
    known_secrets = set(secret_names)
    result: dict[str, dict[str, Any]] = {}
    for variable in variables:
        path = mappings.get(variable)
        if path and path.startswith(SECRET_PREFIX):
            name = path.removeprefix(SECRET_PREFIX)
            if name in known_secrets:
                result[variable] = {"value": SECRET_MASK, "source": SECRET, "secret": name}
            else:
                result[variable] = {"value": None, "source": MANUAL}
            continue
        value = resolve_path(context, path) if path else None
        result[variable] = {
            "value": value,
            "source": MAPPED if value is not None else MANUAL,
        }
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

    # Flat Catalyst-Center Day-N values (match netbox_cc_dayn resolvers).
    port_names = [str(u["name"]) for u in uplinks if u.get("name")]
    ctx["uplink_ports"] = ",".join(port_names)
    peers = {str(u["peer_device"]) for u in uplinks if u.get("peer_device")}
    # unique far-end device only; ambiguous (multiple) or none -> stays unset
    ctx["uplink_switch"] = peers.pop() if len(peers) == 1 else None
    ctx["site_vlans"] = ";".join(
        f"({v.get('vid')},{v.get('name', '')})" for v in (site_vlans or []) if v.get("vid")
    )
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


async def load_device_context(netbox: NetBoxClient, device: dict[str, Any]) -> dict[str, Any]:
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
    return build_device_context(device, interfaces, site_vlans, contacts)


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
) -> None:
    """Poll a CCC task until it ends; raise with the real reason on failure."""
    deadline = asyncio.get_event_loop().time() + task_timeout
    while True:
        task = await client.get_task(task_id)
        if task.get("isError"):
            reason = task.get("failureReason") or ""
            if not reason:
                # §11: errors are often buried in the task tree
                children = await client.get_task_tree(task_id)
                reasons = [str(c["failureReason"]) for c in children if c.get("failureReason")]
                reason = "; ".join(reasons) or "no failureReason from CCC"
            raise PnPBridgeError(f"Day-N task failed: {reason}")
        if task.get("endTime"):
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise TaskTimeout(f"Day-N task {task_id} did not finish within {int(task_timeout)}s.")
        await asyncio.sleep(poll_interval)


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


async def _deploy_one(
    client: CatalystCenterClient,
    netbox_settings: tuple[str, str, bool] | None,
    job_id: int,
    device_id: int,
    template_id: str,
    params: dict[str, str],
    poll_interval: float,
    task_timeout: float,
) -> None:
    with open_session() as db:
        device = db.get(JobDevice, device_id)
        if device is None:
            return
        serial = device.serial
        netbox_device_id = device.netbox_device_id
        try:
            payload = build_deploy_payload(template_id, device, params)
        except PnPBridgeError as exc:
            device.state = "dayn_failed"
            device.error = exc.message
            return

    _set_device(device_id, "dayn_deploying")
    try:
        response = await client.deploy_template(payload)
        task_id = str(
            (response.get("response") or {}).get("taskId") or response.get("taskId") or ""
        )
        if not task_id:
            raise PnPBridgeError("deploy/v2 did not return a taskId.")
        await poll_task(client, task_id, poll_interval=poll_interval, task_timeout=task_timeout)
    except PnPBridgeError as exc:
        logger.error("Day-N failed for device", extra={"job_id": job_id, "serial": serial})
        _set_device(device_id, "dayn_failed", error=exc.message)
        return
    except Exception as exc:  # per-device isolation
        logger.exception("Unexpected Day-N error", extra={"job_id": job_id})
        _set_device(device_id, "dayn_failed", error=str(exc))
        return

    # Day-N verifiably succeeded — only now touch the source of truth.
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
) -> None:
    """Deploy the Day-N template to every eligible device, isolated per device."""
    with open_session() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = "dayn_running"
        job.current_step = 4
        job.dayn_template_id = template_id
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
