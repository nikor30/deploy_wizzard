"""Read/write encrypted service settings (catalyst, netbox, webhook)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import SecretBox
from app.db.models import ServiceSettings
from app.errors import ConfigurationError

SERVICES = ("catalyst", "netbox", "webhook")


def get_secret_box() -> SecretBox:
    return SecretBox(get_settings().require_secret_key())


def get_service_settings(db: Session, service: str) -> ServiceSettings | None:
    return db.scalar(select(ServiceSettings).where(ServiceSettings.service == service))


def upsert_service_settings(
    db: Session,
    service: str,
    *,
    base_url: str | None,
    username: str | None,
    secret: str | None,
    tls_verify: bool,
    enabled: bool,
) -> ServiceSettings:
    """Store settings for a service; `secret=None` keeps the existing secret."""
    if service not in SERVICES:
        raise ConfigurationError(f"Unknown service '{service}'.")
    row = get_service_settings(db, service)
    if row is None:
        row = ServiceSettings(service=service)
        db.add(row)
    row.base_url = base_url
    row.username = username
    row.tls_verify = tls_verify
    row.enabled = enabled
    if secret is not None:
        row.secret_encrypted = get_secret_box().encrypt(secret) if secret else None
    db.flush()
    return row


def decrypt_secret(row: ServiceSettings | None) -> str | None:
    if row is None or not row.secret_encrypted:
        return None
    return get_secret_box().decrypt(row.secret_encrypted)
