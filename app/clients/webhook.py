"""Outbound ISE webhook sender: HMAC-SHA256 signature + retries with backoff.

Webhook failures are reported to the caller and never raise — a failed
notification must not roll back a successful Day-0 claim (CLAUDE.md §5.1).
"""

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.clients.base import DEFAULT_TIMEOUT

SIGNATURE_HEADER = "X-PnPB-Signature"
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0

logger = logging.getLogger(__name__)


@dataclass
class WebhookResult:
    ok: bool
    attempts: int
    status_code: int | None = None
    error: str | None = None


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    tls_verify: bool = True,
    request_timeout: float = DEFAULT_TIMEOUT,
) -> WebhookResult:
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers[SIGNATURE_HEADER] = sign_payload(secret, body)

    last_error: str | None = None
    status_code: int | None = None
    attempts = 0
    async with httpx.AsyncClient(verify=tls_verify, timeout=request_timeout) as client:
        for attempt in range(MAX_RETRIES + 1):
            attempts = attempt + 1
            if attempt:
                await asyncio.sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
            try:
                response = await client.post(url, content=body, headers=headers)
            except httpx.TransportError as exc:
                last_error = str(exc)
                logger.warning("Webhook delivery attempt %d failed: %s", attempts, exc)
                continue
            status_code = response.status_code
            if response.status_code < 400:
                return WebhookResult(ok=True, attempts=attempts, status_code=status_code)
            last_error = f"HTTP {response.status_code}"
            logger.warning("Webhook delivery attempt %d failed: %s", attempts, last_error)
    return WebhookResult(ok=False, attempts=attempts, status_code=status_code, error=last_error)
