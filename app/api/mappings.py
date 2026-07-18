"""Site mapping API: persisted NetBox↔CCC pairs + live source site lists."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import SiteMapping
from app.db.session import get_db
from app.services.connections import get_catalyst_client, get_netbox_client
from app.services.suggest import suggest_site_mappings

router = APIRouter(prefix="/api/mappings", tags=["mappings"])
logger = logging.getLogger(__name__)

DbSession = Annotated[Session, Depends(get_db)]


class SiteMappingItem(BaseModel):
    netbox_site_id: int
    netbox_site_name: str
    ccc_site_id: str
    ccc_site_name: str


class SiteMappingList(BaseModel):
    mappings: list[SiteMappingItem]


class NetBoxSite(BaseModel):
    id: int
    name: str
    slug: str | None = None


class CccSite(BaseModel):
    id: str
    name_hierarchy: str


@router.get("/sites")
def get_site_mappings(db: DbSession) -> SiteMappingList:
    rows = db.scalars(select(SiteMapping).order_by(SiteMapping.netbox_site_name)).all()
    return SiteMappingList(
        mappings=[
            SiteMappingItem(
                netbox_site_id=row.netbox_site_id,
                netbox_site_name=row.netbox_site_name,
                ccc_site_id=row.ccc_site_id,
                ccc_site_name=row.ccc_site_name,
            )
            for row in rows
        ]
    )


@router.put("/sites")
def put_site_mappings(payload: SiteMappingList, db: DbSession) -> SiteMappingList:
    """Replace the full mapping table (used by the editor and JSON import)."""
    seen: set[int] = set()
    for item in payload.mappings:
        if item.netbox_site_id in seen:
            raise HTTPException(
                status_code=422,
                detail=f"Duplicate mapping for NetBox site id {item.netbox_site_id}.",
            )
        seen.add(item.netbox_site_id)
    db.execute(delete(SiteMapping))
    for item in payload.mappings:
        db.add(
            SiteMapping(
                netbox_site_id=item.netbox_site_id,
                netbox_site_name=item.netbox_site_name,
                ccc_site_id=item.ccc_site_id,
                ccc_site_name=item.ccc_site_name,
            )
        )
    db.flush()
    logger.info("Stored %d site mappings", len(payload.mappings))
    return get_site_mappings(db)


@router.get("/sources/netbox")
async def get_netbox_sites(db: DbSession) -> list[NetBoxSite]:
    async with get_netbox_client(db) as client:
        sites = await client.get_sites()
    return [NetBoxSite(id=site["id"], name=site["name"], slug=site.get("slug")) for site in sites]


@router.get("/sources/ccc")
async def get_ccc_sites(db: DbSession) -> list[CccSite]:
    async with get_catalyst_client(db) as client:
        sites = await client.get_sites()
    return [
        CccSite(
            id=site["id"],
            name_hierarchy=site.get("siteNameHierarchy") or site.get("name", ""),
        )
        for site in sites
    ]


class SiteSuggestion(SiteMappingItem):
    confidence: float


@router.get("/sites/suggest")
async def suggest_site_pairs(db: DbSession) -> list[SiteSuggestion]:
    """Pre-match unmapped NetBox sites against the CCC hierarchy.

    Suggestions are review material for the mapping page — nothing is saved
    until the user confirms.
    """
    mapped_ids = set(db.scalars(select(SiteMapping.netbox_site_id)).all())
    async with get_netbox_client(db) as netbox:
        netbox_sites = await netbox.get_sites()
    async with get_catalyst_client(db) as catalyst:
        ccc_sites = await catalyst.get_sites()

    unmapped = [s for s in netbox_sites if s["id"] not in mapped_ids]
    suggestions = suggest_site_mappings(
        [{"id": s["id"], "name": s.get("name", ""), "slug": s.get("slug")} for s in unmapped],
        [
            {
                "id": s["id"],
                "name_hierarchy": s.get("siteNameHierarchy") or s.get("name", ""),
            }
            for s in ccc_sites
        ],
    )
    logger.info("Suggested %d of %d unmapped sites", len(suggestions), len(unmapped))
    return [SiteSuggestion(**s) for s in suggestions]
