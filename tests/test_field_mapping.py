"""
Tests: Field mapping — all 14 custom properties, edge cases.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from app.sync_engine import (
    _extract_association_ids,
    _metrics_to_hs_props,
    _no_change,
    _parse_hs_date,
    compute_metrics,
)


class TestComputeMetrics:
    """Tests for sync_engine.compute_metrics."""

    def test_all_draft_invoices(self, mock_zoho_invoices):
        """Three draft invoices — 100% invoiced, 0% collected, no unpaid amount."""
        metrics = compute_metrics(mock_zoho_invoices, 126000)

        assert metrics["pct_invoiced"] == 100.0
        assert metrics["pct_collected"] == 0.0
        assert metrics["amount_unpaid"] == 0.0  # drafts don't count as unpaid
        assert metrics["amount_uninvoiced"] == 0.0
        assert metrics["overdue"] is False
        assert metrics["next_due_date"] == "2026-10-30"

    def test_first_invoice_sent(self, mock_zoho_invoices):
        """When first invoice is sent, unpaid amount should be 50,400."""
        invoices = mock_zoho_invoices.copy()
        invoices[0] = dict(invoices[0], status="sent")

        metrics = compute_metrics(invoices, 126000)
        assert metrics["amount_unpaid"] == pytest.approx(50400)
        assert metrics["pct_collected"] == 0.0

    def test_first_invoice_paid(self, mock_zoho_invoices):
        """When first invoice is paid, collected % should be ~40%."""
        invoices = mock_zoho_invoices.copy()
        invoices[0] = dict(
            invoices[0], status="paid", balance=0, payment_made=50400
        )
        metrics = compute_metrics(invoices, 126000)
        assert metrics["pct_collected"] == pytest.approx(40.0, abs=0.1)
        assert metrics["amount_unpaid"] == 0.0

    def test_overdue_flag_set(self, mock_zoho_invoices):
        """An overdue invoice should set overdue=True."""
        invoices = mock_zoho_invoices.copy()
        invoices[0] = dict(invoices[0], status="overdue")
        metrics = compute_metrics(invoices, 126000)
        assert metrics["overdue"] is True

    def test_voided_invoices_excluded(self, mock_zoho_invoices):
        """Voided invoices should not count toward any metric."""
        invoices = mock_zoho_invoices.copy()
        invoices[0] = dict(invoices[0], status="void")
        metrics = compute_metrics(invoices, 126000)
        # Only 2 invoices (50400 + 25200 = 75600) should count
        assert metrics["pct_invoiced"] == pytest.approx(75600 / 126000 * 100, abs=0.1)

    def test_no_invoices(self):
        """Empty invoice list should produce zero metrics."""
        metrics = compute_metrics([], 126000)
        assert metrics["pct_invoiced"] == 0.0
        assert metrics["pct_collected"] == 0.0
        assert metrics["overdue"] is False
        assert metrics["next_due_date"] is None

    def test_zero_so_total_no_division_error(self):
        """SO total of 0 should not cause a division by zero."""
        metrics = compute_metrics([{"total": 0, "balance": 0, "payment_made": 0, "status": "draft", "due_date": ""}], 0)
        assert metrics["pct_invoiced"] == 0.0

    def test_all_paid_100_pct_collected(self, mock_zoho_invoices):
        """All three paid → 100% collected."""
        invoices = [
            dict(inv, status="paid", balance=0, payment_made=inv["total"])
            for inv in mock_zoho_invoices
        ]
        metrics = compute_metrics(invoices, 126000)
        assert metrics["pct_collected"] == pytest.approx(100.0, abs=0.1)
        assert metrics["amount_unpaid"] == 0.0
        assert metrics["next_due_date"] is None  # all paid


class TestParseHubSpotDate:
    """Tests for the date parsing helper."""

    def test_parses_epoch_ms(self):
        """HubSpot epoch ms string should parse to a date."""
        result = _parse_hs_date("1761753600000")
        assert result is not None
        assert isinstance(result, date)

    def test_parses_iso_date(self):
        """ISO 8601 date string should parse correctly."""
        result = _parse_hs_date("2026-10-30")
        assert result == date(2026, 10, 30)

    def test_none_returns_none(self):
        assert _parse_hs_date(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_hs_date("") is None

    def test_invalid_string_returns_none(self):
        assert _parse_hs_date("not-a-date") is None


class TestExtractAssociationIds:
    """Tests for the association ID extractor."""

    def test_extracts_company_ids(self):
        assoc = {"companies": {"results": [{"id": "co_001"}, {"id": "co_002"}]}}
        ids = _extract_association_ids(assoc, "companies")
        assert ids == ["co_001", "co_002"]

    def test_missing_assoc_type_returns_empty(self):
        ids = _extract_association_ids({}, "companies")
        assert ids == []


class TestMetricsToHubSpotProps:
    """Tests for the metrics → HubSpot property mapper."""

    def test_all_fields_present(self):
        metrics = {
            "pct_invoiced": 100.0,
            "pct_collected": 40.0,
            "amount_unpaid": 50400.0,
            "amount_uninvoiced": 0.0,
            "next_due_date": "2026-10-30",
            "overdue": False,
        }
        props = _metrics_to_hs_props(metrics)
        assert props["zoho_pct_invoiced"] == "100.0"
        assert props["zoho_pct_collected"] == "40.0"
        assert props["zoho_amount_unpaid"] == "50400.0"
        assert props["zoho_amount_uninvoiced"] == "0.0"
        assert props["zoho_next_due_date"] == "2026-10-30"
        assert props["zoho_overdue"] == "false"

    def test_overdue_true(self):
        metrics = {
            "pct_invoiced": 0, "pct_collected": 0, "amount_unpaid": 0,
            "amount_uninvoiced": 0, "next_due_date": None, "overdue": True,
        }
        props = _metrics_to_hs_props(metrics)
        assert props["zoho_overdue"] == "true"

    def test_no_next_due_date_excluded(self):
        metrics = {
            "pct_invoiced": 0, "pct_collected": 0, "amount_unpaid": 0,
            "amount_uninvoiced": 0, "next_due_date": None, "overdue": False,
        }
        props = _metrics_to_hs_props(metrics)
        assert "zoho_next_due_date" not in props


class TestChangeDetection:
    """Tests for the _no_change function."""

    def test_no_change_returns_true(self):
        state = {
            "pct_invoiced": 100.0, "pct_collected": 40.0,
            "amount_unpaid": 50400.0, "amount_uninvoiced": 0.0,
            "next_due_date": "2026-10-30", "overdue": False,
        }
        assert _no_change(state, state) is True

    def test_change_detected(self):
        old = {"pct_invoiced": 100.0, "pct_collected": 0.0, "amount_unpaid": 126000.0,
               "amount_uninvoiced": 0.0, "next_due_date": "2026-10-30", "overdue": False}
        new = dict(old, pct_collected=40.0, amount_unpaid=75600.0)
        assert _no_change(old, new) is False

    def test_empty_last_state_triggers_update(self):
        """Empty last state should always result in change = True (force write)."""
        metrics = {"pct_invoiced": 0, "pct_collected": 0, "amount_unpaid": 0,
                   "amount_uninvoiced": 0, "next_due_date": None, "overdue": False}
        assert _no_change({}, metrics) is False

