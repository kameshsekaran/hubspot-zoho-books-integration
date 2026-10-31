"""
Async background worker.

Polls the job queue and dispatches each job to the appropriate handler.

The worker runs as a ``asyncio`` background task started during FastAPI
startup.  It uses a simple poll-sleep loop (100 ms between empty-queue polls)
rather than a real message broker to avoid external dependencies.

Jobs:
    - ``crm_to_zoho``: Process a Closed Won deal → create Zoho resources.
    - ``zoho_to_crm``: Process a Zoho Books event → update HubSpot metrics.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from app.database import SessionLocal
from app.models.job import IntegrationJob, JobStatus, JobType
from app.queue import dequeue_next, mark_done, mark_failed, mark_skipped
from app.sync_engine import (
    handle_deal_reopened,
    sync_closed_won_deal,
    sync_zoho_event,
)

logger = logging.getLogger(__name__)

# Flag used to signal the worker loop to stop gracefully.
_stop_event: Optional[asyncio.Event] = None


async def run_worker(stop_event: Optional[asyncio.Event] = None) -> None:
    """
    Main worker loop.

    Continuously dequeues and processes jobs until ``stop_event`` is set.

    Args:
        stop_event: asyncio.Event that signals the loop to exit gracefully.
    """
    global _stop_event
    _stop_event = stop_event or asyncio.Event()
    logger.info("Job worker started.")

    while not _stop_event.is_set():
        db = SessionLocal()
        job: Optional[IntegrationJob] = None
        try:
            job = dequeue_next(db)
            if job is None:
                # Nothing to do — sleep briefly before polling again.
                db.close()
                await asyncio.sleep(0.1)
                continue

            logger.info(
                "Processing job id=%s deal_id=%s type=%s attempt=%s",
                job.id,
                job.deal_id,
                job.job_type,
                job.retry_count + 1,
            )
            await _dispatch(job, db)
            mark_done(db, job)

        except Exception as exc:
            logger.exception("Job id=%s failed: %s", job.id if job else "?", exc)
            if job is not None:
                mark_failed(db, job, str(exc))
        finally:
            db.close()

    logger.info("Job worker stopped.")


async def _dispatch(job: IntegrationJob, db) -> None:
    """
    Dispatch a job to the appropriate handler.

    Runs synchronous handlers in the default thread pool executor to avoid
    blocking the event loop.

    Args:
        job: The claimed job.
        db:  Open database session.
    """
    loop = asyncio.get_running_loop()
    metadata: dict = {}
    if job.metadata_json:
        try:
            metadata = json.loads(job.metadata_json)
        except (ValueError, TypeError):
            pass

    job_type = job.job_type

    if job_type == JobType.CRM_TO_ZOHO.value:
        event = metadata.get("event", "deal.propertyChange")
        new_value = metadata.get("new_value", "")

        if new_value == "closedwon" or not new_value:
            # Closed Won: full provisioning
            await loop.run_in_executor(
                None, sync_closed_won_deal, job.deal_id, db
            )
        else:
            # Deal re-opened (stage changed away from closedwon)
            await loop.run_in_executor(
                None, handle_deal_reopened, job.deal_id, db
            )

    elif job_type == JobType.ZOHO_TO_CRM.value:
        invoice_id = metadata.get("invoice_id")
        payment_id = metadata.get("payment_id")
        so_id = metadata.get("so_id")
        event_type = metadata.get("event_type", "unknown")

        await loop.run_in_executor(
            None,
            _sync_zoho_event_sync,
            db,
            invoice_id,
            payment_id,
            so_id,
            event_type,
        )

    else:
        logger.warning("Unknown job type %r – skipping.", job_type)
        mark_skipped(db, job, f"Unknown job type: {job_type}")


def _sync_zoho_event_sync(db, invoice_id, payment_id, so_id, event_type):
    """Thin sync wrapper for sync_zoho_event to run in executor."""
    return sync_zoho_event(
        db,
        invoice_id=invoice_id,
        payment_id=payment_id,
        so_id=so_id,
        event_type=event_type,
    )


async def stop_worker() -> None:
    """Signal the worker to stop after its current job completes."""
    global _stop_event
    if _stop_event:
        _stop_event.set()
        logger.info("Worker stop signal sent.")

