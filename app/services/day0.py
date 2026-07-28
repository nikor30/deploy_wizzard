"""Day-0 claim orchestration: payload builder, PnP polling, webhook trigger.

Per-device isolation is non-negotiable (CLAUDE.md §11): one failed device
never aborts or rolls back its siblings.
"""

import asyncio
import ipaddress
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.clients.catalyst import CatalystCenterClient
from app.clients.webhook import send_webhook
from app.db.models import Job, JobDevice, ServiceSettings, TemplateSecret, WebhookDelivery
from app.db.session import open_session
from app.errors import ConfigurationError, PnPBridgeError, TaskTimeout
from app.services import settings_store
from app.services.dayn import SECRET, SECRET_MASK, hidden_variable, poll_task, resolve_path
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
# a global variable / template secret matched by name resolves to source
# SECRET (from app.services.dayn) — masked here, decrypted only for the claim

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
    "MGMTVLANIP": "mgmt_ip",
    "MGMTMASK": "mgmt_mask",
    "SUBNETMASK": "mgmt_mask",
    "NETMASK": "mgmt_mask",
    "MASK": "mgmt_mask",
    "MGMTPREFIX": "mgmt_prefix",
    "PREFIX": "mgmt_prefix",
    "PREFIXLENGTH": "mgmt_prefix",
    "MGMTSUBNET": "mgmt_subnet",
    "SUBNET": "mgmt_subnet",
    "GATEWAY": "gateway",
    "DEFAULTGATEWAY": "gateway",
    "GW": "gateway",
    "DEFGW": "gateway",
    "MGMTVLAN": "mgmt_vlan",
    "MANAGEMENTVLAN": "mgmt_vlan",
    "VLAN": "mgmt_vlan",
    "MGMTVLANID": "mgmt_vlan",
    "MGMTVLANNAME": "mgmt_vlan_name",
    "VLANNAME": "mgmt_vlan_name",
}

# Variables whose value comes from the NetBox device context by dot-path
# (not from the JobDevice match row). Role covers switchType.
DAY0_CONTEXT_ALIASES: dict[str, str] = {
    "SWITCHTYPE": "device.role.name",
    "SWITCHTYP": "device.role.name",
    "DEVICEROLE": "device.role.name",
    "ROLE": "device.role.name",
    "SITE": "device.site.name",
    "LOCATION": "device.location.name",
    "RACK": "device.rack.name",
}

# Variables the operator picks from a fixed list rather than typing freely or
# deriving from NetBox. `campusswitch` is a yes/no decision, not a role lookup.
DAY0_CHOICE_VARS: dict[str, list[str]] = {
    "CAMPUSSWITCH": ["no", "yes"],
    "CAMPUSSUPSWITCH": ["no", "yes"],
}


def _normalize_var(name: str) -> str:
    return "".join(c for c in name.upper() if c.isalnum())


def day0_builtins(device: JobDevice, gateway: str | None = None) -> dict[str, str]:
    """The standard onboarding values derived from the NetBox match: hostname,
    mgmt IP/mask/prefix/subnet, mgmt VLAN + its name, and the default gateway.

    `gateway` is the address NetBox documents for the mgmt VLAN (see
    `resolve_vlan_gateway`). Without one we fall back to the first host of the
    mgmt subnet — a convention, not a fact — so the field stays editable either
    way and the operator confirms it."""
    values: dict[str, str] = {}
    if device.netbox_name:
        values["hostname"] = device.netbox_name
    if device.mgmt_ip:
        iface = ipaddress.ip_interface(device.mgmt_ip)
        values["mgmt_ip"] = str(iface.ip)
        values["mgmt_mask"] = str(iface.network.netmask)
        values["mgmt_prefix"] = str(iface.network.prefixlen)
        values["mgmt_subnet"] = str(iface.network)
        hosts = iface.network.hosts()
        first = next(iter(hosts), None)
        if first is not None:
            values["gateway"] = str(first)
    if gateway:
        values["gateway"] = gateway  # NetBox beats the first-host guess
    if device.mgmt_vlan is not None:
        values["mgmt_vlan"] = str(device.mgmt_vlan)
        for option in device.vlan_options or []:
            if option.get("vid") == device.mgmt_vlan and option.get("name"):
                values["mgmt_vlan_name"] = str(option["name"])
                break
    return values


