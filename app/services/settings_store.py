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


def template_filter(db: Session, step: str) -> list[str]:
    """Words that a template's name must contain to be offered in `step`
    ("day0" | "dayn"). Empty list = no filtering (offer everything)."""
    row = db.get(AppSetting, f"{step}_template_filter")
    if row is None or not row.value:
        return []
    return [word.strip() for word in row.value.split(",") if word.strip()]


def filter_templates(templates: list[str], words: list[str]) -> list[str]:
    """Names containing any of `words` (case-insensitive substring match).

    An empty filter keeps everything — the setting is opt-in, so a blank value
    must never hide the templates an operator needs.
    """
    if not words:
        return templates
    lowered = [word.lower() for word in words]
    return [name for name in templates if any(word in name.lower() for word in lowered)]


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
    auth_header: str | None = None,
    auth_token: str | None = None,
) -> ServiceSettings:
    """Store settings for a service; `secret=None` keeps the existing secret.

    `auth_token` follows the same convention: None keeps it, "" clears it."""
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
    row.auth_header = auth_header or None
    if auth_token is not None:
        row.auth_token_encrypted = get_secret_box().encrypt(auth_token) if auth_token else None
    db.flush()
    return row


def decrypt_secret(row: ServiceSettings | None) -> str | None:
    if row is None or not row.secret_encrypted:
        return None
    return get_secret_box().decrypt(row.secret_encrypted)


def decrypt_auth_token(row: ServiceSettings | None) -> str | None:
    if row is None or not row.auth_token_encrypted:
        return None
    return get_secret_box().decrypt(row.auth_token_encrypted)


def provision_after_claim(db: Session) -> bool:
    """Whether to provision a device to its site after a successful claim.

    Provisioning is what pushes the site's network settings (AAA/RADIUS/TACACS,
    DNS, DHCP, NTP, syslog); claim and template deploy do not. Defaults to ON,
    so an unset row means True.
    """
    row = db.get(AppSetting, "provision_after_claim")
    return row is None or row.value == "true"


def http_trace(db: Session) -> bool:
    """Whether to log raw request/response bodies for CCC and NetBox.

    Off by default: it is a troubleshooting mode, and the volume is high.
    """
    row = db.get(AppSetting, "http_trace")
    return row is not None and row.value == "true"


def access_port_source(db: Session) -> str:
    """Where the Day-N access-port list comes from: "netbox" or "device"."""
    row = db.get(AppSetting, "access_port_source")
    return row.value if row is not None and row.value in ("netbox", "device") else "netbox"
