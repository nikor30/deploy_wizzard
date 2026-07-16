from fastapi import APIRouter
from pydantic import BaseModel

import app as app_pkg

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str


@router.get("/api/health")
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=app_pkg.__version__)
