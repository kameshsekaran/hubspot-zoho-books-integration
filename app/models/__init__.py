"""Models package – re-exports all ORM models for convenient importing."""

from app.models.job import IntegrationJob, JobStatus, JobType
from app.models.link import DealLink
from app.models.log import IntegrationLog

__all__ = [
    "IntegrationJob",
    "JobStatus",
    "JobType",
    "DealLink",
    "IntegrationLog",
]
