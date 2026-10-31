"""
APScheduler-based periodic sync.

Runs two scheduled tasks:

1. ``periodic_full_sync`` (every 15 minutes): Re-syncs all active deals in
   ``deal_links`` that have Zoho resources, catching any events missed by
   webhooks.

2. ``reset_zoho_daily_counter`` (daily at 00:01 UTC): Resets the in-memory
   Zoho API call counter so the new day's quota starts clean.

The scheduler is started during FastAPI application startup and shut down
during shutdown.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.database import SessionLocal
from app.models.link import DealLink
from app.queue import enqueue
from app.models.job import JobType
from app.zoho.client import reset_daily_counter

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Return the global scheduler, creating it if necessary."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def start_scheduler() -> None:
    """
    Configure and start all scheduled jobs.

    Should be called once during FastAPI ``startup`` event.
    """
    sched = get_scheduler()

    # ── Periodic sync every 15 minutes ────────────────────────────────────────
    sched.add_job(
        _enqueue_periodic_syncs,
        trigger=IntervalTrigger(minutes=15),
        id="periodic_sync",
        replace_existing=True,
        name="Periodic deal sync",
    )

    # ── Reset Zoho daily counter at 00:01 UTC ─────────────────────────────────
    sched.add_job(
        reset_daily_counter,
        trigger=CronTrigger(hour=0, minute=1, timezone="UTC"),
        id="reset_zoho_daily_counter",
        replace_existing=True,
        name="Reset Zoho daily API counter",
    )

    sched.start()
    logger.info("APScheduler started with %d jobs.", len(sched.get_jobs()))


def stop_scheduler() -> None:
    """
    Shut down the scheduler.

    Should be called during FastAPI ``shutdown`` event.
    """
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
    _scheduler = None


def _enqueue_periodic_syncs() -> None:
    """
    Background task: enqueue a ZOHO_TO_CRM sync job for every active deal.

    A deal is considered active if it has a ``zoho_so_id`` (i.e. it has been
    provisioned in Zoho Books).  Deals with a null SO ID have not yet been
    processed and should not be polled.
    """
    db = SessionLocal()
    try:
        active_links = (
            db.query(DealLink)
            .filter(DealLink.zoho_so_id.isnot(None))
            .all()
        )
        count = 0
        for link in active_links:
            enqueue(
                db,
                deal_id=link.hs_deal_id,
                job_type=JobType.ZOHO_TO_CRM,
                metadata={"event_type": "scheduler"},
                deduplicate=True,
            )
            count += 1
        logger.info("Periodic sync: enqueued %d jobs.", count)
    except Exception as exc:
        logger.error("Periodic sync enqueue failed: %s", exc)
    finally:
        db.close()

