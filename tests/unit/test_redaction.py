import contextlib
import json
import logging

from app.logging_setup import REDACTED, JsonFormatter, redact
from fastapi.testclient import TestClient


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


async def test_http_trace_logs_bodies_but_never_a_token(client: TestClient) -> None:
    """The trace mode exists so an operator can capture a failing call. It must
    capture the payload without ever handing over a credential."""
    import respx
    from app.clients.catalyst import CatalystCenterClient
    from app.logging_setup import set_http_trace

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    logging.getLogger("app.clients").addHandler(handler)
    set_http_trace(True)
    try:
        with respx.mock as respx_mock:
            respx_mock.post("https://ccc.example.com/dna/system/api/v1/auth/token").respond(
                200, json={"Token": "super-secret-token"}
            )
            respx_mock.get(
                "https://ccc.example.com/dna/intent/api/v1/sda/provisionDevices"
            ).respond(200, json={"response": []})
            respx_mock.post(
                "https://ccc.example.com/dna/intent/api/v1/sda/provisionDevices"
            ).respond(400, json={"errorCode": "NCSP11001", "message": "intent validation failed"})
            async with CatalystCenterClient("https://ccc.example.com", "admin", "pw") as ccc:
                with contextlib.suppress(Exception):
                    await ccc.provision_devices("site-1", "uuid-1")
    finally:
        set_http_trace(False)
        logging.getLogger("app.clients").removeHandler(handler)

    traced = [
        r
        for r in records
        if getattr(r, "http_path", None) and getattr(r, "http_method", "") == "POST"
    ]
    assert traced, "the failing call must be captured"
    entry = json.loads(JsonFormatter().format(traced[0]))
    assert entry["http_status"] == 400
    assert entry["response_body"]["errorCode"] == "NCSP11001"
    assert entry["request_body"] == [{"siteId": "site-1", "networkDeviceId": "uuid-1"}]
    assert "super-secret-token" not in json.dumps(entry)


async def test_http_trace_is_off_by_default(client: TestClient) -> None:
    flags = client.get("/api/settings/flags").json()
    assert flags["http_trace"] is False
