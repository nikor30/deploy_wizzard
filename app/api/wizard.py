"""Wizard API: PnP devices, job lifecycle, matching (steps 1-2), Day-0 claim (step 3)."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, JobDevice, SiteMapping
from app.db.session import get_db, open_session
from app.services.connections import get_catalyst_client, get_netbox_client
from app.services.day0 import run_day0
from app.services.matching import MATCHED, SiteMappingLookup, match_serials

router = APIRouter(prefix="/api/wizard", tags=["wizard"])
logger = logging.getLogger(__name__)

DbSession = Annotated[Session, Depends(get_db)]


class PnpDevice(BaseModel):
    ccc_device_id: str
    serial: str
    pid: str | None = None
    state: str | None = None
    ip_address: str | None = None
    last_contact: str | None = None


class JobDeviceIn(BaseModel):
    serial: str
    pid: str | None = None
    ccc_device_id: str


class JobCreate(BaseModel):
    devices: list[JobDeviceIn]


class JobDeviceOut(BaseModel):
    id: int
    serial: str
    pid: str | None
    ccc_device_id: str
    match_status: str | None
    netbox_device_id: int | None
    netbox_name: str | None
    netbox_site_id: int | None
    netbox_site_name: str | None
    ccc_site_id: str | None
    ccc_site_name: str | None
    mgmt_ip: str | None
    mgmt_vlan: int | None
    vlan_options: list[dict[str, Any]]
    state: str
    error: str | None


class JobOut(BaseModel):
    id: int
    status: str
    current_step: int
    created_at: str
    device_count: int
    devices: list[JobDeviceOut]


class DeviceUpdate(BaseModel):
    mgmt_vlan: int | None = None


def _device_out(device: JobDevice) -> JobDeviceOut:
    return JobDeviceOut(
        id=device.id,
        serial=device.serial,
        pid=device.pid,
        ccc_device_id=device.ccc_device_id,
        match_status=device.match_status,
        netbox_device_id=device.netbox_device_id,
        netbox_name=device.netbox_name,
        netbox_site_id=device.netbox_site_id,
        netbox_site_name=device.netbox_site_name,
        ccc_site_id=device.ccc_site_id,
        ccc_site_name=device.ccc_site_name,
        mgmt_ip=device.mgmt_ip,
        mgmt_vlan=device.mgmt_vlan,
        vlan_options=device.vlan_options or [],
        state=device.state,
        error=device.error,
    )


def _job_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        status=job.status,
        current_step=job.current_step,
        created_at=job.created_at.isoformat(),
        device_count=len(job.devices),
        devices=[_device_out(d) for d in job.devices],
    )


def _get_job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
    return job


@router.get("/pnp-devices")
async def list_pnp_devices(db: DbSession) -> list[PnpDevice]:
    """All Catalyst Center PnP devices in state Unclaimed."""
    async with get_catalyst_client(db) as client:
        devices = await client.get_pnp_devices(state="Unclaimed")
    result: list[PnpDevice] = []
    for entry in devices:
        info = entry.get("deviceInfo") or {}
        serial = info.get("serialNumber")
        if not serial:
            continue
        result.append(
            PnpDevice(
                ccc_device_id=str(entry.get("id")),
                serial=serial,
                pid=info.get("pid"),
                state=info.get("state"),
                ip_address=info.get("ipAddress"),
                last_contact=str(info.get("lastContact")) if info.get("lastContact") else None,
            )
        )
    return result


@router.get("/jobs")
def list_jobs(db: DbSession) -> list[JobOut]:
    jobs = db.scalars(select(Job).order_by(Job.id.desc())).all()
    return [_job_out(job) for job in jobs]


@router.post("/jobs", status_code=201)
def create_job(payload: JobCreate, db: DbSession) -> JobOut:
    if not payload.devices:
        raise HTTPException(status_code=422, detail="A job needs at least one device.")
    job = Job()
    for device in payload.devices:
        job.devices.append(
            JobDevice(serial=device.serial, pid=device.pid, ccc_device_id=device.ccc_device_id)
        )
    db.add(job)
    db.flush()
    logger.info("Created job", extra={"job_id": job.id, "devices": len(job.devices)})
    return _job_out(job)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: DbSession) -> JobOut:
    return _job_out(_get_job(db, job_id))


@router.post("/jobs/{job_id}/match")
async def match_job(job_id: int, db: DbSession) -> JobOut:
    """Run NetBox matching for all devices of the job and persist the results."""
    job = _get_job(db, job_id)
    mappings: SiteMappingLookup = {
        m.netbox_site_id: (m.ccc_site_id, m.ccc_site_name)
        for m in db.scalars(select(SiteMapping)).all()
    }
    async with get_netbox_client(db) as netbox:
        results = await match_serials([d.serial for d in job.devices], netbox, mappings)
    by_serial = {r.serial: r for r in results}
    for device in job.devices:
        result = by_serial[device.serial]
        device.match_status = result.match_status
        device.netbox_device_id = result.netbox_device_id
        device.netbox_name = result.netbox_name
        device.netbox_site_id = result.netbox_site_id
        device.netbox_site_name = result.netbox_site_name
        device.ccc_site_id = result.ccc_site_id
        device.ccc_site_name = result.ccc_site_name
        device.mgmt_ip = result.mgmt_ip
        device.vlan_options = result.vlan_options
        if device.mgmt_vlan is not None and not any(
            option.get("vid") == device.mgmt_vlan for option in result.vlan_options
        ):
            device.mgmt_vlan = None
    job.current_step = 2
    db.flush()
    return _job_out(job)


class Day0Template(BaseModel):
    id: str
    name: str
    project: str | None = None


class ClaimRequest(BaseModel):
    config_id: str
    image_id: str | None = None
    poll_interval: float | None = None
    timeout: float | None = None


@router.get("/day0/templates")
async def list_day0_templates(db: DbSession) -> list[Day0Template]:
    async with get_catalyst_client(db) as client:
        templates = await client.get_templates()
    result: list[Day0Template] = []
    for template in templates:
        template_id = template.get("templateId") or template.get("id")
        if not template_id:
            continue
        result.append(
            Day0Template(
                id=str(template_id),
                name=str(template.get("name", template_id)),
                project=template.get("projectName"),
            )
        )
    return result


@router.post("/jobs/{job_id}/claim")
def claim_job(
    job_id: int, payload: ClaimRequest, background: BackgroundTasks, db: DbSession
) -> JobOut:
    """Start Day-0 claiming for all matched devices (runs in the background)."""
    job = _get_job(db, job_id)
    if job.status == "day0_running":
        raise HTTPException(status_code=409, detail="Day-0 is already running for this job.")
    claimable = [d for d in job.devices if d.match_status == MATCHED]
    if not claimable:
        raise HTTPException(status_code=422, detail="No matched devices to claim.")
    job.status = "day0_running"
    job.current_step = 3
    for device in claimable:
        device.state = "queued"
        device.error = None
    # Commit now: the background task opens its own sessions and must not
    # contend with this request's still-open write transaction.
    db.commit()
    kwargs: dict[str, Any] = {}
    if payload.poll_interval is not None:
        kwargs["poll_interval"] = payload.poll_interval
    if payload.timeout is not None:
        kwargs["device_timeout"] = payload.timeout
    background.add_task(
        run_day0, job_id, config_id=payload.config_id, image_id=payload.image_id, **kwargs
    )
    logger.info("Day-0 started", extra={"job_id": job_id, "devices": len(claimable)})
    return _job_out(job)


def _job_snapshot(job_id: int) -> dict[str, Any] | None:
    with open_session() as db:
        job = db.get(Job, job_id)
        if job is None:
            return None
        return _job_out(job).model_dump()


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: int, db: DbSession) -> StreamingResponse:
    """SSE stream of job snapshots while Day-0/Day-N is running."""
    _get_job(db, job_id)

    async def stream() -> AsyncIterator[str]:
        while True:
            snapshot = _job_snapshot(job_id)
            if snapshot is None:
                return
            yield f"data: {json.dumps(snapshot)}\n\n"
            if not snapshot["status"].endswith("_running"):
                return
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.put("/jobs/{job_id}/devices/{device_id}")
def update_job_device(
    job_id: int, device_id: int, payload: DeviceUpdate, db: DbSession
) -> JobDeviceOut:
    job = _get_job(db, job_id)
    device = next((d for d in job.devices if d.id == device_id), None)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not in job {job_id}.")
    if payload.mgmt_vlan is not None:
        if device.match_status != MATCHED:
            raise HTTPException(status_code=422, detail="Only matched devices can get a mgmt VLAN.")
        if not any(option.get("vid") == payload.mgmt_vlan for option in device.vlan_options or []):
            raise HTTPException(
                status_code=422,
                detail=f"VLAN {payload.mgmt_vlan} is not available at site "
                f"{device.netbox_site_name}.",
            )
    device.mgmt_vlan = payload.mgmt_vlan
    db.flush()
    return _device_out(device)
