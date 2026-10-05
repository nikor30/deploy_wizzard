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


# Secret VALUES known to the process. Key names alone miss most of them: a claim
# carries template secrets as `configParameters: [{"key": "AES_KEY", "value": ...}]`,
# a Day-N deploy as `params: {"RADIUS_KEY": ...}`, and CCC echoes rejected CLI
# lines (with the key in them) into error texts. SecretBox registers every
# plaintext it encrypts or decrypts; scrub() replaces them wherever they appear.
# Very short values are not registered - masking "pw" everywhere would shred logs.
MIN_SCRUB_LENGTH = 4
_known_secrets: set[str] = set()
_known_secrets_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    if value and len(value) >= MIN_SCRUB_LENGTH:
        with _known_secrets_lock:
            _known_secrets.add(value)


def scrub(text: str) -> str:
    """Replace every registered secret value in `text` with REDACTED."""
    if not text or not _known_secrets:
        return text
    with _known_secrets_lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def redact(value: Any) -> Any:
    """Recursively replace values of secret-like keys in dicts/lists and any
    registered secret value inside strings."""
    if isinstance(value, dict):
        return {
            key: REDACTED if SECRET_KEY_PATTERN.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return scrub(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        # Context passed via `logger.info(..., extra={...})`, redacted.
        for key, value in record.__dict__.items():
            if key not in _STDLIB_RECORD_FIELDS:
                entry[key] = REDACTED if SECRET_KEY_PATTERN.search(key) else redact(value)
        if record.exc_info:
            entry["exc_info"] = scrub(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


_log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
_sink_thread: threading.Thread | None = None
# Records queue up until the schema exists. setup_logging() closes the sink and
# the app lifespan opens it once Alembic has run: startup messages logged before
# the migrations (e.g. the generated-key warning on a fresh volume) used to hit a
# missing `log_entries` table and were lost from the Logs page.
_sink_open = threading.Event()


_TRACEBACK_FORMATTER = logging.Formatter()
# Keep the end of a long traceback: that is where the raising frame is.
TRACEBACK_LIMIT = 8000


def _write_log_entry(record: logging.LogRecord) -> None:
    # Imported lazily to avoid a circular import at module load time.
    from app.db.models import LogEntry
    from app.db.session import open_session

    context: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key not in _STDLIB_RECORD_FIELDS:
            context[key] = REDACTED if SECRET_KEY_PATTERN.search(key) else redact(value)
    if record.exc_info:
        # the traceback is what makes an unexpected error diagnosable from the
        # Logs page; scrubbed like the message, capped so one error stays small
        context["exc_info"] = scrub(_TRACEBACK_FORMATTER.formatException(record.exc_info))[
            -TRACEBACK_LIMIT:
        ]
    job_id = context.pop("job_id", None)
    serial = context.pop("device_serial", None) or context.pop("serial", None)
    with open_session() as db:
        db.add(
            LogEntry(
                level=record.levelname,
                component=record.name,
                message=scrub(record.getMessage())[:4096],
                job_id=int(job_id)
                if isinstance(job_id, int | str) and str(job_id).isdigit()
                else None,
                device_serial=str(serial) if serial else None,
                context=context or None,
            )
        )


_sink_failed = False


def _sink_worker() -> None:
    global _sink_failed
    while True:
        record = _log_queue.get()
        _sink_open.wait()
        try:
            _write_log_entry(record)
            _sink_failed = False
        except Exception as exc:  # a broken sink must never take the app down
            # ...but it must not fail silently either: an empty Logs page with a
            # working app looked like data loss and took a while to spot.
            if not _sink_failed:
                _sink_failed = True
                print(
                    json.dumps(
                        {
                            "timestamp": datetime.now(tz=UTC).isoformat(),
                            "level": "ERROR",
                            "logger": "app.logging_setup",
                            "message": (
                                "DB log sink write failed - the Logs page will stop updating: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                        }
                    ),
                    file=sys.stderr,
                    flush=True,
                )
        finally:
            _log_queue.task_done()


def open_db_sink() -> None:
    """Start persisting queued records; call once the DB schema is migrated."""
    _sink_open.set()


def flush_db_sink() -> None:
    """Block until every queued record is persisted (used by tests).

    A closed sink (startup failed before the migrations) is not waited on —
    its records could never be written and joining would hang forever.
    """
    if _sink_open.is_set():
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


# Loggers switched to DEBUG by the HTTP-trace flag: every outbound request and
# response to Catalyst Center / NetBox, bodies included. Kept off the root
# logger so trace never drags in SQLAlchemy/uvicorn debug noise.
TRACE_LOGGERS = ("app.clients",)


def set_http_trace(enabled: bool) -> None:
    """Turn raw request/response logging of the external APIs on or off.

    A child logger's own level decides whether a record is emitted, so this
    works regardless of the root level set from PNPB_LOG_LEVEL. Bodies still
    pass through `redact()` — trace must never leak a token.
    """
    for name in TRACE_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if enabled else logging.NOTSET)


def setup_logging(level: str = "INFO") -> None:
    global _sink_thread
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [stream_handler, DbLogHandler()]
    root.setLevel(level.upper())
    # Hold DB writes until the lifespan has migrated the schema (open_db_sink).
    _sink_open.clear()
    if _sink_thread is None or not _sink_thread.is_alive():
        _sink_thread = threading.Thread(target=_sink_worker, name="db-log-sink", daemon=True)
        _sink_thread.start()
