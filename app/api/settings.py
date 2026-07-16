"""Settings API: credentials are write-only; GET returns masked values."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.clients.catalyst import CatalystCenterClient
from app.clients.netbox import NetBoxClient
from app.crypto import mask_secret
from app.db.models import ServiceSettings
from app.db.session import get_db
from app.errors import PnPBridgeError
from app.services import settings_store

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
