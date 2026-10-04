"""Close out job phases that a restart interrupted.

Day-0/Day-N run as in-process background tasks. When the process stops mid-run
(container restart, image update, crash) the task is gone, but the job row still
says `*_running`: the UI would poll forever and delete/claim/deploy would answer
409 until someone edits the database. At startup nothing can be running yet, so
every such job is finished here with an actionable per-device error.
"""

import logging

from sqlalchemy import select

from app.db.models import Job, WebhookDelivery
from app.db.session import open_session
from app.services.day0 import _finish_job
from app.services.dayn import _finish

logger = logging.getLogger(__name__)

DAY0_IN_FLIGHT = ("queued", "claiming", "provisioning")
DAYN_IN_FLIGHT = ("dayn_queued", "dayn_deploying")

DAY0_INTERRUPTED = (
    "Interrupted: the application restarted while this device was being claimed. "
    "Check its PnP state in Catalyst Center before claiming it again."
)
DAYN_INTERRUPTED = (
    "Interrupted: the application restarted during the Day-N deploy. Check the device "
    "configuration in Catalyst Center before deploying again; NetBox was not changed."
)


def recover_interrupted_jobs() -> list[int]:
    """Finish every `*_running` job left over from before this start; returns their ids."""
    interrupted: list[tuple[int, str]] = []
    with open_session() as db:
        jobs = db.scalars(select(Job).where(Job.status.like("%\\_running", escape="\\"))).all()
        for job in jobs:
            interrupted.append((job.id, job.status))
            for device in job.devices:
                if device.state in DAY0_IN_FLIGHT:
                    device.state = "failed"
                    device.error = DAY0_INTERRUPTED
                elif device.state in DAYN_IN_FLIGHT:
                    device.state = "dayn_failed"
                    device.error = DAYN_INTERRUPTED

    for job_id, status in interrupted:
        if status == "day0_running":
            _finish_job(job_id)
        else:
            _finish(job_id)
        logger.warning(
            "Job %d was interrupted by an application restart (%s); in-flight devices were "
            "marked failed and can be retried",
            job_id,
            status,
            extra={"job_id": job_id},
        )
    _release_interrupted_webhook_retries()
    return [job_id for job_id, _ in interrupted]


def _release_interrupted_webhook_retries() -> None:
    """A retry marks its delivery `retrying` while it sends; a restart in that
    window would hide the delivery from the Logs page for good."""
    with open_session() as db:
        stuck = db.scalars(select(WebhookDelivery).where(WebhookDelivery.status == "retrying"))
        for delivery in stuck.all():
            delivery.status = "failed"
            delivery.last_error = "Retry interrupted by an application restart - retry again."
            logger.warning(
                "Webhook delivery %d was being retried during a restart; marked failed",
                delivery.id,
                extra={"job_id": delivery.job_id},
            )
