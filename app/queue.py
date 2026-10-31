"""
Job queue helpers.

Provides:
- ``enqueue``: Insert a new job, deduplicating pending jobs by deal_id + type.
- ``dequeue_next``: Atomically claim the next pending job ready to run.
- ``mark_done``: Mark a job as completed.
- ``mark_failed``: Increment retry count, schedule next attempt with
  exponential back-off, or permanently fail after max retries.
- ``mark_skipped``: Mark a job as skipped (not an error).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.job import IntegrationJob, JobStatus, JobType

logger = logging.getLogger(__name__)

# Maximum exponential back-off (seconds): 2^6 minutes = 64 minutes
_MAX_BACKOFF_SECONDS = 64 * 60


def enqueue(
    db: Session,
    deal_id: str,
    job_type: JobType,
    metadata: Optional[dict[str, Any]] = None,
    *,
    deduplicate: bool = True,
) -> Optional[IntegrationJob]:
    """
    Add a new job to the persistent queue.

    If ``deduplicate`` is True (default), an existing *pending* or *running*
    job with the same ``deal_id`` and ``job_type`` prevents a new insertion.
    The existing job is returned unchanged in that case.

    Args:
        db:          Open SQLAlchemy session.
        deal_id:     HubSpot deal ID this job relates to.
        job_type:    ``JobType`` enum value.
        metadata:    Optional JSON-serialisable dict attached to the job.
        deduplicate: Whether to skip insertion if a duplicate active job exists.

    Returns:
        The newly created (or existing duplicate) ``IntegrationJob``, or None
        if deduplication prevented insertion and no existing row was found
        (shouldn't happen in practice).
    """
    if deduplicate:
        existing = (
            db.query(IntegrationJob)
            .filter(
                IntegrationJob.deal_id == deal_id,
                IntegrationJob.job_type == job_type.value,
                IntegrationJob.status.in_(
                    [JobStatus.PENDING.value, JobStatus.RUNNING.value]
                ),
            )
            .first()
        )
        if existing:
            logger.debug(
                "Deduplicating job: deal_id=%s type=%s (existing id=%s)",
                deal_id,
                job_type.value,
                existing.id,
            )
            return existing

    job = IntegrationJob(
        deal_id=deal_id,
        job_type=job_type.value,
        status=JobStatus.PENDING.value,
        retry_count=0,
        metadata_json=json.dumps(metadata) if metadata else None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info(
        "Enqueued job id=%s deal_id=%s type=%s", job.id, deal_id, job_type.value
    )
    return job


def dequeue_next(db: Session) -> Optional[IntegrationJob]:
    """
    Atomically claim the oldest pending job that is ready to run.

    A job is *ready* when its ``next_retry_at`` is None or is in the past.

    Returns:
        An ``IntegrationJob`` with status updated to ``RUNNING``, or None if
        the queue is empty.
    """
    now = datetime.utcnow()
    job = (
        db.query(IntegrationJob)
        .filter(
            IntegrationJob.status == JobStatus.PENDING.value,
            or_(
                IntegrationJob.next_retry_at.is_(None),
                IntegrationJob.next_retry_at <= now,
            ),
        )
        .order_by(IntegrationJob.created_at)
        .with_for_update(skip_locked=True)
        .first()
    )
    if job is None:
        return None

    job.status = JobStatus.RUNNING.value
    db.commit()
    db.refresh(job)
    return job


def mark_done(db: Session, job: IntegrationJob) -> None:
    """
    Mark a job as successfully completed.

    Args:
        db:  Open SQLAlchemy session.
        job: The job to complete.
    """
    job.status = JobStatus.DONE.value
    job.error_message = None
    db.commit()
    logger.info("Job id=%s marked DONE.", job.id)


def mark_skipped(db: Session, job: IntegrationJob, reason: str = "") -> None:
    """
    Mark a job as skipped (not an error; duplicate or irrelevant).

    Args:
        db:     Open SQLAlchemy session.
        job:    The job to skip.
        reason: Human-readable reason for skipping.
    """
    job.status = JobStatus.SKIPPED.value
    job.error_message = reason or "skipped"
    db.commit()
    logger.info("Job id=%s marked SKIPPED: %s", job.id, reason)


def mark_failed(
    db: Session,
    job: IntegrationJob,
    error: str,
) -> None:
    """
    Increment retry count and schedule the next attempt with exponential
    back-off, or permanently fail the job if max retries are exhausted.

    Back-off formula: ``2 ** retry_count`` minutes, capped at
    ``_MAX_BACKOFF_SECONDS``.

    Args:
        db:    Open SQLAlchemy session.
        job:   The job that failed.
        error: Human-readable error message to store on the job.
    """
    job.retry_count += 1
    job.error_message = error[:2000]  # truncate very long errors

    if job.retry_count > job.max_retries:
        job.status = JobStatus.FAILED.value
        logger.error(
            "Job id=%s permanently FAILED after %s retries: %s",
            job.id,
            job.retry_count - 1,
            error,
        )
    else:
        backoff_s = min(2 ** job.retry_count * 60, _MAX_BACKOFF_SECONDS)
        job.next_retry_at = datetime.utcnow() + timedelta(seconds=backoff_s)
        job.status = JobStatus.PENDING.value
        logger.warning(
            "Job id=%s failed (attempt %s/%s); retry in %ss. Error: %s",
            job.id,
            job.retry_count,
            job.max_retries,
            backoff_s,
            error,
        )

    db.commit()


def get_queue_depth(db: Session) -> dict[str, int]:
    """
    Return counts of jobs by status.

    Returns:
        Dict mapping status string → count.
    """
    from sqlalchemy import func as sa_func

    rows = (
        db.query(IntegrationJob.status, sa_func.count(IntegrationJob.id))
        .group_by(IntegrationJob.status)
        .all()
    )
    return {row[0]: row[1] for row in rows}

