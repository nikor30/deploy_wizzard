"""Day-0 claim orchestration: payload builder, PnP polling, webhook trigger.

Per-device isolation is non-negotiable (CLAUDE.md §11): one failed device
never aborts or rolls back its siblings.
"""

import asyncio
import ipaddress
import logging
from datetime import UTC, datetime
from typing import Any

from app.clients.catalyst import CatalystCenterClient
from app.clients.webhook import send_webhook
from app.db.models import Job, JobDevice, ServiceSettings, WebhookDelivery
from app.db.session import open_session
from app.errors import ConfigurationError, PnPBridgeError, TaskTimeout
from app.services import settings_store
from app.services.dayn import resolve_path
from app.services.matching import MATCHED

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0
DEVICE_TIMEOUT_SECONDS = 30 * 60

# PnP deviceInfo.state values treated as terminal (§6.1 baseline — verify
# against live fixtures; unknown error-ish states fail loudly via timeout).
STATE_SUCCESS = "Provisioned"
STATES_FAILED = ("Error", "Failed")


# Source labels for a resolved Day-0 variable (also used by the UI).
SRC_NETBOX = "netbox"  # prefilled from the NetBox match (read-only)
SRC_MAPPED = "mapped"  # prefilled via a Day-N dot-path mapping (read-only)
SRC_MANUAL = "manual"  # open field the operator fills (may carry a suggestion)

# Normalized template-variable name -> built-in onboarding value key. The
# names CCC onboarding templates use vary, so a handful of aliases each.
DAY0_ALIASES: dict[str, str] = {
    "HOSTNAME": "hostname",
    "HOST": "hostname",
    "DEVICENAME": "hostname",
    "SYSNAME": "hostname",
    "MGMTIP": "mgmt_ip",
    "MANAGEMENTIP": "mgmt_ip",
    "IP": "mgmt_ip",
    "IPADDRESS": "mgmt_ip",
    "MGMTMASK": "mgmt_mask",
    "SUBNETMASK": "mgmt_mask",
    "NETMASK": "mgmt_mask",
    "MASK": "mgmt_mask",
    "MGMTPREFIX": "mgmt_prefix",
    "PREFIX": "mgmt_prefix",
    "PREFIXLENGTH": "mgmt_prefix",
    "GATEWAY": "gateway",
    "DEFAULTGATEWAY": "gateway",
    "GW": "gateway",
    "DEFGW": "gateway",
    "MGMTVLAN": "mgmt_vlan",
    "MANAGEMENTVLAN": "mgmt_vlan",
    "VLAN": "mgmt_vlan",
    "MGMTVLANID": "mgmt_vlan",
}


def _normalize_var(name: str) -> str:
    return "".join(c for c in name.upper() if c.isalnum())


def day0_builtins(device: JobDevice) -> dict[str, str]:
    """The standard onboarding values derived from the NetBox match: hostname,
    mgmt IP/mask/prefix, mgmt VLAN, and a best-effort gateway guess (first host
    of the mgmt subnet — the operator confirms/overrides it)."""
    values: dict[str, str] = {}
    if device.netbox_name:
        values["hostname"] = device.netbox_name
    if device.mgmt_ip:
        iface = ipaddress.ip_interface(device.mgmt_ip)
        values["mgmt_ip"] = str(iface.ip)
        values["mgmt_mask"] = str(iface.network.netmask)
        values["mgmt_prefix"] = str(iface.network.prefixlen)
        hosts = iface.network.hosts()
        first = next(iter(hosts), None)
        if first is not None:
            values["gateway"] = str(first)
    if device.mgmt_vlan is not None:
        values["mgmt_vlan"] = str(device.mgmt_vlan)
    return values


