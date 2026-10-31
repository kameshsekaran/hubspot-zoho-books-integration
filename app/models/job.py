"""
IntegrationJob ORM model.

Represents a unit of asynchronous work (e.g., sync a CRM deal to Zoho Books).
Jobs are created by webhook handlers and processed by the job-queue worker.
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class JobStatus(str, enum.Enum):
    """Lifecycle states for a queued integration job."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobType(str, enum.Enum):
    """Direction / type of sync work to perform."""

    CRM_TO_ZOHO = "crm_to_zoho"
    ZOHO_TO_CRM = "zoho_to_crm"


class IntegrationJob(Base):
    """
    Persistent job queue entry.

    Each row represents one attempt to synchronise a HubSpot deal with Zoho
    Books (or vice-versa).  Failed jobs are retried with exponential back-off
    up to *max_retries* times before being marked FAILED permanently.
    """

    __tablename__ = "integration_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The HubSpot deal ID this job relates to.
    deal_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    job_type: Mapped[str] = mapped_column(
        Enum(JobType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        Enum(JobStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=JobStatus.PENDING.value,
        index=True,
    )

    # Number of times this job has been attempted (0 = not yet tried).
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Maximum number of retries before the job is abandoned.
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=6)

    # Human-readable error captured on the last failure.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional JSON payload attached by the enqueue caller (e.g., webhook body).
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # When a failed job becomes eligible for the next retry attempt.
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<IntegrationJob id={self.id} deal_id={self.deal_id!r} "
            f"type={self.job_type!r} status={self.status!r} "
            f"retry={self.retry_count}/{self.max_retries}>"
        )
