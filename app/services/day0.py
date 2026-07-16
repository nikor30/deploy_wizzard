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
from app.services.matching import MATCHED

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0
DEVICE_TIMEOUT_SECONDS = 30 * 60

# PnP deviceInfo.state values treated as terminal (§6.1 baseline — verify
# against live fixtures; unknown error-ish states fail loudly via timeout).
STATE_SUCCESS = "Provisioned"
STATES_FAILED = ("Error", "Failed")


def build_claim_payload(
    device: JobDevice, *, config_id: str, image_id: str | None
) -> dict[str, Any]:
    """Site-claim payload per CLAUDE.md §6.1 with NetBox-prefilled variables."""
    if device.match_status != MATCHED:
        raise ConfigurationError(f"Device {device.serial} is not matched — cannot claim.")
    if not device.ccc_site_id:
        raise ConfigurationError(f"Device {device.serial} has no mapped CCC site.")

    parameters: list[dict[str, str]] = []
    if device.netbox_name:
        parameters.append({"key": "HOSTNAME", "value": device.netbox_name})
    if device.mgmt_ip:
        interface = ipaddress.ip_interface(device.mgmt_ip)
        parameters.append({"key": "MGMT_IP", "value": str(interface.ip)})
        parameters.append({"key": "MGMT_MASK", "value": str(interface.network.netmask)})
        gateway = None  # not derivable from NetBox data yet; Day-N mapping can supply it
        if gateway:
            parameters.append({"key": "GATEWAY", "value": gateway})
    if device.mgmt_vlan is not None:
        parameters.append({"key": "MGMT_VLAN", "value": str(device.mgmt_vlan)})

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
