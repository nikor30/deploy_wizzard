import hashlib
import hmac
import json

import app.clients.webhook as webhook_module
import httpx
import pytest
import respx
from app.clients.webhook import send_webhook, sign_payload

URL = "https://ise-helper.example.com/hook"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "BACKOFF_BASE_SECONDS", 0)


def test_signature_is_hmac_sha256_hex() -> None:
    body = b'{"event":"day0_success"}'
    expected = hmac.new(b"shared-secret", body, hashlib.sha256).hexdigest()
    assert sign_payload("shared-secret", body) == expected


@respx.mock
async def test_delivery_sends_signature_header() -> None:
    route = respx.post(URL).respond(200)
    result = await send_webhook(URL, {"event": "day0_success"}, secret="shared-secret")
    assert result.ok
    assert result.attempts == 1
    request = route.calls[0].request
    expected = hmac.new(b"shared-secret", request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-PnPB-Signature"] == expected
    assert json.loads(request.content) == {"event": "day0_success"}


@respx.mock
async def test_no_secret_means_no_signature_header() -> None:
    route = respx.post(URL).respond(200)
    result = await send_webhook(URL, {"event": "day0_success"}, secret=None)
    assert result.ok
    assert "X-PnPB-Signature" not in route.calls[0].request.headers


@respx.mock
async def test_retries_on_5xx_then_succeeds() -> None:
    route = respx.post(URL)
    route.side_effect = [httpx.Response(500), httpx.Response(502), httpx.Response(200)]
    result = await send_webhook(URL, {"e": 1}, secret=None)
    assert result.ok
    assert result.attempts == 3


@respx.mock
async def test_gives_up_after_max_attempts_with_error() -> None:
    respx.post(URL).respond(500)
    result = await send_webhook(URL, {"e": 1}, secret=None)
    assert not result.ok
    assert result.attempts == 4  # 1 + 3 retries
    assert "500" in (result.error or "")


@respx.mock
async def test_transport_error_reported() -> None:
    respx.post(URL).side_effect = httpx.ConnectError("refused")
    result = await send_webhook(URL, {"e": 1}, secret=None)
    assert not result.ok
    assert "refused" in (result.error or "")


async def test_auth_token_is_sent_so_token_based_receivers_stop_answering_401() -> None:
    """An HMAC signature proves the payload came from us; it does not
    *authenticate* us to a receiver that wants a token — those answer 401 no
    matter how well the body is signed."""
    with respx.mock as respx_mock:
        route = respx_mock.post(URL).respond(200)
        result = await send_webhook(
            URL, {"event": "day0_success"}, secret="s3cret", auth_token="Bearer abc123"
        )
    assert result.ok
    headers = route.calls[0].request.headers
    assert headers["Authorization"] == "Bearer abc123"  # default header name
    assert headers["X-PnPB-Signature"], "HMAC still applied alongside the token"


async def test_auth_header_name_is_configurable() -> None:
    with respx.mock as respx_mock:
        route = respx_mock.post(URL).respond(200)
        await send_webhook(
            URL, {"event": "day0_success"}, auth_header="X-Api-Token", auth_token="plain-token"
        )
    assert route.calls[0].request.headers["X-Api-Token"] == "plain-token"
