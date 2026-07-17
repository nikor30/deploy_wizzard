"""Statistics dashboard aggregation endpoint."""

from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.services.stats import collect_stats

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
def get_stats(days: Annotated[int, Query(ge=1, le=365)] = 30) -> dict[str, Any]:
    return collect_stats(days)
