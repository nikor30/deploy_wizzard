"""Catalyst Center Intent API client (target 2.3.7.x).

Auth: POST /dna/system/api/v1/auth/token with HTTP Basic -> Token, sent as
X-Auth-Token. Tokens live ~60 min; we refresh proactively at 55 min and on a
401 exactly once, serialized behind an async lock. All requests share a global
5-connection semaphore (CCC rate limit).
"""

import asyncio
import time
from types import TracebackType
from typing import Any

import httpx

from app.clients.base import DEFAULT_TIMEOUT, get_with_retries
from app.errors import CatalystAuthError, CatalystError

TOKEN_LIFETIME_SECONDS = 55 * 60
MAX_CONCURRENT_REQUESTS = 5
PAGE_SIZE = 50


class CatalystCenterClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        tls_verify: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), verify=tls_verify, timeout=timeout
        )
        self._token: str | None = None
        self._token_fetched_at = 0.0
        self._token_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def __aenter__(self) -> "CatalystCenterClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _fetch_token(self) -> str:
        try:
            response = await self._client.post(
                "/dna/system/api/v1/auth/token",
                auth=(self._username, self._password),
            )
        except httpx.TransportError as exc:
            raise CatalystError(f"Cannot reach Catalyst Center: {exc}") from exc
        if response.status_code in (401, 403):
            raise CatalystAuthError(
                "Catalyst Center rejected the credentials (HTTP "
                f"{response.status_code}). Check username/password in Settings."
            )
        if response.status_code != 200:
            raise CatalystError(
                f"Catalyst Center token request failed with HTTP {response.status_code}."
            )
        token = response.json().get("Token")
        if not token or not isinstance(token, str):
            raise CatalystError("Catalyst Center token response did not contain a Token.")
        return token

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            expired = time.monotonic() - self._token_fetched_at > TOKEN_LIFETIME_SECONDS
            if self._token is None or expired or force_refresh:
                self._token = await self._fetch_token()
                self._token_fetched_at = time.monotonic()
            return self._token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Authenticated GET with retry, plus 401-refresh-retry exactly once."""
        token = await self._get_token()
        async with self._semaphore:
            response = await get_with_retries(
                self._client, path, headers={"X-Auth-Token": token}, params=params
            )
        if response.status_code == 401:
            token = await self._get_token(force_refresh=True)
            async with self._semaphore:
                response = await get_with_retries(
                    self._client, path, headers={"X-Auth-Token": token}, params=params
                )
            if response.status_code == 401:
                raise CatalystAuthError(
                    "Catalyst Center returned 401 even after a token refresh. "
                    "Check the credentials in Settings."
                )
        if response.status_code >= 400:
            raise CatalystError(
                f"Catalyst Center GET {path} failed with HTTP {response.status_code}."
            )
        return response

    async def _get_paginated(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Collect all pages of an offset/limit-paginated list endpoint."""
        items: list[dict[str, Any]] = []
        offset = 1  # CCC list endpoints are 1-based
        while True:
            page_params: dict[str, Any] = dict(params or {})
            page_params.update({"limit": PAGE_SIZE, "offset": offset})
            response = await self._get(path, params=page_params)
            payload = response.json()
            page = payload.get("response", payload)
            if not isinstance(page, list):
                raise CatalystError(f"Unexpected response shape from Catalyst Center {path}.")
            items.extend(page)
            if len(page) < PAGE_SIZE:
                return items
            offset += PAGE_SIZE

    async def test_connection(self) -> int:
        """Fetch a token and count sites; returns the site count."""
        await self._get_token(force_refresh=True)
        return len(await self.get_sites())

    async def get_sites(self) -> list[dict[str, Any]]:
        return await self._get_paginated("/dna/intent/api/v1/site")

    async def get_pnp_devices(self, state: str = "Unclaimed") -> list[dict[str, Any]]:
        return await self._get_paginated(
            "/dna/intent/api/v1/onboarding/pnp-device", params={"state": state}
        )
