"""Logs API: searchable DB log sink + webhook delivery listing/retry."""

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.clients.webhook import send_webhook
from app.db.models import LogEntry, WebhookDelivery
from app.db.session import get_db
from app.services import settings_store

router = APIRouter(prefix="/api/logs", tags=["logs"])
logger = logging.getLogger(__name__)

DbSession = Annotated[Session, Depends(get_db)]


class LogEntryOut(BaseModel):
    id: int
    timestamp: str
    level: str
    component: str
    message: str
    job_id: int | None
    device_serial: str | None
    context: dict[str, Any] | None


class LogPage(BaseModel):
    total: int
    entries: list[LogEntryOut]


class WebhookDeliveryOut(BaseModel):
    id: int
    job_id: int
    device_serial: str
    status: str
    attempts: int
    last_error: str | None
    created_at: str
    payload: dict[str, Any]


def _apply_filters(
    statement: Select[Any],
    *,
    job_id: int | None,
    serial: str | None,
    level: str | None,
    component: str | None,
    q: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Select[Any]:
    if job_id is not None:
        statement = statement.where(LogEntry.job_id == job_id)
    if serial:
        statement = statement.where(LogEntry.device_serial == serial)
    if level:
        statement = statement.where(LogEntry.level == level.upper())
    if component:
        statement = statement.where(LogEntry.component.contains(component))
    if q:
        statement = statement.where(LogEntry.message.contains(q))
    if since:
        statement = statement.where(LogEntry.timestamp >= since)
    if until:
        statement = statement.where(LogEntry.timestamp <= until)
    return statement


@router.get("")
def list_logs(
    db: DbSession,
    job_id: int | None = None,
    serial: str | None = None,
    level: str | None = None,
    component: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogPage:
    def filtered(statement: Select[Any]) -> Select[Any]:
        return _apply_filters(
            statement,
            job_id=job_id,
            serial=serial,
            level=level,
            component=component,
            q=q,
            since=since,
            until=until,
        )

    total = db.scalar(filtered(select(func.count(LogEntry.id)))) or 0
    rows = db.scalars(
        filtered(select(LogEntry))
        .order_by(LogEntry.timestamp.desc(), LogEntry.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return LogPage(
        total=int(total),
        entries=[
            LogEntryOut(
                id=row.id,
                timestamp=row.timestamp.isoformat(),
                level=row.level,
                component=row.component,
                message=row.message,
                job_id=row.job_id,
                device_serial=row.device_serial,
                context=row.context,
            )
            for row in rows
        ],
    )


def _delivery_out(row: WebhookDelivery) -> WebhookDeliveryOut:
    return WebhookDeliveryOut(
        id=row.id,
        job_id=row.job_id,
        device_serial=row.device_serial,
        status=row.status,
        attempts=row.attempts,
        last_error=row.last_error,
        created_at=row.created_at.isoformat(),
        payload=row.payload,
    )


@router.get("/webhook-deliveries")
def list_webhook_deliveries(db: DbSession, job_id: int | None = None) -> list[WebhookDeliveryOut]:
    statement = select(WebhookDelivery).order_by(WebhookDelivery.id.desc())
    if job_id is not None:
        statement = statement.where(WebhookDelivery.job_id == job_id)
    return [_delivery_out(row) for row in db.scalars(statement).all()]


@router.post("/webhook-deliveries/{delivery_id}/retry")
async def retry_webhook_delivery(delivery_id: int, db: DbSession) -> WebhookDeliveryOut:
    """Re-send a stored webhook payload using the current webhook settings."""
    row = db.get(WebhookDelivery, delivery_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Delivery {delivery_id} not found.")
    settings_row = settings_store.get_service_settings(db, "webhook")
    if settings_row is None or not settings_row.base_url:
        raise HTTPException(status_code=400, detail="Webhook is not configured in Settings.")
    secret = settings_store.decrypt_secret(settings_row)

    result = await send_webhook(
        settings_row.base_url, row.payload, secret=secret, tls_verify=settings_row.tls_verify
    )
    row.attempts += result.attempts
    row.status = "delivered" if result.ok else "failed"
    row.last_error = result.error
    db.flush()
    logger.info(
        "Webhook delivery retried",
        extra={"job_id": row.job_id, "delivery_id": row.id, "delivered": result.ok},
    )
    return _delivery_out(row)
