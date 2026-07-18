"""Mock Catalyst Center 2.3.7 Intent API.

Faithful to the live quirks the real clients depend on: the PnP list is a
bare JSON array with 0-based offsets, `/site` wraps in `{"response": []}`
with 1-based offsets, deploy/v2 returns a task, and task errors can carry
an empty `failureReason` that only the task tree explains.
"""

from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from tests.mocks.state import (
    CCC_SITE_ID,
    CCC_SITE_NAME,
    DAY0_TEMPLATE_ID,
    DAYN_TEMPLATE_ID,
    DAYN_VARIABLES,
    STATE,
)


def _check_token(request: Request) -> None:
    if not request.headers.get("X-Auth-Token"):
        raise HTTPException(status_code=401, detail="No X-Auth-Token")


def create_ccc_app() -> FastAPI:
    app = FastAPI(title="mock-ccc")

    @app.middleware("http")
    async def track_concurrency(request: Request, call_next: Any) -> Response:
        STATE.ccc_requests += 1
        STATE.ccc_in_flight += 1
        STATE.ccc_max_in_flight = max(STATE.ccc_max_in_flight, STATE.ccc_in_flight)
        try:
            if request.method == "GET" and STATE.fail_next_ccc_gets > 0:
                STATE.fail_next_ccc_gets -= 1
                return Response(status_code=503, content="mock: injected 503")
            return await call_next(request)  # type: ignore[no-any-return]
        finally:
            STATE.ccc_in_flight -= 1

    @app.post("/dna/system/api/v1/auth/token")
    def token(request: Request) -> dict[str, str]:
        if STATE.auth_fail or "authorization" not in request.headers:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        STATE.token_counter += 1
        return {"Token": f"mock-token-{STATE.token_counter}"}

    @app.get("/dna/intent/api/v1/site")
    def sites(request: Request, offset: int = 1, limit: int = 50) -> dict[str, Any]:
        _check_token(request)
        all_sites = [
            {"id": "uuid-global", "siteNameHierarchy": "Global"},
            {"id": CCC_SITE_ID, "siteNameHierarchy": CCC_SITE_NAME},
        ]
        return {"response": all_sites[offset - 1 : offset - 1 + limit]}

    @app.get("/dna/intent/api/v1/onboarding/pnp-device")
    def pnp_list(
        request: Request, state: str | None = None, offset: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        _check_token(request)
        devices = [
            d
            for d in STATE.pnp_devices.values()
            if state is None or d["deviceInfo"]["state"] == state
        ]
        return devices[offset : offset + limit]

    @app.get("/dna/intent/api/v1/onboarding/pnp-device/{device_id}")
    def pnp_device(device_id: str, request: Request) -> dict[str, Any]:
        _check_token(request)
        device = STATE.pnp_devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Unknown PnP device")
        info = device["deviceInfo"]
        serial = info["serialNumber"]
        if serial in STATE.provision_polls:
            if serial in STATE.fail_onboarding_serials:
                info["state"] = "Error"
                info["errorMessage"] = "mock: onboarding failed on device"
            elif STATE.provision_polls[serial] <= 0:
                info["state"] = "Provisioned"
            else:
                STATE.provision_polls[serial] -= 1
                info["state"] = "Provisioning"
        return device

    @app.post("/dna/intent/api/v1/onboarding/pnp-device/site-claim")
    async def site_claim(request: Request) -> dict[str, Any]:
        _check_token(request)
        payload = await request.json()
        for key in ("deviceId", "siteId", "type", "configInfo"):
            if key not in payload:
                raise HTTPException(status_code=400, detail=f"site-claim missing '{key}'")
        device = STATE.pnp_devices.get(payload["deviceId"])
        if device is None:
            raise HTTPException(status_code=404, detail="Unknown PnP device")
        STATE.claims.append(payload)
        serial = device["deviceInfo"]["serialNumber"]
        STATE.provision_polls[serial] = STATE.claim_polls
        device["deviceInfo"]["state"] = "Planned"
        return {"response": "Claimed", "version": "1.0"}

    @app.get("/dna/intent/api/v1/template-programmer/template")
    def templates(request: Request) -> list[dict[str, Any]]:
        _check_token(request)
        return [
            {"templateId": DAY0_TEMPLATE_ID, "name": "Day0 Onboarding", "projectName": "PnP"},
            {"templateId": DAYN_TEMPLATE_ID, "name": "DayN Baseline", "projectName": "Baseline"},
        ]

    @app.get("/dna/intent/api/v1/template-programmer/template/{template_id}")
    def template_detail(template_id: str, request: Request) -> dict[str, Any]:
        _check_token(request)
        if template_id == DAYN_TEMPLATE_ID:
            params = [{"parameterName": name} for name in DAYN_VARIABLES]
        else:
            params = [{"parameterName": "HOSTNAME"}]
        return {"id": template_id, "templateParams": params}

    @app.post("/dna/intent/api/v1/template-programmer/template/deploy/v2")
    async def deploy(request: Request) -> dict[str, Any]:
        _check_token(request)
        payload = await request.json()
        if not payload.get("templateId") or not payload.get("targetInfo"):
            raise HTTPException(status_code=400, detail="deploy/v2 missing templateId/targetInfo")
        STATE.task_counter += 1
        task_id = f"task-{STATE.task_counter}"
        STATE.tasks[task_id] = {"polls": STATE.task_polls, "fail": STATE.dayn_task_fail}
        return {"response": {"taskId": task_id}, "version": "1.0"}

    @app.get("/dna/intent/api/v1/task/{task_id}")
    def task(task_id: str, request: Request) -> dict[str, Any]:
        _check_token(request)
        entry = STATE.tasks.get(task_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Unknown task")
        if entry["polls"] > 0:
            entry["polls"] -= 1
            return {"response": {"taskId": task_id, "progress": "in progress"}}
        if entry["fail"]:
            # failureReason intentionally empty: the reason lives in the tree
            return {
                "response": {"taskId": task_id, "isError": True, "failureReason": "", "endTime": 1}
            }
        return {"response": {"taskId": task_id, "isError": False, "endTime": 1}}

    @app.get("/dna/intent/api/v1/task/{task_id}/tree")
    def task_tree(task_id: str, request: Request) -> dict[str, Any]:
        _check_token(request)
        return {
            "response": [
                {
                    "taskId": f"{task_id}-child",
                    "isError": True,
                    "failureReason": "mock: config apply failed on device",
                },
            ]
        }

    return app
