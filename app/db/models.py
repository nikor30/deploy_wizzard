from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Job(Base):
    """One wizard run = one batch of devices; state lives server-side so the
    browser can be closed and the job resumed."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="in_progress")
    current_step: Mapped[int] = mapped_column(default=2)
    day0_config_id: Mapped[str | None] = mapped_column(String(64))
    day0_image_id: Mapped[str | None] = mapped_column(String(64))
    dayn_template_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    devices: Mapped[list["JobDevice"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobDevice.id"
    )


class JobDevice(Base):
    """A device within a job, including its NetBox match result."""

    __tablename__ = "job_devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    serial: Mapped[str] = mapped_column(String(64))
    pid: Mapped[str | None] = mapped_column(String(64))
    ccc_device_id: Mapped[str] = mapped_column(String(64))

    match_status: Mapped[str | None] = mapped_column(String(16))
    netbox_device_id: Mapped[int | None]
    netbox_name: Mapped[str | None] = mapped_column(String(256))
    netbox_site_id: Mapped[int | None]
    netbox_site_name: Mapped[str | None] = mapped_column(String(256))
    ccc_site_id: Mapped[str | None] = mapped_column(String(64))
    ccc_site_name: Mapped[str | None] = mapped_column(String(512))
    mgmt_ip: Mapped[str | None] = mapped_column(String(64))
    mgmt_vlan: Mapped[int | None]
    vlan_options: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(24), default="pending")
    error: Mapped[str | None] = mapped_column(String(2048))
    day0_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    day0_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dayn_variables: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    dayn_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dayn_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[Job] = relationship(back_populates="devices")


class LogEntry(Base):
    """Structured log record persisted by the DB sink (context is redacted)."""

    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    level: Mapped[str] = mapped_column(String(16), index=True)
    component: Mapped[str] = mapped_column(String(128), index=True)
    message: Mapped[str] = mapped_column(String(4096))
    job_id: Mapped[int | None] = mapped_column(index=True)
    device_serial: Mapped[str | None] = mapped_column(String(64), index=True)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class DayNMapping(Base):
    """Maps a CCC template variable to a NetBox dot-path expression."""

    __tablename__ = "dayn_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    variable: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source_path: Mapped[str] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class WebhookDelivery(Base):
    """Outcome of one outbound ISE webhook delivery (retryable from the UI in P6)."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    device_serial: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
