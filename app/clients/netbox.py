"""NetBox v4.x REST client (token auth: 'Authorization: Token <key>')."""

from types import TracebackType
from typing import Any

import httpx

from app.clients.base import DEFAULT_TIMEOUT, get_with_retries
from app.errors import NetBoxAuthError, NetBoxError, NetBoxNotFound


class NetBoxClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        tls_verify: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=tls_verify,
            timeout=timeout,
            headers={"Authorization": f"Token {token}"},
        )

    async def __aenter__(self) -> "NetBoxClient":
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

    def _check(self, response: httpx.Response, context: str) -> httpx.Response:
        if response.status_code in (401, 403):
            raise NetBoxAuthError(
                f"NetBox rejected the API token (HTTP {response.status_code}). "
                "Check the token in Settings."
            )
        if response.status_code == 404:
            raise NetBoxNotFound(f"NetBox object not found: {context}.")
        if response.status_code >= 400:
            raise NetBoxError(f"NetBox request failed ({context}): HTTP {response.status_code}.")
        return response

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = await get_with_retries(self._client, path, params=params)
        except httpx.TransportError as exc:
            raise NetBoxError(f"Cannot reach NetBox: {exc}") from exc
        return self._check(response, path)

    async def _get_paginated(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Collect all pages by following `next` links."""
        items: list[dict[str, Any]] = []
        url: str | None = path
        next_params = params
        while url:
            payload = (await self._get(url, params=next_params)).json()
            items.extend(payload.get("results", []))
            url = payload.get("next")
            next_params = None  # the next link already carries the query string
        return items

    async def test_connection(self) -> str:
        """GET /api/status/ and return the NetBox version string."""
        payload = (await self._get("/api/status/")).json()
        version = payload.get("netbox-version")
        if not version:
            raise NetBoxError("NetBox /api/status/ did not return a version.")
        return str(version)

    async def get_device(self, device_id: int) -> dict[str, Any]:
        """Full device object (incl. custom_fields and config_context)."""
        response = await self._get(f"/api/dcim/devices/{device_id}/")
        return dict(response.json())

    async def get_devices(
        self, *, status: str | None = None, serial: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if serial:
            params["serial"] = serial
        return await self._get_paginated("/api/dcim/devices/", params=params)

    async def get_sites(self) -> list[dict[str, Any]]:
        return await self._get_paginated("/api/dcim/sites/")

    async def get_locations(self) -> list[dict[str, Any]]:
        """Location hierarchy (buildings/floors …) below the sites."""
        return await self._get_paginated("/api/dcim/locations/")

    async def get_interfaces(self, device_id: int) -> list[dict[str, Any]]:
        """All interfaces of a device (uplink/port details for Day-N)."""
        return await self._get_paginated("/api/dcim/interfaces/", params={"device_id": device_id})

    async def get_vlans(self, site_id: int) -> list[dict[str, Any]]:
        return await self._get_paginated("/api/ipam/vlans/", params={"site_id": site_id})

    async def get_contact_assignments(
        self, object_type: str, object_id: int, role: str | None = None
    ) -> list[dict[str, Any]]:
        """Contact assignments for a NetBox object (v4.x tenancy). `object_type`
        is e.g. 'dcim.site' or 'dcim.device'; `role` filters by contact-role
        name. Used to derive the Day-N support_contact variable."""
        params: dict[str, Any] = {"object_type": object_type, "object_id": object_id}
        if role:
            params["role"] = role
        return await self._get_paginated("/api/tenancy/contact-assignments/", params=params)

    async def get_ip_addresses(self, device_id: int) -> list[dict[str, Any]]:
        return await self._get_paginated("/api/ipam/ip-addresses/", params={"device_id": device_id})

    async def patch_device_status(self, device_id: int, status: str) -> dict[str, Any]:
        try:
            response = await self._client.patch(
                f"/api/dcim/devices/{device_id}/", json={"status": status}
            )
        except httpx.TransportError as exc:
            raise NetBoxError(f"Cannot reach NetBox: {exc}") from exc
        checked = self._check(response, f"device {device_id}")
        return dict(checked.json())
