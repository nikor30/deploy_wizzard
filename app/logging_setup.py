"""Structured JSON logging to stdout with mandatory secret redaction.

The per-job DB sink specified in CLAUDE.md arrives with P6; this module covers
the stdout half so every phase has consistent, redacted structured logs.
"""

import json
import logging
import queue
import re
import sys
import threading
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"

# Keys whose values must never appear in logs (matched case-insensitively,
# also as substrings: "netbox_token", "x-auth-token", "webhook_secret", ...).
SECRET_KEY_PATTERN = re.compile(r"password|token|secret|authorization|api[_-]?key", re.IGNORECASE)

_STDLIB_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def redact(value: Any) -> Any:
    """Recursively replace values of secret-like keys in dicts/lists."""
    if isinstance(value, dict):
        return {
            key: REDACTED if SECRET_KEY_PATTERN.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Context passed via `logger.info(..., extra={...})`, redacted.
        for key, value in record.__dict__.items():
            if key not in _STDLIB_RECORD_FIELDS:
                entry[key] = REDACTED if SECRET_KEY_PATTERN.search(key) else redact(value)
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


_log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
_sink_thread: threading.Thread | None = None


def _write_log_entry(record: logging.LogRecord) -> None:
    # Imported lazily to avoid a circular import at module load time.
    from app.db.models import LogEntry
    from app.db.session import open_session

    context: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key not in _STDLIB_RECORD_FIELDS:
            context[key] = REDACTED if SECRET_KEY_PATTERN.search(key) else redact(value)
    job_id = context.pop("job_id", None)
    serial = context.pop("device_serial", None) or context.pop("serial", None)
    with open_session() as db:
        db.add(
            LogEntry(
                level=record.levelname,
                component=record.name,
                message=record.getMessage()[:4096],
                job_id=int(job_id)
                if isinstance(job_id, int | str) and str(job_id).isdigit()
                else None,
                device_serial=str(serial) if serial else None,
                context=context or None,
            )
        )


def _sink_worker() -> None:
    while True:
        record = _log_queue.get()
        try:
            _write_log_entry(record)
        except Exception:  # a broken sink must never take the app down
            pass
        finally:
            _log_queue.task_done()


def flush_db_sink() -> None:
    """Block until every queued record is persisted (used by tests)."""
    _log_queue.join()


class DbLogHandler(logging.Handler):
    """Queue app.* records for the DB sink worker (context redacted there).

    Writes happen on a separate thread so a request holding an open SQLite
    write transaction never waits on its own log line. SQLAlchemy/uvicorn
    records are excluded — persisting them would recurse.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("app."):
            _log_queue.put(record)


def setup_logging(level: str = "INFO") -> None:
    global _sink_thread
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [stream_handler, DbLogHandler()]
    root.setLevel(level.upper())
    if _sink_thread is None or not _sink_thread.is_alive():
        _sink_thread = threading.Thread(target=_sink_worker, name="db-log-sink", daemon=True)
        _sink_thread.start()
