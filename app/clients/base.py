"""Shared HTTP plumbing: timeouts and retry with backoff for idempotent GETs."""

import asyncio
import logging
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0
GET_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5

logger = logging.getLogger(__name__)


async def get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """GET with up to GET_RETRIES retries (exponential backoff) on transport
    errors and 5xx responses.

    Non-5xx responses (including 4xx) are returned immediately — they are
    deterministic and the caller maps them to typed errors.
    """
    last_error: Exception | None = None
    last_response: httpx.Response | None = None
    for attempt in range(GET_RETRIES + 1):
        if attempt:
            await asyncio.sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        try:
            response = await client.get(url, headers=headers, params=params)
        except httpx.TransportError as exc:
            last_error = exc
            logger.warning(
                "GET %s failed (attempt %d/%d): %s", url, attempt + 1, GET_RETRIES + 1, exc
            )
            continue
        if response.status_code >= 500:
            last_response = response
            logger.warning(
                "GET %s returned %d (attempt %d/%d)",
                url,
                response.status_code,
                attempt + 1,
                GET_RETRIES + 1,
            )
            continue
        return response
    if last_response is not None:
        return last_response
    assert last_error is not None
    raise last_error
