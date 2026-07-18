"""Combined mock stack: CCC under /ccc, NetBox under /netbox, ISE under /ise.

Control API (used by integration tests, Playwright e2e, and manual demos):

    GET  /__mock__/health          liveness
    POST /__mock__/reset           reseed; body {"devices": N} (default 2)
    POST /__mock__/config          set failure-injection knobs (see state.MockState)
    GET  /__mock__/state           claims, deliveries, NetBox statuses, stats

Standalone:  python -m tests.mocks.stack --port 9100
"""

import argparse
from dataclasses import fields
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from tests.mocks import state as state_module
from tests.mocks.ccc import create_ccc_app
from tests.mocks.ise import create_ise_app
from tests.mocks.netbox import create_netbox_app

STATE = state_module.STATE

_KNOBS = {
    "auth_fail",
    "fail_next_ccc_gets",
    "fail_onboarding_serials",
    "dayn_task_fail",
    "netbox_patch_fail",
    "ise_fail",
    "claim_polls",
    "task_polls",
}


def create_stack() -> FastAPI:
    app = FastAPI(title="pnp-bridge-mock-stack")
    app.mount("/ccc", create_ccc_app())
    app.mount("/netbox", create_netbox_app())
    app.mount("/ise", create_ise_app())

    @app.get("/__mock__/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/__mock__/reset")
    async def reset(request: Request) -> dict[str, str]:
        body: dict[str, Any] = {}
        if int(request.headers.get("content-length") or 0) > 0:
            body = await request.json()
        defaults = state_module.MockState()
        for f in fields(state_module.MockState):
            setattr(STATE, f.name, getattr(defaults, f.name))
        state_module.seed(STATE, devices=int(body.get("devices", 2)))
        return {"status": "reset"}

    @app.post("/__mock__/config")
    async def config(request: Request) -> dict[str, str]:
        for key, value in (await request.json()).items():
            if key not in _KNOBS:
                raise HTTPException(status_code=400, detail=f"Unknown knob '{key}'")
            setattr(STATE, key, value)
        return {"status": "configured"}

    @app.get("/__mock__/state")
    def snapshot() -> dict[str, Any]:
        return {
            "claims": STATE.claims,
            "deliveries": STATE.deliveries,
            "netbox_statuses": {
                str(device_id): device["status"]["value"]
                for device_id, device in STATE.netbox_devices.items()
            },
            "stats": {
                "ccc_requests": STATE.ccc_requests,
                "ccc_max_in_flight": STATE.ccc_max_in_flight,
                "token_fetches": STATE.token_counter,
            },
        }

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="PnP Bridge mock CCC/NetBox/ISE stack")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(create_stack(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
