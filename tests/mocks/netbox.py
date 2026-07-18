"""Mock NetBox 4.x REST API (token auth, paginated results envelopes)."""

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from tests.mocks.state import NETBOX_SITE_ID, NETBOX_SITE_NAME, STATE


def _check_token(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Token "):
        raise HTTPException(status_code=401, detail="Missing token")


def _page(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(results), "next": None, "previous": None, "results": results}


def create_netbox_app() -> FastAPI:
    app = FastAPI(title="mock-netbox")

    @app.get("/api/status/")
    def status(request: Request) -> dict[str, Any]:
        _check_token(request)
        return {"netbox-version": "4.2-mock"}

    @app.get("/api/dcim/devices/")
    def devices(
        request: Request, serial: str | None = None, status: str | None = None
    ) -> dict[str, Any]:
        _check_token(request)
        rows = [
            d
            for d in STATE.netbox_devices.values()
            if (serial is None or d["serial"] == serial)
            and (status is None or d["status"]["value"] == status)
        ]
        return _page(rows)

    @app.get("/api/dcim/devices/{device_id}/")
    def device_detail(device_id: int, request: Request) -> dict[str, Any]:
        _check_token(request)
        device = STATE.netbox_devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Not found")
        return device

    @app.patch("/api/dcim/devices/{device_id}/")
    async def patch_device(device_id: int, request: Request) -> dict[str, Any]:
        _check_token(request)
        if STATE.netbox_patch_fail:
            raise HTTPException(status_code=500, detail="mock: injected PATCH failure")
        device = STATE.netbox_devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Not found")
        payload = await request.json()
        if "status" in payload:
            device["status"] = {"value": payload["status"]}
        return device

    @app.get("/api/dcim/sites/")
    def sites(request: Request) -> dict[str, Any]:
        _check_token(request)
        return _page([{"id": NETBOX_SITE_ID, "name": NETBOX_SITE_NAME, "slug": "ffm-dc1"}])

    @app.get("/api/ipam/vlans/")
    def vlans(request: Request, site_id: int | None = None) -> dict[str, Any]:
        _check_token(request)
        if site_id is not None and site_id != NETBOX_SITE_ID:
            return _page([])
        return _page(
            [
                {"id": 5, "vid": 110, "name": "MGMT"},
                {"id": 6, "vid": 120, "name": "USERS"},
            ]
        )

    @app.get("/api/ipam/ip-addresses/")
    def ip_addresses(request: Request, device_id: int | None = None) -> dict[str, Any]:
        _check_token(request)
        device = STATE.netbox_devices.get(device_id or 0)
        if device is None or device.get("primary_ip4"):
            return _page([])
        return _page(
            [
                {
                    "id": 9000 + (device_id or 0),
                    "address": f"172.20.11.{(device_id or 0) % 250}/24",
                    "assigned_object": {"name": "Vlan110"},
                }
            ]
        )

    return app