def resolve_day0_variables(
    variables: list[str],
    device: JobDevice,
    context: dict[str, Any],
    mappings: dict[str, str],
    secret_names: Iterable[str] = (),
    gateway: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve each Day-0 template variable, in order:
    garbled password-leak names are dropped entirely → built-in onboarding value
    (by name alias) → NetBox context alias (role/site/…) → explicit Day-N
    dot-path mapping → fixed-choice picker (campusswitch yes/no) → global
    variable / secret matched by name (set once, masked) → open for manual
    entry. `gateway` is a guess and stays editable (source `manual`)."""
    builtins = day0_builtins(device, gateway)
    secrets_by_norm = {_normalize_var(name): name for name in secret_names}
    result: dict[str, dict[str, Any]] = {}
    for variable in variables:
        if hidden_variable(variable):
            continue
        norm = _normalize_var(variable)
        key = DAY0_ALIASES.get(norm)
        if key and key in builtins:
            source = SRC_MANUAL if key == "gateway" else SRC_NETBOX
            info: dict[str, Any] = {"value": builtins[key], "source": source}
            # CCC onboarding templates consume the mgmt subnet as the interface
            # mask (`ip address <ip> <mask>`), and IOS rejects prefix/CIDR form
            # ("Invalid input" — PnP error 1413). Present the CIDR to the
            # operator but send the full dotted mask (255.255.255.0) to CCC.
            if key == "mgmt_subnet" and "mgmt_mask" in builtins:
                info["claim_value"] = builtins["mgmt_mask"]
            result[variable] = info
            continue
        context_path = DAY0_CONTEXT_ALIASES.get(norm)
        value = resolve_path(context, context_path) if context_path else None
        if value is not None:
            result[variable] = {"value": value, "source": SRC_NETBOX}
            continue
        path = mappings.get(variable)
        value = resolve_path(context, path) if path else None
        if value is not None:
            result[variable] = {"value": value, "source": SRC_MAPPED}
            continue
        choices = DAY0_CHOICE_VARS.get(norm)
        if choices is not None:
            result[variable] = {
                "value": choices[0],
                "source": SRC_MANUAL,
                "choices": list(choices),
            }
            continue
        if norm in secrets_by_norm:
            result[variable] = {
                "value": SECRET_MASK,
                "source": SECRET,
                "secret": secrets_by_norm[norm],
            }
            continue
        result[variable] = {"value": "", "source": SRC_MANUAL}
    return result


def build_claim_payload(
    device: JobDevice,
    *,
    config_id: str,
    image_id: str | None,
    overrides: dict[str, str] | None = None,
    secret_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Site-claim payload per CLAUDE.md §6.1.

    Uses the resolved `day0_variables` (template introspection) when present,
    applying operator `overrides` for open fields and decrypting secret/global
    values just-in-time from `secret_values`; empty values are omitted. Falls
    back to the legacy fixed HOSTNAME/MGMT_IP/MGMT_MASK/MGMT_VLAN set when the
    job was claimed without a prepare step."""
    if device.match_status != MATCHED:
        raise ConfigurationError(f"Device {device.serial} is not matched — cannot claim.")
    if not device.ccc_site_id:
        raise ConfigurationError(f"Device {device.serial} has no mapped CCC site.")

    parameters: list[dict[str, str]] = []
    if device.day0_variables:
        overrides = overrides or {}
        secret_values = secret_values or {}
        for variable, info in device.day0_variables.items():
            if info.get("source") == SECRET:
                value = secret_values.get(str(info.get("secret")), "")
            elif variable in overrides:
                value = overrides[variable]  # operator entry wins
            else:
                # claim_value is the wire form when it differs from the display
                # value (e.g. mgmt subnet shown as CIDR, sent as a dotted mask).
                value = info.get("claim_value") or info.get("value") or ""
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

    payload: dict[str, Any] = {
        "deviceId": device.ccc_device_id,
        "siteId": device.ccc_site_id,
        "type": "Default",
        "imageInfo": {"imageId": image_id or "", "skip": image_id is None},
        "configInfo": {"configId": config_id, "configParameters": parameters},
    }
    # Top-level device name for the PnP record ("Device Name" in the claim UI).
    # Onboarding templates' SET_HOSTNAME reads this, not a config parameter — so
    # without it CCC keeps the device's default hostname ("Switch"). Set it to
    # the NetBox name so the box comes up correctly named.
    if device.netbox_name:
        payload["hostname"] = device.netbox_name
    return payload


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
        auth_header = settings_row.auth_header
        auth_token = settings_store.decrypt_auth_token(settings_row)
        payload = _webhook_payload(job_id, device)

    result = await send_webhook(
        url,
        payload,
        secret=secret,
        tls_verify=tls_verify,
        auth_header=auth_header,
        auth_token=auth_token,
    )
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


# CCC inventory roles are a fixed enum; anything else is rejected. The NetBox
# role name is free text ("Access", "access-switch", "Campus Access"), so match
# on a keyword rather than equality.
CCC_ROLES: tuple[tuple[str, str], ...] = (
    ("access", "ACCESS"),
    ("distribution", "DISTRIBUTION"),
    ("core", "CORE"),
    ("border", "BORDER ROUTER"),
)

# day0_variables keys that carry the switch role (see DAY0_ALIASES).
ROLE_VARIABLES: tuple[str, ...] = ("SWITCHTYPE", "SWITCHTYP", "DEVICEROLE", "ROLE")

# CCC tag names are group names: it rejects anything outside letters, digits,
# space, dash, underscore and dot with NCGR10060 ("The specified group name is
# invalid"). NetBox role names are free text, so normalise before creating.
_TAG_SAFE = set(" -_.")


def ccc_tag_name(role_name: str) -> str | None:
    """A CCC-acceptable tag name for a NetBox role, or None if nothing is left."""
    cleaned = "".join(c if c.isalnum() or c in _TAG_SAFE else "_" for c in role_name).strip()
    # collapse the runs of underscores a messy name can leave behind
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned.strip(" -_.")
    # a name of nothing but separators is not a name
    if not any(c.isalnum() for c in cleaned):
        return None
    return cleaned[:128]


def device_role_name(
    day0_variables: dict[str, Any] | None, netbox_role: str | None = None
) -> str | None:
    """The role to mirror into CCC inventory.

    The value the operator saw (and could edit) in wizard step 3 wins, so an
    edit there is honoured. Not every Day-0 template declares a role variable
    though, and the role is still known from the NetBox match — so fall back to
    that rather than leaving the CCC inventory role unset."""
    for key, value in (day0_variables or {}).items():
        if _normalize_var(key) in ROLE_VARIABLES and str(value).strip():
            return str(value).strip()
    return netbox_role.strip() if netbox_role and netbox_role.strip() else None


def ccc_role(role_name: str | None) -> str | None:
    """NetBox role name -> Catalyst Center inventory role, or None if it maps
    to nothing CCC accepts (better to leave the role alone than guess)."""
    lowered = (role_name or "").casefold()
    for keyword, role in CCC_ROLES:
        if keyword in lowered:
            return role
    return None


async def _apply_inventory_metadata(
    client: CatalystCenterClient, job_id: int, device_id: int
) -> None:
    """Mirror the wizard's role selection into CCC inventory as role + tag.

    Best effort by design: the switch is already onboarded at this point, so a
    failure here is logged and surfaced in the Logs page but never turns a good
    Day-0 into a failed one.
    """
    with open_session() as db:
        device = db.get(JobDevice, device_id)
        if device is None or not device.mgmt_ip:
            return
        serial = device.serial
        ip = device.mgmt_ip.split("/")[0]
        role_name = device_role_name(device.day0_variables, device.netbox_role)

    if not role_name:
        return
    role = ccc_role(role_name)
    try:
        inventory = await client.get_network_device_by_ip(ip)
        uuid = str(inventory.get("id") or "")
        if not uuid:
            logger.warning(
                "Device not in CCC inventory yet — role/tag not set",
                extra={"job_id": job_id, "serial": serial},
            )
            return
        if role:
            await client.set_device_role(uuid, role)
        tag_name = ccc_tag_name(role_name)
        if tag_name:
            tag_id = await client.ensure_tag(tag_name)
            await client.tag_device(tag_id, uuid)
        logger.info(
            "Applied CCC inventory role and tag",
            extra={"job_id": job_id, "serial": serial, "role": role, "tag": tag_name},
        )
    except Exception as exc:  # non-fatal: onboarding already succeeded
        logger.warning(
            "Could not set CCC inventory role/tag (Day-0 itself succeeded): %s",
            exc,
            extra={"job_id": job_id, "serial": serial},
        )


async def _provision_to_site(
    client: CatalystCenterClient,
    job_id: int,
    device_id: int,
    poll_interval: float,
    device_timeout: float,
) -> str | None:
    """Provision the claimed device to its site; returns an error or None.

    This is the step that pushes the site's network settings (AAA, RADIUS/
    TACACS, DNS, DHCP, NTP, syslog) to the switch. Claim + template deploy do
    not, which is why an otherwise green onboarding produced a switch with no
    AAA config at all.
    """
    with open_session() as db:
        device = db.get(JobDevice, device_id)
        if device is None:
            return None
        serial = device.serial
        site_id = device.ccc_site_id
        ip = (device.mgmt_ip or "").split("/")[0]
    if not site_id:
        return "No Catalyst Center site resolved for this device — cannot provision."
    if not ip:
        return "No management IP for this device — cannot look it up in CCC inventory."

    inventory = await client.get_network_device_by_ip(ip)
    uuid = str(inventory.get("id") or "")
    if not uuid:
        return (
            f"Device {ip} is not in the Catalyst Center inventory yet, so it cannot be "
            "provisioned. Network settings (AAA/RADIUS/DNS/DHCP) were NOT applied."
        )
    response = await client.provision_devices(site_id, uuid)
    inner = response.get("response")
    task_id = (inner or {}).get("taskId") if isinstance(inner, dict) else None
    if not task_id:
        return "Catalyst Center accepted the provision request but returned no taskId."
    await poll_task(
        client,
        str(task_id),
        poll_interval=poll_interval,
        task_timeout=device_timeout,
        label="Provisioning",
    )
    logger.info("Provisioned to site", extra={"job_id": job_id, "serial": serial})
    return None


async def _claim_one(
    client: CatalystCenterClient,
    job_id: int,
    device_id: int,
    payload: dict[str, Any],
    poll_interval: float,
    device_timeout: float,
    provision: bool,
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

    await _apply_inventory_metadata(client, job_id, device_id)

    provision_warning: str | None = None
    if provision:
        try:
            provision_warning = await _provision_to_site(
                client, job_id, device_id, poll_interval, device_timeout
            )
        except PnPBridgeError as exc:
            provision_warning = exc.message
        except Exception as exc:  # per-device isolation
            logger.exception("Unexpected provisioning error", extra={"job_id": job_id})
            provision_warning = str(exc)
        if provision_warning:
            logger.error(
                "Claim succeeded but provisioning failed",
                extra={
                    "job_id": job_id,
                    "ccc_device_id": ccc_device_id,
                    "error": provision_warning,
                },
            )

    # The claim itself worked, so the device stays claimable for Day-N — a
    # failed provision must not strand the whole batch at "0 devices". It is
    # recorded as a warning instead, because a switch without its site's
    # AAA/RADIUS/DNS/DHCP is not a silent success either.
    _set_device_state(
        device_id,
        "success",
        error=(
            "Warning: the Day-0 claim succeeded, but provisioning the device to its site "
            f"failed, so the site's network settings (AAA/RADIUS/DNS/DHCP) were NOT applied: "
            f"{provision_warning} — provision it manually in Catalyst Center, or turn off "
            "'Provision to site after claim' in Settings to stop attempting it."
            if provision_warning
            else None
        ),
    )
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
        provision = settings_store.provision_after_claim(db)
        # decrypt template secrets once (name -> plaintext) for global variables
        box = settings_store.get_secret_box()
        secret_values = {
            row.name: box.decrypt(row.secret_encrypted)
            for row in db.scalars(select(TemplateSecret)).all()
        }
        work: list[tuple[int, dict[str, Any]]] = []
        for device in job.devices:
            if device.match_status != MATCHED:
                continue
            try:
                payload = build_claim_payload(
                    device, config_id=config_id, image_id=image_id, secret_values=secret_values
                )
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
                _claim_one(
                    client,
                    job_id,
                    device_id,
                    payload,
                    poll_interval,
                    device_timeout,
                    provision,
                )
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