def resolve_day0_variables(
    variables: list[str],
    device: JobDevice,
    context: dict[str, Any],
    mappings: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Resolve each Day-0 template variable: first the built-in onboarding
    values (by name alias), then a Day-N dot-path mapping, else open for manual
    entry. `gateway` is a guess and stays editable (source `manual`)."""
    builtins = day0_builtins(device)
    result: dict[str, dict[str, Any]] = {}
    for variable in variables:
        key = DAY0_ALIASES.get(_normalize_var(variable))
        if key and key in builtins:
            if key == "gateway":
                # a guess — surface it as an editable manual field pre-filled
                result[variable] = {"value": builtins[key], "source": SRC_MANUAL}
            else:
                result[variable] = {"value": builtins[key], "source": SRC_NETBOX}
            continue
        path = mappings.get(variable)
        value = resolve_path(context, path) if path else None
        if value is not None:
            result[variable] = {"value": value, "source": SRC_MAPPED}
        else:
            result[variable] = {"value": "", "source": SRC_MANUAL}
    return result


def build_claim_payload(
    device: JobDevice,
    *,
    config_id: str,
    image_id: str | None,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Site-claim payload per CLAUDE.md §6.1.

    Uses the resolved `day0_variables` (template introspection) when present,
    applying operator `overrides` for open fields; empty values are omitted.
    Falls back to the legacy fixed HOSTNAME/MGMT_IP/MGMT_MASK/MGMT_VLAN set when
    the job was claimed without a prepare step."""
    if device.match_status != MATCHED:
        raise ConfigurationError(f"Device {device.serial} is not matched — cannot claim.")
    if not device.ccc_site_id:
        raise ConfigurationError(f"Device {device.serial} has no mapped CCC site.")

    parameters: list[dict[str, str]] = []
    if device.day0_variables:
        overrides = overrides or {}
        for variable, info in device.day0_variables.items():
            value = overrides.get(variable, info.get("value") or "")
            if value != "":
                parameters.append({"key": variable, "value": str(value)})
    else:
        for variable, value in day0_builtins(device).items():
            key = {
                "hostname": "HOSTNAME",
                "mgmt_ip": "MGMT_IP",
                "mgmt_mask": "MGMT_MASK",
                "mgmt_vlan": "MGMT_VLAN",
            }.get(variable)
            if key:  # gateway/prefix are omitted in the legacy fallback
                parameters.append({"key": key, "value": value})

    return {
        "deviceId": device.ccc_device_id,
        "siteId": device.ccc_site_id,
        "type": "Default",
        "imageInfo": {"imageId": image_id or "", "skip": image_id is None},
        "configInfo": {"configId": config_id, "configParameters": parameters},
    }


def _webhook_payload(job_id: int, device: JobDevice) -> dict[str, Any]:
    mgmt_ip = None
    if device.mgmt_ip:
        mgmt_ip = str(ipaddress.ip_interface(device.mgmt_ip).ip)
    return {
        "event": "day0_success",
        "timestamp": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "job_id": job_id,
        "device": {
            "serial": device.serial,
            "hostname": device.netbox_name,
            "pid": device.pid,
            "mgmt_ip": mgmt_ip,
            "mgmt_vlan": device.mgmt_vlan,
            "netbox_site": device.netbox_site_name,
            "ccc_site": device.ccc_site_name,
            "netbox_device_id": device.netbox_device_id,
        },
    }


def _set_device_state(device_id: int, state: str, error: str | None = None) -> None:
    with open_session() as db:
        device = db.get(JobDevice, device_id)
        if device is None:
            return
        device.state = state
        device.error = error
        if state == "claiming":
            device.day0_started_at = datetime.now(tz=UTC)
        if state in ("success", "failed"):
            device.day0_finished_at = datetime.now(tz=UTC)


async def _notify_ise(job_id: int, device_id: int) -> None:
    """Fire the ISE webhook for one successfully claimed device."""
    with open_session() as db:
        device = db.get(JobDevice, device_id)
        settings_row = settings_store.get_service_settings(db, "webhook")
        if device is None:
            return
        if settings_row is None or not settings_row.enabled or not settings_row.base_url:
            logger.info("Webhook not configured/enabled — skipping", extra={"job_id": job_id})
            return
        url = settings_row.base_url
        secret = settings_store.decrypt_secret(settings_row)
        tls_verify = settings_row.tls_verify
        payload = _webhook_payload(job_id, device)

    result = await send_webhook(url, payload, secret=secret, tls_verify=tls_verify)
    with open_session() as db:
        db.add(
            WebhookDelivery(
                job_id=job_id,
                device_serial=payload["device"]["serial"],
                payload=payload,
                status="delivered" if result.ok else "failed",
                attempts=result.attempts,
                last_error=result.error,
            )
        )
    if not result.ok:
        logger.error(
            "ISE webhook delivery failed (claim NOT rolled back)",
            extra={"job_id": job_id, "device_serial": payload["device"]["serial"]},
        )


async def _claim_one(
    client: CatalystCenterClient,
    job_id: int,
    device_id: int,
    payload: dict[str, Any],
    poll_interval: float,
    device_timeout: float,
) -> None:
    ccc_device_id = payload["deviceId"]
    _set_device_state(device_id, "claiming")
    try:
        await client.claim_device(payload)
        _set_device_state(device_id, "provisioning")
        deadline = asyncio.get_event_loop().time() + device_timeout
        while True:
            info = (await client.get_pnp_device(ccc_device_id)).get("deviceInfo") or {}
            state = info.get("state")
            if state == STATE_SUCCESS:
                break
            if state in STATES_FAILED:
                raise PnPBridgeError(
                    f"PnP onboarding failed (state={state}): "
                    f"{info.get('errorMessage') or 'no error detail from CCC'}"
                )
            if asyncio.get_event_loop().time() >= deadline:
                raise TaskTimeout(
                    f"Device did not reach '{STATE_SUCCESS}' within {int(device_timeout)}s "
                    f"(last state: {state})."
                )
            await asyncio.sleep(poll_interval)
    except PnPBridgeError as exc:
        logger.error(
            "Day-0 failed for device",
            extra={"job_id": job_id, "ccc_device_id": ccc_device_id, "error": exc.message},
        )
        _set_device_state(device_id, "failed", error=exc.message)
        return
    except Exception as exc:  # per-device isolation: never let one crash the batch
        logger.exception("Unexpected Day-0 error", extra={"job_id": job_id})
        _set_device_state(device_id, "failed", error=str(exc))
        return

    _set_device_state(device_id, "success")
    await _notify_ise(job_id, device_id)


async def run_day0(
    job_id: int,
    *,
    config_id: str,
    image_id: str | None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    device_timeout: float = DEVICE_TIMEOUT_SECONDS,
) -> None:
    """Claim every matched device of the job concurrently, isolated per device."""
    with open_session() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = "day0_running"
        job.current_step = 3
        job.day0_config_id = config_id
        job.day0_image_id = image_id
        catalyst_row = settings_store.get_service_settings(db, "catalyst")
        catalyst_secret = settings_store.decrypt_secret(catalyst_row)
        work: list[tuple[int, dict[str, Any]]] = []
        for device in job.devices:
            if device.match_status != MATCHED:
                continue
            try:
                payload = build_claim_payload(device, config_id=config_id, image_id=image_id)
            except PnPBridgeError as exc:
                device.state = "failed"
                device.error = exc.message
                continue
            device.state = "queued"
            device.error = None
            work.append((device.id, payload))

    if not _catalyst_configured(catalyst_row, catalyst_secret):
        _finish_job(job_id, error="Catalyst Center is not configured.")
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
                _claim_one(client, job_id, device_id, payload, poll_interval, device_timeout)
                for device_id, payload in work
            ),
            return_exceptions=True,
        )
    _finish_job(job_id)


def _catalyst_configured(row: ServiceSettings | None, secret: str | None) -> bool:
    return row is not None and bool(row.base_url) and bool(row.username) and bool(secret)


def _finish_job(job_id: int, error: str | None = None) -> None:
    with open_session() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        states = {d.state for d in job.devices if d.match_status == MATCHED}
        if error:
            for device in job.devices:
                if device.state in ("queued", "claiming", "provisioning"):
                    device.state = "failed"
                    device.error = error
            job.status = "day0_failed"
        elif states <= {"success"}:
            job.status = "day0_complete"
        elif "success" in states:
            job.status = "day0_partial"
        else:
            job.status = "day0_failed"
