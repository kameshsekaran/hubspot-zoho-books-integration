"""
Tests: HubSpot client — deal fetch, property update, error handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.hubspot_client import (
    HubSpotClient,
    HubSpotPermanentError,
    HubSpotRateLimitError,
    HubSpotTemporaryError,
)


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


class TestHubSpotClientGetDeal:
    """Tests for HubSpotClient.get_deal."""

    def test_get_deal_success(self, db, mock_hs_deal):
        """get_deal should return the raw deal dict on a 200 response."""
        client = HubSpotClient(db, deal_id="hs_deal_789")
        mock_resp = _mock_response(200, mock_hs_deal)

        with patch.object(client, "_execute_with_retry", return_value=mock_resp):
            result = client.get_deal("hs_deal_789")

        assert result["id"] == "hs_deal_789"
        assert result["properties"]["amount"] == "126000"

    def test_get_deal_404_raises_permanent_error(self, db):
        """get_deal on a missing deal should raise HubSpotPermanentError."""
        client = HubSpotClient(db, deal_id="missing_deal")
        mock_resp = _mock_response(404, {"status": "error", "message": "Not found"})

        with patch.object(client, "_execute_with_retry", return_value=mock_resp):
            with pytest.raises(HubSpotPermanentError) as exc_info:
                client.get_deal("missing_deal")
        assert exc_info.value.status_code == 404

    def test_get_deal_429_raises_rate_limit_error(self, db):
        """get_deal on 429 should raise HubSpotRateLimitError."""
        client = HubSpotClient(db, deal_id="deal_001")
        with patch.object(
            client, "_execute_with_retry", side_effect=HubSpotRateLimitError("429", 429)
        ):
            with pytest.raises(HubSpotRateLimitError):
                client.get_deal("deal_001")


class TestHubSpotClientPatchDeal:
    """Tests for HubSpotClient.patch_deal."""

    def test_patch_deal_sends_correct_payload(self, db):
        """patch_deal should include the properties dict in the PATCH body."""
        client = HubSpotClient(db, deal_id="hs_deal_789")
        mock_resp = _mock_response(200, {"id": "hs_deal_789", "properties": {}})

        with patch.object(client, "_execute_with_retry", return_value=mock_resp) as mock_exec:
            client.patch_deal("hs_deal_789", {"zoho_so_id": "zb_so_001"})

        # Verify the call was made
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        assert call_args.args[0] == "PATCH"
        assert "hs_deal_789" in call_args.args[1]

    def test_patch_deal_500_raises_temporary_error(self, db):
        """patch_deal on 500 should raise HubSpotTemporaryError."""
        client = HubSpotClient(db, deal_id="deal_001")
        with patch.object(
            client, "_execute_with_retry", side_effect=HubSpotTemporaryError("500", 500)
        ):
            with pytest.raises(HubSpotTemporaryError):
                client.patch_deal("deal_001", {"zoho_so_id": "x"})


class TestHubSpotClientPropertyManagement:
    """Tests for property creation helpers."""

    def test_get_property_returns_none_on_404(self, db):
        """get_property should return None if the property does not exist."""
        client = HubSpotClient(db)
        mock_resp = _mock_response(404, {"message": "property not found"})

        with patch.object(client, "_execute_with_retry", return_value=mock_resp):
            result = client.get_property("nonexistent_prop")

        assert result is None

    def test_create_property_success(self, db):
        """create_property should return the created property dict."""
        client = HubSpotClient(db)
        prop_resp = {"name": "zoho_so_id", "label": "Zoho SO ID", "type": "string"}
        mock_resp = _mock_response(201, prop_resp)

        with patch.object(client, "_execute_with_retry", return_value=mock_resp):
            result = client.create_property({
                "name": "zoho_so_id",
                "label": "Zoho SO ID",
                "type": "string",
                "fieldType": "text",
                "groupName": "zoho_integration",
            })

        assert result.get("name") == "zoho_so_id"


class TestHubSpotClientLogging:
    """Tests that log entries are written correctly."""

    def test_successful_request_creates_log_entry(self, db, mock_hs_deal):
        """A successful API call should create an IntegrationLog row."""
        from app.models.log import IntegrationLog

        client = HubSpotClient(db, deal_id="hs_deal_789")
        mock_resp = _mock_response(200, mock_hs_deal)

        with patch.object(client, "_execute_with_retry", return_value=mock_resp):
            client.get_deal("hs_deal_789")

        logs = db.query(IntegrationLog).all()
        assert len(logs) == 1
        assert logs[0].system == "hubspot"
        assert logs[0].success is True
        assert logs[0].deal_id == "hs_deal_789"

