"""Structured JSON logging to stdout with mandatory secret redaction.

The per-job DB sink specified in CLAUDE.md arrives with P6; this module covers
the stdout half so every phase has consistent, redacted structured logs.
"""

import json
import logging
import re
import sys
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


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
