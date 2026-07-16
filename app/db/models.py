from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class SiteMapping(Base):
    """Maps a NetBox site to a Catalyst Center site (hierarchy node)."""

    __tablename__ = "site_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    netbox_site_id: Mapped[int] = mapped_column(unique=True, index=True)
    netbox_site_name: Mapped[str] = mapped_column(String(256))
    ccc_site_id: Mapped[str] = mapped_column(String(64))
    ccc_site_name: Mapped[str] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ServiceSettings(Base):
    """Connection settings for one external service: catalyst, netbox, or webhook.

    `secret_encrypted` holds the service's secret (CCC password, NetBox token,
    webhook HMAC secret) encrypted with Fernet — never plaintext.
    """

    __tablename__ = "service_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    base_url: Mapped[str | None] = mapped_column(String(512))
    username: Mapped[str | None] = mapped_column(String(256))
    secret_encrypted: Mapped[str | None] = mapped_column(String(2048))
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
