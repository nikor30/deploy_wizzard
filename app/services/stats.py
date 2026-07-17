"""Dashboard aggregation and log retention."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.db.models import Job, JobDevice, LogEntry
from app.db.session import open_session

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90

ERROR_CATEGORIES = (
    ("timeout", ("did not reach", "did not finish", "timed out", "timeout")),
    ("authentication", ("credentials", "token", "401", "403")),
    ("netbox", ("netbox",)),
    ("catalyst", ("catalyst", "pnp", "task failed", "claim")),
    ("webhook", ("webhook",)),
)


def categorize_error(error: str | None) -> str:
    text = (error or "").lower()
    if not text:
        return "unknown"
    for category, needles in ERROR_CATEGORIES:
        if any(needle in text for needle in needles):
            return category
    return "other"


def _avg_seconds(pairs: list[tuple[datetime | None, datetime | None]]) -> float | None:
    durations = [
        (end - start).total_seconds()
        for start, end in pairs
        if start is not None and end is not None
    ]
    if not durations:
        return None
    return round(sum(durations) / len(durations), 1)


def collect_stats(days: int) -> dict[str, Any]:
    """Aggregate job/device outcomes for the dashboard over the last N days."""
    since = datetime.now(tz=UTC) - timedelta(days=days)
    with open_session() as db:
        jobs = db.scalars(select(Job).where(Job.created_at >= since)).all()
        devices = [d for job in jobs for d in job.devices]

        claimed = [d for d in devices if d.day0_finished_at is not None and d.state != "failed"]
        day0_failed = [d for d in devices if d.state == "failed"]
        provisioned = [d for d in devices if d.state in ("completed", "activate_failed")]
        dayn_failed = [d for d in devices if d.state == "dayn_failed"]
        failed = day0_failed + dayn_failed
        finished = len(claimed) + len(day0_failed)

        failures_by_category: dict[str, int] = {}
        for device in failed + [d for d in devices if d.state == "activate_failed"]:
            category = categorize_error(device.error)
            failures_by_category[category] = failures_by_category.get(category, 0) + 1

        jobs_over_time: dict[str, dict[str, int]] = {}
        for job in jobs:
            day = job.created_at.date().isoformat()
            bucket = jobs_over_time.setdefault(day, {"jobs": 0, "succeeded": 0, "failed": 0})
            bucket["jobs"] += 1
            for device in job.devices:
                if device.state in ("completed", "success", "activate_failed"):
                    bucket["succeeded"] += 1
                elif device.state in ("failed", "dayn_failed"):
                    bucket["failed"] += 1

        return {
            "days": days,
            "totals": {
                "jobs": len(jobs),
                "devices": len(devices),
                "claimed": len(claimed),
                "provisioned": len(provisioned),
                "failed": len(failed),
            },
            "success_rate": round(len(claimed) / finished, 3) if finished else None,
            "avg_day0_seconds": _avg_seconds(
                [(d.day0_started_at, d.day0_finished_at) for d in devices]
            ),
            "avg_dayn_seconds": _avg_seconds(
                [(d.dayn_started_at, d.dayn_finished_at) for d in devices]
            ),
            "failures_by_category": failures_by_category,
            "jobs_over_time": [
                {"date": day, **bucket} for day, bucket in sorted(jobs_over_time.items())
            ],
        }


def cleanup_old_logs(retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete log entries older than the retention window; returns rows removed."""
    cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
    with open_session() as db:
        cursor = db.execute(delete(LogEntry).where(LogEntry.timestamp < cutoff))
        removed = int(getattr(cursor, "rowcount", 0) or 0)
    if removed:
        logger.info("Log retention removed %d entries older than %d days", removed, retention_days)
    return removed


# JobDevice is re-exported for the API layer's eligibility checks.
__all__ = ["JobDevice", "categorize_error", "cleanup_old_logs", "collect_stats"]
