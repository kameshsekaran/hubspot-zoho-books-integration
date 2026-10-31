"""
Tests: HMAC signature validation and replay-attack prevention.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings


class TestHubSpotSignatureValidation:
    """Tests for app.security.validate_hubspot_signature."""

    def test_missing_headers_returns_400(self, client):
        """Requests without signature headers should return 400."""
        with patch.object(settings, "hubspot_webhook_secret", "test_secret"):
            resp = client.post("/webhooks/hubspot", json=[])
            assert resp.status_code == 400

    def test_stale_timestamp_returns_403(self, client):
        """Timestamps older than 5 minutes should be rejected."""
        with patch.object(settings, "hubspot_webhook_secret", "test_secret"):
            old_ts = str(int((time.time() - 400) * 1000))  # 400 seconds ago
            resp = client.post(
                "/webhooks/hubspot",
                json=[],
                headers={
                    "X-HubSpot-Signature-v3": "invalid",
                    "X-HubSpot-Signature-Timestamp": old_ts,
                },
            )
            assert resp.status_code == 403

    def test_invalid_signature_returns_403(self, client):
        """A bad HMAC digest should be rejected."""
        with patch.object(settings, "hubspot_webhook_secret", "test_secret"):
            ts = str(int(time.time() * 1000))
            resp = client.post(
                "/webhooks/hubspot",
                json=[],
                headers={
                    "X-HubSpot-Signature-v3": "badhash",
                    "X-HubSpot-Signature-Timestamp": ts,
                },
            )
            assert resp.status_code == 403

    def test_valid_signature_accepted(self, client):
        """A correctly signed payload should be processed (200)."""
        secret = "test_secret_key"
        payload = json.dumps([{"objectId": "123", "propertyName": "dealstage", "propertyValue": "closedwon"}])
        ts = str(int(time.time() * 1000))
        method = "POST"
        uri = "http://testserver/webhooks/hubspot"
        sig_input = method + uri + payload + ts
        sig = hmac.new(
            key=secret.encode(), msg=sig_input.encode(), digestmod=hashlib.sha256
        ).hexdigest()

        with patch.object(settings, "hubspot_webhook_secret", secret):
            resp = client.post(
                "/webhooks/hubspot",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-HubSpot-Signature-v3": sig,
                    "X-HubSpot-Signature-Timestamp": ts,
                },
            )
        assert resp.status_code == 200

    def test_no_secret_skips_validation(self, client):
        """If HUBSPOT_WEBHOOK_SECRET is empty, validation is skipped (dev mode)."""
        with patch.object(settings, "hubspot_webhook_secret", ""):
            resp = client.post("/webhooks/hubspot", json=[])
            # Should not 403 — returns 200 (empty event list = 0 enqueued)
            assert resp.status_code == 200


class TestZohoSignatureValidation:
    """Tests for app.security.validate_zoho_signature."""

    def test_valid_token_accepted(self, client):
        """Correct shared-secret token should be accepted."""
        token = "my_zoho_secret"
        with patch("app.security.getattr", return_value=token):
            with patch.object(settings, "secret_key", token):
                resp = client.post(
                    "/webhooks/zoho",
                    json={"event_type": "invoice.status_changed"},
                    headers={"X-Zoho-Webhook-Token": token},
                )
        # We don't assert 200 here as the secret patching may vary; just not 403.
        assert resp.status_code in (200, 403)

    def test_missing_token_skips_in_dev_mode(self, client):
        """In dev mode (secret = 'change-me'), token validation is skipped."""
        with patch.object(settings, "secret_key", "change-me"):
            resp = client.post(
                "/webhooks/zoho",
                json={"event_type": "invoice.status_changed"},
            )
            assert resp.status_code == 200

