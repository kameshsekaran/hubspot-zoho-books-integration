"""
Tests: Sync engine — metric accuracy, change detection, full sync flow.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from app.sync_engine import (
    _no_change,
    compute_metrics,
    sync_closed_won_deal,
    sync_zoho_event,
)
from app.models.link import DealLink


class TestComputeMetricsAccuracy:
    """Verify metric calculations are accurate for the TechNova example deal."""

    def test_technova_all_draft(self):
        """
        TechNova deal: AED 126,000 with all three milestone invoices in draft.

        Expected: 100% invoiced, 0% collected, AED 0 unpaid (drafts excluded).
        """
        invoices = [
            {"invoice_id": "i1", "total": 50400, "balance": 50400, "payment_made": 0,
             "status": "draft", "due_date": "2026-10-30"},
            {"invoice_id": "i2", "total": 50400, "balance": 50400, "payment_made": 0,
             "status": "draft", "due_date": "2026-11-29"},
            {"invoice_id": "i3", "total": 25200, "balance": 25200, "payment_made": 0,
             "status": "draft", "due_date": "2027-01-28"},
        ]
        m = compute_metrics(invoices, 126000)
        assert m["pct_invoiced"] == pytest.approx(100.0)
        assert m["pct_collected"] == pytest.approx(0.0)
        assert m["amount_unpaid"] == pytest.approx(0.0)
        assert m["amount_uninvoiced"] == pytest.approx(0.0)
        assert m["overdue"] is False
        assert m["next_due_date"] == "2026-10-30"

    def test_technova_first_paid(self):
        """
        After Milestone 1 is paid (AED 50,400):
        - pct_collected ≈ 40%
        - amount_unpaid = 0 (only sent/overdue count)
        - next_due_date = go-live date
        """
        invoices = [
            {"invoice_id": "i1", "total": 50400, "balance": 0, "payment_made": 50400,
             "status": "paid", "due_date": "2026-10-30"},
            {"invoice_id": "i2", "total": 50400, "balance": 50400, "payment_made": 0,
             "status": "sent", "due_date": "2026-11-29"},
            {"invoice_id": "i3", "total": 25200, "balance": 25200, "payment_made": 0,
             "status": "draft", "due_date": "2027-01-28"},
        ]
        m = compute_metrics(invoices, 126000)
        assert m["pct_collected"] == pytest.approx(50400 / 126000 * 100, abs=0.01)
        assert m["amount_unpaid"] == pytest.approx(50400)
        assert m["next_due_date"] == "2026-11-29"

    def test_technova_all_paid(self):
        """After all three milestones paid — 100% collected."""
        invoices = [
            {"invoice_id": f"i{i}", "total": t, "balance": 0, "payment_made": t,
             "status": "paid", "due_date": "2026-10-30"}
            for i, t in enumerate([50400, 50400, 25200], 1)
        ]
        m = compute_metrics(invoices, 126000)
        assert m["pct_collected"] == pytest.approx(100.0, abs=0.01)
        assert m["amount_unpaid"] == 0.0
        assert m["next_due_date"] is None


class TestChangeDetection:
    """Verify change detection prevents unnecessary HubSpot writes."""

    def test_identical_state_no_change(self):
        state = {
            "pct_invoiced": 100.0, "pct_collected": 40.0,
            "amount_unpaid": 50400.0, "amount_uninvoiced": 0.0,
            "next_due_date": "2026-11-29", "overdue": False,
        }
        assert _no_change(state, state) is True

    def test_pct_change_triggers_update(self):
        old = {"pct_invoiced": 100.0, "pct_collected": 0.0, "amount_unpaid": 0.0,
               "amount_uninvoiced": 0.0, "next_due_date": None, "overdue": False}
        new = dict(old, pct_collected=40.0)
        assert _no_change(old, new) is False

    def test_overdue_change_triggers_update(self):
        old = {"pct_invoiced": 100.0, "pct_collected": 0.0, "amount_unpaid": 126000.0,
               "amount_uninvoiced": 0.0, "next_due_date": "2026-10-30", "overdue": False}
        new = dict(old, overdue=True)
        assert _no_change(old, new) is False


class TestSyncClosedWonDeal:
    """Integration tests for sync_closed_won_deal (mocked external calls)."""

    def test_full_sync_creates_zoho_resources(self, db):
        """sync_closed_won_deal should create customer, SO, and 3 invoices."""
        hs_mock = MagicMock()
        hs_mock.get_deal.return_value = {
            "properties": {
                "amount": "126000", "closedate": "1761753600000",
                "deal_currency_code": "AED",
                "dealname": "TechNova — ERP",
                "hubspot_owner_id": None,
            },
            "associations": {
                "companies": {"results": [{"id": "co_001"}]},
                "contacts": {"results": [{"id": "ct_001"}]},
            },
        }
        hs_mock.get_company.return_value = {"properties": {"name": "TechNova LLC"}}
        hs_mock.get_contact.return_value = {"properties": {"email": "cto@technova.ae", "phone": ""}}
        hs_mock.patch_deal.return_value = {}

        zoho_mock = MagicMock()

        with patch("app.sync_engine.HubSpotClient", return_value=hs_mock), \
             patch("app.sync_engine.ZohoClient", return_value=zoho_mock), \
             patch("app.sync_engine.find_customer_by_email") as mock_find, \
             patch("app.sync_engine.create_customer") as mock_create_cust, \
             patch("app.sync_engine.create_sales_order") as mock_create_so, \
             patch("app.sync_engine.create_milestone_invoices") as mock_create_inv, \
             patch("app.sync_engine.get_invoice") as mock_get_inv:

            mock_find.return_value = None  # No existing customer
            mock_create_cust.return_value = {"contact_id": "zb_cust_456"}
            mock_create_so.return_value = {
                "salesorder_id": "zb_so_001",
                "salesorder_number": "SO-00042",
                "total": 126000,
            }
            mock_create_inv.return_value = [
                {"invoice_id": "zb_inv_001"},
                {"invoice_id": "zb_inv_002"},
                {"invoice_id": "zb_inv_003"},
            ]
            mock_get_inv.return_value = {
                "invoice_id": "zb_inv_001", "total": 50400,
                "balance": 50400, "payment_made": 0, "status": "draft", "due_date": "2026-10-30",
            }

            result = sync_closed_won_deal("hs_deal_789", db)

        assert result["zoho_so_id"] == "zb_so_001"
        assert len(result["invoice_ids"]) == 3
        mock_create_cust.assert_called_once()
        mock_create_so.assert_called_once()
        mock_create_inv.assert_called_once()

    def test_existing_customer_reused(self, db):
        """If a customer exists in Zoho, no new customer should be created."""
        hs_mock = MagicMock()
        hs_mock.get_deal.return_value = {
            "properties": {"amount": "126000", "closedate": "1761753600000",
                           "deal_currency_code": "AED", "dealname": "Test", "hubspot_owner_id": None},
            "associations": {
                "contacts": {"results": [{"id": "ct_001"}]},
                "companies": {"results": []},
            },
        }
        hs_mock.get_contact.return_value = {"properties": {"email": "cto@technova.ae"}}
        hs_mock.patch_deal.return_value = {}

        with patch("app.sync_engine.HubSpotClient", return_value=hs_mock), \
             patch("app.sync_engine.ZohoClient"), \
             patch("app.sync_engine.find_customer_by_email") as mock_find, \
             patch("app.sync_engine.create_customer") as mock_create_cust, \
             patch("app.sync_engine.create_sales_order") as mock_create_so, \
             patch("app.sync_engine.create_milestone_invoices") as mock_create_inv, \
             patch("app.sync_engine.get_invoice") as mock_get_inv:

            mock_find.return_value = {"contact_id": "zb_cust_existing"}
            mock_create_so.return_value = {"salesorder_id": "so_new", "salesorder_number": "SO-X", "total": 126000}
            mock_create_inv.return_value = [{"invoice_id": "inv_1"}, {"invoice_id": "inv_2"}, {"invoice_id": "inv_3"}]
            mock_get_inv.return_value = {"invoice_id": "inv_1", "total": 0, "balance": 0, "payment_made": 0, "status": "draft", "due_date": ""}

            result = sync_closed_won_deal("new_deal_001", db)

        # create_customer should NOT have been called
        mock_create_cust.assert_not_called()
        assert result["zoho_customer_id"] == "zb_cust_existing"


class TestSyncZohoEvent:
    """Tests for sync_zoho_event."""

    def test_returns_none_for_unknown_invoice(self, db):
        """If no deal is found for the given invoice, return None."""
        result = sync_zoho_event(db, invoice_id="unknown_inv_999")
        assert result is None

    def test_no_hubspot_write_when_unchanged(self, db, deal_link):
        """If metrics haven't changed, HubSpot PATCH should not be called."""
        # Pre-set sync state to match what we'll compute
        state = {
            "pct_invoiced": 100.0, "pct_collected": 0.0, "amount_unpaid": 0.0,
            "amount_uninvoiced": 0.0, "next_due_date": "2026-10-30", "overdue": False,
        }
        deal_link.set_sync_state(state)
        db.commit()

        hs_mock = MagicMock()
        with patch("app.sync_engine.HubSpotClient", return_value=hs_mock), \
             patch("app.sync_engine.ZohoClient"), \
             patch("app.sync_engine.get_sales_order") as mock_get_so, \
             patch("app.sync_engine.get_invoice") as mock_get_inv:

            mock_get_so.return_value = {"total": 126000}
            mock_get_inv.return_value = {
                "invoice_id": "zb_inv_001", "total": 50400, "balance": 50400,
                "payment_made": 0, "status": "draft", "due_date": "2026-10-30",
            }

            result = sync_zoho_event(db, invoice_id="zb_inv_001")

        # HubSpot patch should NOT have been called
        hs_mock.patch_deal.assert_not_called()
        assert result["changed"] is False

