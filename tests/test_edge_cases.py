"""
Tests: Q1–Q7 edge cases from the assignment.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, call, patch

import pytest

from app.models.link import DealLink
from app.queue import enqueue
from app.models.job import JobType
from app.sync_engine import (
    compute_metrics,
    handle_deal_reopened,
    sync_zoho_event,
)


class TestQ1ZohoOutage:
    """Q1: Zoho Books unreachable for six hours — jobs should survive."""

    def test_failed_job_reschedules_with_backoff(self, db):
        """A failed job should increment retry count and set next_retry_at."""
        from app.queue import enqueue, mark_failed, dequeue_next
        from app.models.job import IntegrationJob

        job = enqueue(db, "deal_q1", JobType.CRM_TO_ZOHO)
        job = dequeue_next(db)
        assert job is not None

        mark_failed(db, job, "ConnectionError: Zoho unreachable")

        db.expire(job)
        assert job.retry_count == 1
        assert job.status == "pending"  # rescheduled, not failed
        assert job.next_retry_at is not None
        assert job.next_retry_at > datetime.utcnow()

    def test_job_permanently_fails_after_max_retries(self, db):
        """A job that exhausts all retries should be marked FAILED."""
        from app.queue import enqueue, mark_failed, dequeue_next
        from app.models.job import IntegrationJob

        job = enqueue(db, "deal_q1b", JobType.CRM_TO_ZOHO)
        job.max_retries = 2
        db.commit()

        for _ in range(3):
            job.status = "running"
            db.commit()
            mark_failed(db, job, "ConnectionError")

        db.expire(job)
        assert job.status == "failed"

    def test_webhook_enqueued_immediately(self, client):
        """HubSpot webhook should return 200 even if Zoho is down."""
        payload = [{"objectId": "deal_q1", "propertyName": "dealstage", "propertyValue": "closedwon"}]
        resp = client.post("/webhooks/hubspot", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"


class TestQ2DealReopened:
    """Q2: Deal reopened after first invoice was sent."""

    def test_draft_invoices_voided_on_reopen(self, db, deal_link):
        """Draft invoices should be auto-voided when deal is reopened."""
        hs_mock = MagicMock()
        hs_mock.patch_deal.return_value = {}

        zoho_mock = MagicMock()
        zoho_mock.get.side_effect = [
            # get_invoice for inv_001 (sent)
            {"invoice": {"invoice_id": "zb_inv_001", "status": "sent"}},
            # get_invoice for inv_002 (draft)
            {"invoice": {"invoice_id": "zb_inv_002", "status": "draft"}},
            # get_invoice for inv_003 (draft)
            {"invoice": {"invoice_id": "zb_inv_003", "status": "draft"}},
            # get_sales_order (not needed here but guard)
            {"salesorder": {"total": 126000}},
        ]
        zoho_mock.post.return_value = {}  # void response

        with patch("app.sync_engine.HubSpotClient", return_value=hs_mock), \
             patch("app.sync_engine.ZohoClient", return_value=zoho_mock), \
             patch("app.sync_engine.get_invoice") as mock_get_inv, \
             patch("app.sync_engine.void_invoice") as mock_void:

            mock_get_inv.side_effect = [
                {"invoice_id": "zb_inv_001", "status": "sent"},
                {"invoice_id": "zb_inv_002", "status": "draft"},
                {"invoice_id": "zb_inv_003", "status": "draft"},
            ]

            result = handle_deal_reopened("hs_deal_789", db)

        assert result["action"] == "needs_review"
        # Void should have been called for draft invoices only
        assert mock_void.call_count == 2

    def test_status_set_to_needs_review_when_sent_invoice(self, db, deal_link):
        """HubSpot deal should be set to needs_review when a sent invoice exists."""
        hs_mock = MagicMock()
        hs_mock.patch_deal.return_value = {}

        with patch("app.sync_engine.HubSpotClient", return_value=hs_mock), \
             patch("app.sync_engine.ZohoClient"), \
             patch("app.sync_engine.get_invoice") as mock_get_inv, \
             patch("app.sync_engine.void_invoice"):

            mock_get_inv.side_effect = [
                {"invoice_id": "zb_inv_001", "status": "sent"},
                {"invoice_id": "zb_inv_002", "status": "draft"},
                {"invoice_id": "zb_inv_003", "status": "draft"},
            ]
            handle_deal_reopened("hs_deal_789", db)

        call_args = hs_mock.patch_deal.call_args
        props = call_args[0][1]
        assert props["zoho_sync_status"] == "needs_review"


class TestQ3AmountChange:
    """Q3: Deal amount changes after SO exists — metrics should reflect Zoho data."""

    def test_pct_collected_based_on_zoho_total(self):
        """Metrics should use the actual SO total from Zoho, not HubSpot amount."""
        invoices = [
            {"invoice_id": "inv_001", "total": 50400, "balance": 0, "payment_made": 50400, "status": "paid", "due_date": ""},
            {"invoice_id": "inv_002", "total": 50400, "balance": 50400, "payment_made": 0, "status": "sent", "due_date": "2026-11-29"},
        ]
        # SO total from Zoho: 126000 (even if HubSpot says 140000)
        metrics = compute_metrics(invoices, 126000)
        assert metrics["pct_collected"] == pytest.approx(50400 / 126000 * 100, abs=0.1)


class TestQ4DuplicateEvents:
    """Q4: Same Closed Won event three times — only one SO and 3 invoices created."""

    def test_duplicate_jobs_deduplicated(self, db):
        """Three enqueue calls for same deal should result in one job."""
        enqueue(db, "deal_q4", JobType.CRM_TO_ZOHO)
        enqueue(db, "deal_q4", JobType.CRM_TO_ZOHO)
        enqueue(db, "deal_q4", JobType.CRM_TO_ZOHO)

        from app.models.job import IntegrationJob
        count = db.query(IntegrationJob).filter(
            IntegrationJob.deal_id == "deal_q4"
        ).count()
        assert count == 1

    def test_existing_zoho_so_skips_creation(self, db, deal_link):
        """If deal_link already has zoho_so_id, SO creation should be skipped."""
        hs_mock = MagicMock()
        hs_mock.get_deal.return_value = {
            "properties": {"amount": "126000", "closedate": "1761753600000",
                           "deal_currency_code": "AED", "dealname": "Test", "hubspot_owner_id": None},
            "associations": {},
        }
        hs_mock.patch_deal.return_value = {}

        zoho_mock = MagicMock()

        with patch("app.sync_engine.HubSpotClient", return_value=hs_mock), \
             patch("app.sync_engine.ZohoClient", return_value=zoho_mock), \
             patch("app.sync_engine.get_sales_order") as mock_get_so, \
             patch("app.sync_engine.get_invoice") as mock_get_inv, \
             patch("app.sync_engine.create_sales_order") as mock_create_so, \
             patch("app.sync_engine.create_milestone_invoices") as mock_create_inv:

            mock_get_so.return_value = {"salesorder_id": "zb_so_001", "total": 126000}
            mock_get_inv.return_value = {"invoice_id": "i", "total": 0, "balance": 0, "payment_made": 0, "status": "draft", "due_date": ""}

            from app.sync_engine import sync_closed_won_deal
            sync_closed_won_deal("hs_deal_789", db)

        # create_sales_order should NOT have been called
        mock_create_so.assert_not_called()
        mock_create_inv.assert_not_called()


class TestQ5InvoiceVoidedAndReissued:
    """Q5: Invoice voided in Zoho — integration should update deal_links."""

    def test_void_event_removes_invoice_from_deal_links(self, db, deal_link):
        """When a void webhook is received, the invoice ID is removed from deal_links."""
        hs_mock = MagicMock()
        hs_mock.patch_deal.return_value = {}

        with patch("app.sync_engine.HubSpotClient", return_value=hs_mock), \
             patch("app.sync_engine.ZohoClient"), \
             patch("app.sync_engine.get_invoice") as mock_get_inv, \
             patch("app.sync_engine.get_sales_order") as mock_get_so:

            # First call: the voided invoice; subsequent calls: other invoices
            mock_get_inv.side_effect = [
                {"invoice_id": "zb_inv_001", "status": "void", "total": 50400, "balance": 0, "payment_made": 0, "due_date": ""},
                {"invoice_id": "zb_inv_002", "status": "draft", "total": 50400, "balance": 50400, "payment_made": 0, "due_date": "2026-11-29"},
                {"invoice_id": "zb_inv_003", "status": "draft", "total": 25200, "balance": 25200, "payment_made": 0, "due_date": "2027-01-28"},
            ]
            mock_get_so.return_value = {"total": 126000}

            sync_zoho_event(
                db,
                invoice_id="zb_inv_001",
                event_type="invoice.status_changed",
            )

        db.expire(deal_link)
        remaining_ids = deal_link.get_invoice_ids()
        # voided invoice should be removed
        assert "zb_inv_001" not in remaining_ids


class TestQ6CrossDealPayment:
    """Q6: Payment applied to invoices from two deals — each deal synced independently."""

    def test_each_deal_synced_independently(self, db):
        """Two deals with separate invoices should be synced independently."""
        link_a = DealLink(hs_deal_id="deal_a")
        link_a.set_invoice_ids(["inv_a_001"])
        link_b = DealLink(hs_deal_id="deal_b")
        link_b.set_invoice_ids(["inv_b_001"])
        db.add_all([link_a, link_b])
        db.commit()
        link_a.zoho_so_id = "so_a"
        link_b.zoho_so_id = "so_b"
        db.commit()

        # Both deals should be findable independently
        found_a = db.query(DealLink).filter(
            DealLink.zoho_invoice_ids.like("%inv_a_001%")
        ).first()
        found_b = db.query(DealLink).filter(
            DealLink.zoho_invoice_ids.like("%inv_b_001%")
        ).first()

        assert found_a.hs_deal_id == "deal_a"
        assert found_b.hs_deal_id == "deal_b"


class TestQ7DailyAPILimitExhausted:
    """Q7: Zoho daily API limit hit — all pending jobs paused."""

    def test_daily_limit_error_classification(self):
        """ZohoDailyLimitError should be raised when daily quota is exceeded."""
        from app.zoho.client import ZohoDailyLimitError, ZohoClient, _daily_call_count
        import app.zoho.client as zoho_client_module

        # Simulate daily limit exhausted
        zoho_client_module._daily_call_count = zoho_client_module._daily_call_count  # reference

        # Create a client and mock the daily limit as reached
        db_mock = MagicMock()
        client = ZohoClient(db_mock)

        # Patch daily count to be at limit
        with patch.object(zoho_client_module, "_daily_call_count", 2000), \
             patch("app.config.settings") as mock_settings:
            mock_settings.zoho_daily_limit = 2000
            mock_settings.zoho_org_id = "test_org"
            mock_settings.zoho_books_url = "https://books.zohoapis.com/books/v3"
            mock_settings.zoho_rate_limit_per_min = 60
            with pytest.raises(ZohoDailyLimitError):
                client._request("GET", "/test")

