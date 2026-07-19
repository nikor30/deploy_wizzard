"""Site mapping API: persisted NetBox↔CCC pairs + live source site lists."""

import logging
from typing import Annotated, Any

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
    # optional NetBox location (building/floor …) below the site
    netbox_location_id: int | None = None
    netbox_location_name: str | None = None
    ccc_site_id: str
    ccc_site_name: str


class SiteMappingList(BaseModel):
    mappings: list[SiteMappingItem]


class NetBoxSite(BaseModel):
    """A mappable NetBox target: a site or a location below a site.
    `name` is the full path, e.g. "FFM-DC1 / Building A / Floor 2"."""

    site_id: int
    location_id: int | None = None
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
                netbox_location_id=row.netbox_location_id,
                netbox_location_name=row.netbox_location_name,
                ccc_site_id=row.ccc_site_id,
                ccc_site_name=row.ccc_site_name,
            )
            for row in rows
        ]
    )


@router.put("/sites")
def put_site_mappings(payload: SiteMappingList, db: DbSession) -> SiteMappingList:
    """Replace the full mapping table (used by the editor and JSON import)."""
    seen: set[tuple[int, int | None]] = set()
    for item in payload.mappings:
        key = (item.netbox_site_id, item.netbox_location_id)
        if key in seen:
            raise HTTPException(
                status_code=422,
                detail=f"Duplicate mapping for NetBox site/location {key}.",
            )
        seen.add(key)
    db.execute(delete(SiteMapping))
    for item in payload.mappings:
        db.add(
            SiteMapping(
                netbox_site_id=item.netbox_site_id,
                netbox_site_name=item.netbox_site_name,
                netbox_location_id=item.netbox_location_id,
                netbox_location_name=item.netbox_location_name,
                ccc_site_id=item.ccc_site_id,
                ccc_site_name=item.ccc_site_name,
            )
        )
    db.flush()
    logger.info("Stored %d site mappings", len(payload.mappings))
    return get_site_mappings(db)


def _location_paths(locations: list[dict[str, Any]]) -> dict[int, str]:
    """location_id -> "Loc / SubLoc" path (without the site prefix)."""
    by_id = {loc["id"]: loc for loc in locations}
    paths: dict[int, str] = {}

    def path(location_id: int, depth: int = 0) -> str:
        if location_id in paths or depth > 20:
            return paths.get(location_id, "")
        loc = by_id[location_id]
        parent_id: int | None = (loc.get("parent") or {}).get("id")
        prefix = ""
        if parent_id is not None and parent_id in by_id:
            prefix = f"{path(parent_id, depth + 1)} / "
        paths[location_id] = f"{prefix}{loc.get('name', '')}"
        return paths[location_id]

    for location_id in by_id:
        path(location_id)
    return paths


@router.get("/sources/netbox")
async def get_netbox_sites(db: DbSession) -> list[NetBoxSite]:
    """Mappable NetBox targets: every site plus its location tree
    (buildings, floors, …) as path entries."""
    async with get_netbox_client(db) as client:
        sites = await client.get_sites()
        locations = await client.get_locations()
    entries = [
        NetBoxSite(site_id=site["id"], name=site["name"], slug=site.get("slug")) for site in sites
    ]
    site_names = {site["id"]: site["name"] for site in sites}
    location_paths = _location_paths(locations)
    for loc in locations:
        site_id = (loc.get("site") or {}).get("id")
        if site_id is None:
            continue
        entries.append(
            NetBoxSite(
                site_id=site_id,
                location_id=loc["id"],
                name=f"{site_names.get(site_id, '?')} / {location_paths[loc['id']]}",
                slug=loc.get("slug"),
            )
        )
    return sorted(entries, key=lambda e: e.name)


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
    mapped_keys = {
        (m.netbox_site_id, m.netbox_location_id) for m in db.scalars(select(SiteMapping)).all()
    }
    candidates = [
        entry
        for entry in await get_netbox_sites(db)
        if (entry.site_id, entry.location_id) not in mapped_keys
    ]
    async with get_catalyst_client(db) as catalyst:
        ccc_sites = await catalyst.get_sites()

    # candidate ids are list indices; translated back to site/location below
    suggestions = suggest_site_mappings(
        [
            {"id": index, "name": entry.name, "slug": entry.slug}
            for index, entry in enumerate(candidates)
        ],
        [
            {
                "id": s["id"],
                "name_hierarchy": s.get("siteNameHierarchy") or s.get("name", ""),
            }
            for s in ccc_sites
        ],
    )
    logger.info("Suggested %d of %d unmapped sites/locations", len(suggestions), len(candidates))
    result: list[SiteSuggestion] = []
    for item in suggestions:
        entry = candidates[int(item["netbox_site_id"])]
        result.append(
            SiteSuggestion(
                netbox_site_id=entry.site_id,
                netbox_site_name=entry.name,
                netbox_location_id=entry.location_id,
                netbox_location_name=entry.name if entry.location_id is not None else None,
                ccc_site_id=item["ccc_site_id"],
                ccc_site_name=item["ccc_site_name"],
                confidence=item["confidence"],
            )
        )
    return result
