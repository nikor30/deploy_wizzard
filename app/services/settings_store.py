"""Read/write encrypted service settings (catalyst, netbox, webhook) and
app-wide preferences stored in the key/value AppSetting table."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.catalyst import PNP_ACTIONABLE_STATES, PNP_SELECTABLE_STATES
from app.config import get_settings
from app.crypto import SecretBox
from app.db.models import AppSetting, ServiceSettings
from app.errors import ConfigurationError

SERVICES = ("catalyst", "netbox", "webhook")


def pnp_states(db: Session) -> list[str]:
    """PnP workflow states wizard step 1 lists (Settings → Credentials).

    Falls back to the actionable default when unset or when the stored value
    holds nothing recognizable, so a bad row can never blank the device list.
    """
    row = db.get(AppSetting, "pnp_states")
    if row is None or not row.value:
        return list(PNP_ACTIONABLE_STATES)
    selected = [state for state in row.value.split(",") if state in PNP_SELECTABLE_STATES]
    return selected or list(PNP_ACTIONABLE_STATES)


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
