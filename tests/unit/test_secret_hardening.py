"""Secrets must never leave the app through a side door (code review, package 1).

- the connection tests must not send a STORED secret to a caller-supplied URL
- redaction must work on secret VALUES, not only on secret-looking key names
  (claim configParameters, Day-N params, message text, CCC error texts)
- operator-entered Day-0 values reach the claim but are not persisted
"""

import json
import logging
from typing import Any

import app.clients.webhook as webhook_module
import pytest
import respx
from app.crypto import SecretBox
from app.db.models import JobDevice
from app.db.session import open_session
from app.logging_setup import REDACTED, JsonFormatter, register_secret, scrub
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from tests.unit.test_day0_service import CCC, HOOK, _mock_ccc, _pnp_state, _setup

STORED = {
    "catalyst": {
        "base_url": "https://ccc.example.com",
        "username": "admin",
        "secret": "catalyst-password-9999",
    },
    "netbox": {"base_url": "https://netbox.example.com", "secret": "netbox-token-8888"},
    "webhook": {
        "base_url": "https://ise-helper.example.com/hook",
        "secret": "hmac-shared-7777",
        "auth_token": "Bearer hook-token-6666",
        "enabled": True,
    },
}
ATTACKER = "https://collector.invalid"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "BACKOFF_BASE_SECONDS", 0)


# --- connection tests -------------------------------------------------------


@pytest.mark.parametrize("service", ["catalyst", "netbox", "webhook"])
def test_connection_test_never_sends_stored_secret_to_another_url(
    client: TestClient, service: str
) -> None:
    client.put("/api/settings/credentials", json=STORED)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        collector = respx_mock.route(host="collector.invalid").respond(200, json={})
        response = client.post(
            f"/api/settings/credentials/{service}/test", json={"base_url": ATTACKER}
        )
    assert response.status_code == 422
    assert not collector.called


def test_connection_test_with_stored_url_still_uses_stored_secret(client: TestClient) -> None:
    client.put("/api/settings/credentials", json=STORED)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        status = respx_mock.get("https://netbox.example.com/api/status/").respond(
            200, json={"netbox-version": "4.1.0"}
        )
        # same URL as stored (trailing slash is not a different target)
        response = client.post(
            "/api/settings/credentials/netbox/test",
            json={"base_url": "https://netbox.example.com/"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert status.calls.last.request.headers["Authorization"] == "Token netbox-token-8888"


def test_connection_test_to_new_url_works_with_submitted_secret(client: TestClient) -> None:
    client.put("/api/settings/credentials", json=STORED)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        status = respx_mock.get("https://netbox2.example.com/api/status/").respond(
            200, json={"netbox-version": "4.1.0"}
        )
        response = client.post(
            "/api/settings/credentials/netbox/test",
            json={"base_url": "https://netbox2.example.com", "secret": "new-token-5555"},
        )
    assert response.json()["ok"] is True
    assert status.calls.last.request.headers["Authorization"] == "Token new-token-5555"


# --- value-based redaction --------------------------------------------------


def test_decrypted_secret_is_redacted_wherever_it_appears() -> None:
    box = SecretBox(Fernet.generate_key().decode())
    plaintext = box.decrypt(box.encrypt("Aes-Key-Plain-4242"))
    record = logging.LogRecord("app.clients.base", logging.DEBUG, "", 0, "msg", None, None)
    record.msg = f"CCC said: Invalid CLI - Current output : key {plaintext}"
    record.request_body = {  # type: ignore[attr-defined]
        "configInfo": {"configParameters": [{"key": "AES_KEY", "value": plaintext}]},
        "params": {"RADIUS_KEY": plaintext},
    }
    text = JsonFormatter().format(record)
    assert plaintext not in text
    entry = json.loads(text)
    assert entry["request_body"]["params"]["RADIUS_KEY"] == REDACTED
    assert entry["request_body"]["configInfo"]["configParameters"][0]["key"] == "AES_KEY"
    assert REDACTED in entry["message"]


def test_very_short_values_are_not_registered() -> None:
    # masking "pw" everywhere would shred ordinary log text
    register_secret("pw")
    assert scrub("upgrade pwsh") == "upgrade pwsh"


def test_job_device_error_never_stores_a_known_secret() -> None:
    register_secret("Tacacs-Plain-3131")
    device = JobDevice(serial="X", state="failed")
    device.error = "Invalid CLI: tacacs-server key Tacacs-Plain-3131"
    assert device.error is not None
    assert "Tacacs-Plain-3131" not in device.error


# --- Day-0 manual values ------------------------------------------------------


def test_claim_sends_manual_values_but_does_not_persist_them(client: TestClient) -> None:
    job_id = _setup(client)
    devices = client.get(f"/api/wizard/jobs/{job_id}").json()["devices"]
    with open_session() as db:
        for d in devices:
            device = db.get(JobDevice, d["id"])
            assert device is not None
            device.day0_variables = {
                "HOSTNAME": {"value": device.netbox_name, "source": "netbox"},
                "ENABLE_PASSWORD": {"value": "", "source": "manual"},
            }
    manual = {str(d["id"]): {"ENABLE_PASSWORD": "Enable-Plain-2727"} for d in devices}

    claims: list[dict[str, Any]] = []
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        claim_route = respx_mock.post(
            f"{CCC}/dna/intent/api/v1/onboarding/pnp-device/site-claim"
        ).respond(200, json={"response": "Device Claimed"})
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioned"})
        respx_mock.post(HOOK).respond(200)
        response = client.post(
            f"/api/wizard/jobs/{job_id}/claim",
            json={"config_id": "tmpl-1", "manual": manual, "poll_interval": 0, "timeout": 5},
        )
        claims = [json.loads(call.request.content) for call in claim_route.calls]
    assert response.status_code == 200, response.text
    sent = [{p["key"]: p["value"] for p in c["configInfo"]["configParameters"]} for c in claims]
    assert len(sent) == 2
    assert all(params["ENABLE_PASSWORD"] == "Enable-Plain-2727" for params in sent)
    assert "Enable-Plain-2727" not in response.text
    assert "Enable-Plain-2727" not in client.get(f"/api/wizard/jobs/{job_id}").text
