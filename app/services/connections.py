"""Build configured API clients from the stored (decrypted) credentials."""

from sqlalchemy.orm import Session

from app.clients.catalyst import CatalystCenterClient
from app.clients.netbox import NetBoxClient
from app.errors import ConfigurationError
from app.services import settings_store


def get_catalyst_client(db: Session) -> CatalystCenterClient:
    row = settings_store.get_service_settings(db, "catalyst")
    secret = settings_store.decrypt_secret(row)
    if row is None or not row.base_url or not row.username or not secret:
        raise ConfigurationError(
            "Catalyst Center is not configured. Set URL, username and password "
            "under Settings → Credentials."
        )
    return CatalystCenterClient(row.base_url, row.username, secret, tls_verify=row.tls_verify)


def get_netbox_client(db: Session) -> NetBoxClient:
    row = settings_store.get_service_settings(db, "netbox")
    secret = settings_store.decrypt_secret(row)
    if row is None or not row.base_url or not secret:
        raise ConfigurationError(
            "NetBox is not configured. Set URL and API token under Settings → Credentials."
        )
    return NetBoxClient(row.base_url, secret, tls_verify=row.tls_verify)
