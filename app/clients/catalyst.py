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

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        headers = {"X-Auth-Token": token}
        async with self._semaphore:
            if method == "GET":
                # only GETs are idempotent — retry with backoff
                return await get_with_retries(self._client, path, headers=headers, params=params)
            try:
                return await self._client.request(
                    method, path, headers=headers, params=params, json=json
                )
            except httpx.TransportError as exc:
                raise CatalystError(f"Cannot reach Catalyst Center: {exc}") from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Authenticated request with 401-refresh-retry exactly once."""
        token = await self._get_token()
        response = await self._send(method, path, token, params, json)
        if response.status_code == 401:
            token = await self._get_token(force_refresh=True)
            response = await self._send(method, path, token, params, json)
            if response.status_code == 401:
                raise CatalystAuthError(
                    "Catalyst Center returned 401 even after a token refresh. "
                    "Check the credentials in Settings."
                )
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = f" — {body.get('message') or body.get('response') or ''}".rstrip(" —")
            except ValueError:
                pass
            raise CatalystError(
                f"Catalyst Center {method} {path} failed with HTTP {response.status_code}{detail}."
            )
        return response

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

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

    async def get_pnp_device(self, device_id: str) -> dict[str, Any]:
        """Single PnP device — used to poll deviceInfo.state during claiming."""
        response = await self._get(f"/dna/intent/api/v1/onboarding/pnp-device/{device_id}")
        return dict(response.json())

    async def claim_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST site-claim (§6.1). Not retried — claiming is not idempotent."""
        response = await self._request(
            "POST", "/dna/intent/api/v1/onboarding/pnp-device/site-claim", json=payload
        )
        return dict(response.json())

    async def get_templates(self) -> list[dict[str, Any]]:
        """Onboarding/CLI templates from the template programmer."""
        response = await self._get("/dna/intent/api/v1/template-programmer/template")
        body = response.json()
        return list(body) if isinstance(body, list) else list(body.get("response", []))

    async def get_template(self, template_id: str) -> dict[str, Any]:
        """Single template incl. variable definitions (templateParams)."""
        response = await self._get(f"/dna/intent/api/v1/template-programmer/template/{template_id}")
        return dict(response.json())

    async def deploy_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST deploy/v2 (§6.1); returns a task. Not retried (not idempotent)."""
        response = await self._request(
            "POST", "/dna/intent/api/v1/template-programmer/template/deploy/v2", json=payload
        )
        return dict(response.json())

    async def get_task(self, task_id: str) -> dict[str, Any]:
        response = await self._get(f"/dna/intent/api/v1/task/{task_id}")
        body = response.json()
        return dict(body.get("response", body))

    async def get_task_tree(self, task_id: str) -> list[dict[str, Any]]:
        """Child tasks — §11: claim/deploy errors are often buried here."""
        response = await self._get(f"/dna/intent/api/v1/task/{task_id}/tree")
        body = response.json()
        return list(body.get("response", body if isinstance(body, list) else []))
