"""
Tests: Idempotency — duplicate events, deal_links deduplication.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.models.job import IntegrationJob, JobStatus, JobType
from app.models.link import DealLink
from app.queue import enqueue, dequeue_next, mark_done


class TestJobQueueDeduplication:
    """Enqueuing the same job twice should not create duplicates."""

    def test_duplicate_pending_job_not_inserted(self, db):
        """A second enqueue for same deal_id + job_type returns the existing job."""
        job1 = enqueue(db, deal_id="deal_001", job_type=JobType.CRM_TO_ZOHO)
        job2 = enqueue(db, deal_id="deal_001", job_type=JobType.CRM_TO_ZOHO)

        assert job1.id == job2.id
        count = db.query(IntegrationJob).filter(
            IntegrationJob.deal_id == "deal_001",
            IntegrationJob.job_type == JobType.CRM_TO_ZOHO.value,
        ).count()
        assert count == 1

    def test_different_deal_ids_both_inserted(self, db):
        """Different deal IDs should each get their own job."""
        enqueue(db, deal_id="deal_001", job_type=JobType.CRM_TO_ZOHO)
        enqueue(db, deal_id="deal_002", job_type=JobType.CRM_TO_ZOHO)

        count = db.query(IntegrationJob).count()
        assert count == 2

    def test_done_job_allows_new_enqueue(self, db):
        """After a job is marked DONE, a new job for the same deal should be created."""
        job1 = enqueue(db, deal_id="deal_001", job_type=JobType.CRM_TO_ZOHO)
        job1_id = job1.id
        job = dequeue_next(db)
        mark_done(db, job)

        job2 = enqueue(db, deal_id="deal_001", job_type=JobType.CRM_TO_ZOHO)
        assert job2.id != job1_id

    def test_no_dedup_flag_inserts_duplicate(self, db):
        """With deduplicate=False, multiple jobs for the same deal are allowed."""
        enqueue(db, deal_id="deal_001", job_type=JobType.CRM_TO_ZOHO, deduplicate=False)
        enqueue(db, deal_id="deal_001", job_type=JobType.CRM_TO_ZOHO, deduplicate=False)

        count = db.query(IntegrationJob).filter(
            IntegrationJob.deal_id == "deal_001"
        ).count()
        assert count == 2


class TestDealLinksIdempotency:
    """DealLink ensures we don't create duplicate Zoho resources."""

    def test_deal_link_unique_constraint(self, db):
        """Inserting two DealLink rows with the same hs_deal_id should fail."""
        from sqlalchemy.exc import IntegrityError

        db.add(DealLink(hs_deal_id="deal_001"))
        db.commit()
        db.add(DealLink(hs_deal_id="deal_001"))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_invoice_ids_roundtrip(self, db):
        """Invoice IDs should survive JSON serialisation round-trip."""
        link = DealLink(hs_deal_id="deal_002")
        db.add(link)
        db.commit()

        ids = ["inv_001", "inv_002", "inv_003"]
        link.set_invoice_ids(ids)
        db.commit()

        db.expire(link)
        assert link.get_invoice_ids() == ids

    def test_sync_state_roundtrip(self, db):
        """Sync state dict should survive JSON serialisation round-trip."""
        link = DealLink(hs_deal_id="deal_003")
        db.add(link)
        db.commit()

        state = {"pct_collected": 40.0, "overdue": False, "next_due_date": "2026-11-30"}
        link.set_sync_state(state)
        db.commit()

        db.expire(link)
        assert link.get_sync_state() == state

    def test_empty_invoice_ids(self, db):
        """An unset invoice_ids field should return an empty list."""
        link = DealLink(hs_deal_id="deal_004")
        db.add(link)
        db.commit()
        assert link.get_invoice_ids() == []

    def test_empty_sync_state(self, db):
        """An unset sync_state field should return an empty dict."""
        link = DealLink(hs_deal_id="deal_005")
        db.add(link)
        db.commit()
        assert link.get_sync_state() == {}

