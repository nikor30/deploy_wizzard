"""Settings API: credentials are write-only; GET returns masked values."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.clients.catalyst import CatalystCenterClient
from app.clients.netbox import NetBoxClient
from app.crypto import mask_secret
from app.db.models import AppSetting, DayNMapping, ServiceSettings, TemplateSecret
from app.db.session import get_db
from app.errors import PnPBridgeError
from app.services import settings_store
from app.services.connections import get_catalyst_client, get_netbox_client
from app.services.dayn import load_device_context, resolve_variables
from app.services.suggest import suggest_variable_mappings

router = APIRouter(prefix="/api/settings", tags=["settings"])
logger = logging.getLogger(__name__)

DbSession = Annotated[Session, Depends(get_db)]


class ServiceSettingsIn(BaseModel):
    base_url: str | None = None
    username: str | None = None
    # None = keep the stored secret; "" = clear it; anything else = replace it.
    secret: str | None = None
    tls_verify: bool = True
    enabled: bool = True


class CredentialsIn(BaseModel):
    catalyst: ServiceSettingsIn | None = None
    netbox: ServiceSettingsIn | None = None
    webhook: ServiceSettingsIn | None = None


class ServiceSettingsOut(BaseModel):
    base_url: str | None = None
    username: str | None = None
    secret_masked: str | None = Field(default=None, description="e.g. ****abcd")
    tls_verify: bool = True
    enabled: bool = True
    configured: bool = False


class CredentialsOut(BaseModel):
    catalyst: ServiceSettingsOut
    netbox: ServiceSettingsOut
    webhook: ServiceSettingsOut


class TestResult(BaseModel):
    ok: bool
    detail: str


def _to_out(row: ServiceSettings | None) -> ServiceSettingsOut:
    if row is None:
        return ServiceSettingsOut()
    secret = settings_store.decrypt_secret(row)
    return ServiceSettingsOut(
        base_url=row.base_url,
        username=row.username,
        secret_masked=mask_secret(secret),
        tls_verify=row.tls_verify,
        enabled=row.enabled,
        configured=bool(row.base_url),
    )


@router.get("/credentials")
def get_credentials(db: DbSession) -> CredentialsOut:
    return CredentialsOut(
        catalyst=_to_out(settings_store.get_service_settings(db, "catalyst")),
        netbox=_to_out(settings_store.get_service_settings(db, "netbox")),
        webhook=_to_out(settings_store.get_service_settings(db, "webhook")),
    )


@router.put("/credentials")
def put_credentials(payload: CredentialsIn, db: DbSession) -> CredentialsOut:
    for service in settings_store.SERVICES:
        block: ServiceSettingsIn | None = getattr(payload, service)
        if block is None:
            continue
        settings_store.upsert_service_settings(
            db,
            service,
            base_url=block.base_url,
            username=block.username,
            secret=block.secret,
            tls_verify=block.tls_verify,
            enabled=block.enabled,
        )
        logger.info("Stored settings for %s", service)
    return get_credentials(db)


class TestRequest(ServiceSettingsIn):
    """Test with the submitted values; a missing secret falls back to the stored one."""


def _resolve_test_input(
    db: Session, service: str, payload: TestRequest
) -> tuple[str, str | None, str | None, bool]:
    stored = settings_store.get_service_settings(db, service)
    base_url = payload.base_url or (stored.base_url if stored else None)
    username = payload.username or (stored.username if stored else None)
    secret = payload.secret if payload.secret else settings_store.decrypt_secret(stored)
    tls_verify = payload.tls_verify
    if not base_url:
        raise HTTPException(status_code=422, detail=f"No base URL configured for {service}.")
    return base_url, username, secret, tls_verify


@router.post("/credentials/catalyst/test")
async def test_catalyst(payload: TestRequest, db: DbSession) -> TestResult:
    base_url, username, secret, tls_verify = _resolve_test_input(db, "catalyst", payload)
    if not username or not secret:
        raise HTTPException(status_code=422, detail="Catalyst Center needs username + password.")
    try:
        async with CatalystCenterClient(
            base_url, username, secret, tls_verify=tls_verify
        ) as client:
            site_count = await client.test_connection()
    except PnPBridgeError as exc:
        logger.warning("Catalyst connection test failed: %s", exc.message)
        return TestResult(ok=False, detail=exc.message)
    return TestResult(ok=True, detail=f"Connected. {site_count} sites visible.")


class DayNMappingItem(BaseModel):
    variable: str
    source_path: str


class DayNMappingList(BaseModel):
    mappings: list[DayNMappingItem]


@router.get("/dayn")
def get_dayn_mappings(db: DbSession) -> DayNMappingList:
    rows = db.scalars(select(DayNMapping).order_by(DayNMapping.variable)).all()
    return DayNMappingList(
        mappings=[DayNMappingItem(variable=r.variable, source_path=r.source_path) for r in rows]
    )


@router.put("/dayn")
def put_dayn_mappings(payload: DayNMappingList, db: DbSession) -> DayNMappingList:
    """Replace the full variable-mapping table."""
    seen: set[str] = set()
    for item in payload.mappings:
        if item.variable in seen:
            raise HTTPException(
                status_code=422, detail=f"Duplicate mapping for variable '{item.variable}'."
            )
        seen.add(item.variable)
    db.execute(delete(DayNMapping))
    for item in payload.mappings:
        db.add(DayNMapping(variable=item.variable, source_path=item.source_path))
    db.flush()
    logger.info("Stored %d Day-N variable mappings", len(payload.mappings))
    return get_dayn_mappings(db)


@router.post("/credentials/netbox/test")
async def test_netbox(payload: TestRequest, db: DbSession) -> TestResult:
    base_url, _username, secret, tls_verify = _resolve_test_input(db, "netbox", payload)
    if not secret:
        raise HTTPException(status_code=422, detail="NetBox needs an API token.")
    try:
        async with NetBoxClient(base_url, secret, tls_verify=tls_verify) as client:
            version = await client.test_connection()
    except PnPBridgeError as exc:
        logger.warning("NetBox connection test failed: %s", exc.message)
        return TestResult(ok=False, detail=exc.message)
    return TestResult(ok=True, detail=f"Connected. NetBox {version}.")


class DayNSuggestRequest(BaseModel):
    template_id: str


class DayNSuggestion(BaseModel):
    variable: str
    source_path: str | None
    confidence: float


@router.post("/dayn/suggest")
async def suggest_dayn_mappings(payload: DayNSuggestRequest, db: DbSession) -> list[DayNSuggestion]:
    """Pre-match a template's variables against NetBox data.

    Uses a sample NetBox device (prefer status `planned`) to discover the
    available fields, custom fields, and config-context keys. Suggestions are
    review material for the Day-N settings page — nothing is saved here.
    """
    async with get_catalyst_client(db) as catalyst:
        template = await catalyst.get_template(payload.template_id)
    variables = [
        str(p.get("parameterName"))
        for p in template.get("templateParams", [])
        if p.get("parameterName")
    ]
    if not variables:
        return []

    async with get_netbox_client(db) as netbox:
        sample_devices = await netbox.get_devices(status="planned")
        if not sample_devices:
            sample_devices = await netbox.get_devices()
        if not sample_devices:
            raise HTTPException(
                status_code=422,
                detail="No NetBox device found to sample fields from - create at least one "
                "device in NetBox first.",
            )
        # enrich so the computed CC-style paths (device.uplink_ports,
        # device.site_vlans, …) are offered as candidates too
        context = await load_device_context(netbox, sample_devices[0])
    secret_names = list(db.scalars(select(TemplateSecret.name)).all())
    suggestions = suggest_variable_mappings(variables, context["device"], secret_names=secret_names)
    logger.info(
        "Suggested %d of %d Day-N variables for template %s",
        sum(1 for s in suggestions.values() if s["source_path"]),
        len(variables),
        payload.template_id,
    )
    return [
        DayNSuggestion(
            variable=variable,
            source_path=info["source_path"],
            confidence=info["confidence"],
        )
        for variable, info in suggestions.items()
    ]


class DayNPreviewRequest(BaseModel):
    serial: str
    template_id: str | None = None


class DayNPreviewVariable(BaseModel):
    variable: str
    source_path: str | None
    value: str | None
    source: str  # mapped | manual | secret


class DayNPreviewOut(BaseModel):
    netbox_device_id: int
    netbox_name: str | None
    netbox_site: str | None
    variables: list[DayNPreviewVariable]


@router.post("/dayn/preview")
async def preview_dayn_for_serial(payload: DayNPreviewRequest, db: DbSession) -> DayNPreviewOut:
    """Resolve the current Day-N variable mappings against one real NetBox
    device, looked up by serial, so the operator can verify the values against
    reality before deploying. Read-only — nothing is saved and no device is
    touched. Secret-sourced variables show `****`, never the plaintext.
    """
    serial = payload.serial.strip()
    if not serial:
        raise HTTPException(status_code=422, detail="Enter a device serial number to preview.")

    async with get_netbox_client(db) as netbox:
        matches = await netbox.get_devices(serial=serial)
        if not matches:
            raise HTTPException(
                status_code=404,
                detail=f"No NetBox device with serial '{serial}'.",
            )
        device = matches[0]
        context = await load_device_context(netbox, device)

    variables: list[str] = []
    mappings = {m.variable: m.source_path for m in db.scalars(select(DayNMapping)).all()}
    if payload.template_id:
        async with get_catalyst_client(db) as catalyst:
            template = await catalyst.get_template(payload.template_id)
        variables = [
            str(p.get("parameterName"))
            for p in template.get("templateParams", [])
            if p.get("parameterName")
        ]
    else:
        variables = sorted(mappings)

    secret_names = set(db.scalars(select(TemplateSecret.name)).all())
    resolved = resolve_variables(variables, mappings, context, secret_names=secret_names)
    return DayNPreviewOut(
        netbox_device_id=int(device["id"]),
        netbox_name=device.get("name"),
        netbox_site=(device.get("site") or {}).get("name"),
        variables=[
            DayNPreviewVariable(
                variable=variable,
                source_path=mappings.get(variable),
                value=info.get("value"),
                source=str(info.get("source")),
            )
            for variable, info in resolved.items()
        ],
    )


class TemplateSecretOut(BaseModel):
    name: str
    secret_masked: str


class TemplateSecretIn(BaseModel):
    secret: str = Field(min_length=1, max_length=1024)


@router.get("/secrets")
def list_template_secrets(db: DbSession) -> list[TemplateSecretOut]:
    """Named template secrets, values masked — the plaintext is write-only."""
    rows = db.scalars(select(TemplateSecret).order_by(TemplateSecret.name)).all()
    box = settings_store.get_secret_box()
    return [
        TemplateSecretOut(
            name=row.name,
            secret_masked=mask_secret(box.decrypt(row.secret_encrypted)) or "****",
        )
        for row in rows
    ]


@router.put("/secrets/{name}")
def upsert_template_secret(
    name: str, payload: TemplateSecretIn, db: DbSession
) -> TemplateSecretOut:
    """Create or replace a template secret; usable as `secret.<name>` in
    Day-N variable mappings."""
    row = db.scalar(select(TemplateSecret).where(TemplateSecret.name == name))
    encrypted = settings_store.get_secret_box().encrypt(payload.secret)
    if row is None:
        row = TemplateSecret(name=name, secret_encrypted=encrypted)
        db.add(row)
    else:
        row.secret_encrypted = encrypted
    db.flush()
    logger.info("Stored template secret", extra={"secret_name": name})
    return TemplateSecretOut(name=name, secret_masked=mask_secret(payload.secret) or "****")


@router.delete("/secrets/{name}", status_code=204)
def delete_template_secret(name: str, db: DbSession) -> None:
    row = db.scalar(select(TemplateSecret).where(TemplateSecret.name == name))
    if row is None:
        raise HTTPException(status_code=404, detail=f"No template secret named '{name}'.")
    db.delete(row)
    db.flush()
    logger.info("Deleted template secret", extra={"secret_name": name})


class AppFlags(BaseModel):
    # global "debug" view: show the source of every wizard variable
    # (netbox / mapped / manual) so operators can verify Day-0/Day-N coverage
    debug: bool = False


def _flag(db: Session, key: str) -> bool:
    row = db.get(AppSetting, key)
    return row is not None and row.value == "true"


@router.get("/flags")
def get_flags(db: DbSession) -> AppFlags:
    return AppFlags(debug=_flag(db, "debug"))


@router.put("/flags")
def put_flags(payload: AppFlags, db: DbSession) -> AppFlags:
    value = "true" if payload.debug else "false"
    row = db.get(AppSetting, "debug")
    if row is None:
        db.add(AppSetting(key="debug", value=value))
    else:
        row.value = value
    db.flush()
    logger.info("Set debug flag to %s", payload.debug)
    return AppFlags(debug=payload.debug)
