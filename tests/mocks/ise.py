"""Mock ISE helper: records webhook deliveries incl. the HMAC signature."""

from typing import Any

from fastapi import FastAPI, HTTPException, Request


def create_ise_app() -> FastAPI:
    from tests.mocks.state import STATE

    app = FastAPI(title="mock-ise")

    @app.post("/hook")
    async def hook(request: Request) -> dict[str, str]:
        if STATE.ise_fail:
            raise HTTPException(status_code=500, detail="mock: ISE helper down")
        STATE.deliveries.append(
            {
                "payload": await request.json(),
                "signature": request.headers.get("X-PnPB-Signature"),
            }
        )
        return {"status": "accepted"}

    return app


def deliveries() -> list[dict[str, Any]]:
    from tests.mocks.state import STATE

    return STATE.deliveries
