import json
import logging

from app.logging_setup import REDACTED, JsonFormatter, redact


def test_redact_masks_secret_like_keys_recursively() -> None:
    data = {
        "username": "admin",
        "password": "hunter2",
        "nested": {"netbox_token": "abc", "items": [{"webhook_secret": "s"}]},
        "Authorization": "Token xyz",
    }
    result = redact(data)
    assert result["username"] == "admin"
    assert result["password"] == REDACTED
    assert result["nested"]["netbox_token"] == REDACTED
    assert result["nested"]["items"][0]["webhook_secret"] == REDACTED
    assert result["Authorization"] == REDACTED


def test_formatter_redacts_extra_context() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="saving",
        args=None,
        exc_info=None,
    )
    record.password = "hunter2"  # type: ignore[attr-defined]
    record.request = {"headers": {"X-Auth-Token": "tok"}}  # type: ignore[attr-defined]
    entry = json.loads(JsonFormatter().format(record))
    assert entry["password"] == REDACTED
    assert entry["request"]["headers"]["X-Auth-Token"] == REDACTED
    assert "hunter2" not in json.dumps(entry)
    assert "tok" not in json.dumps(entry["request"])
