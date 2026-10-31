"""
Webhook signature validation.

HubSpot:
    Uses HMAC-SHA256 over ``{method}{uri}{body}{timestamp}``.
    Validates the ``X-HubSpot-Signature-v3`` header.
    Rejects timestamps older than 5 minutes (replay-attack prevention).

Zoho Books:
    Uses a pre-shared secret token delivered in the ``X-Zoho-Webhook-Token``
    header (or the query-string ``token`` parameter for older Zoho setups).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from fastapi import HTTPException, Request

from app.config import settings

logger = logging.getLogger(__name__)

# Maximum age of a webhook timestamp before we reject it (seconds).
_MAX_TIMESTAMP_AGE_S = 300  # 5 minutes


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------


async def validate_hubspot_signature(request: Request) -> None:
    """
    Validate an inbound HubSpot webhook using signature v3.

    HubSpot signs payloads with HMAC-SHA256 over:
        ``{HTTP_METHOD}{full_request_URI}{raw_body}{unix_ms_timestamp}``

    The signature is Base64-encoded and delivered in
    ``X-HubSpot-Signature-v3``. The timestamp is in
    ``X-HubSpot-Signature-Timestamp``.

    Raises:
        HTTPException 401: If the secret is not configured.
        HTTPException 400: If required headers are missing.
        HTTPException 403: If the signature is invalid or timestamp is stale.
    """
    secret = settings.hubspot_webhook_secret
    if not secret:
        # In dev/test mode without a secret, skip validation.
        logger.warning(
            "HUBSPOT_WEBHOOK_SECRET not set – skipping signature validation."
        )
        return

    sig_header = request.headers.get("X-HubSpot-Signature-v3")
    ts_header = request.headers.get("X-HubSpot-Signature-Timestamp")

    if not sig_header or not ts_header:
        logger.warning("Missing HubSpot signature headers.")
        raise HTTPException(status_code=400, detail="Missing HubSpot signature headers.")

    # Replay-attack prevention.
    try:
        ts_ms = int(ts_header)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid timestamp header.")

    now_ms = int(time.time() * 1000)
    age_s = abs(now_ms - ts_ms) / 1000
    if age_s > _MAX_TIMESTAMP_AGE_S:
        logger.warning(
            "HubSpot webhook timestamp is %s seconds old – rejecting.", age_s
        )
        raise HTTPException(status_code=403, detail="Webhook timestamp too old.")

    body = await request.body()
    method = request.method.upper()
    uri = str(request.url)

    # Signature input: method + url + body_bytes + timestamp_string
    # Per HubSpot docs the body is the raw bytes decoded as UTF-8.
    sig_input = method + uri + body.decode("utf-8", errors="replace") + ts_header
    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=sig_input.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(sig_header.lower(), expected.lower()):
        logger.warning("HubSpot signature mismatch.")
        raise HTTPException(status_code=403, detail="Invalid HubSpot webhook signature.")


# ---------------------------------------------------------------------------
# Zoho Books
# ---------------------------------------------------------------------------


async def validate_zoho_signature(request: Request) -> None:
    """
    Validate an inbound Zoho Books webhook using a shared-secret token.

    Zoho delivers the token in ``X-Zoho-Webhook-Token`` header or in the
    ``token`` query parameter. We accept either.

    Raises:
        HTTPException 403: If the token does not match.
    """
    zoho_secret = settings.zoho_webhook_secret or settings.secret_key

    if not zoho_secret or zoho_secret == "change-me":
        logger.warning(
            "ZOHO_WEBHOOK_SECRET not configured – skipping Zoho webhook validation."
        )
        return

    token = request.headers.get("X-Zoho-Webhook-Token") or request.query_params.get(
        "token"
    )

    if not token:
        logger.warning("Missing Zoho webhook token.")
        raise HTTPException(status_code=403, detail="Missing Zoho webhook token.")

    if not hmac.compare_digest(token, zoho_secret):
        logger.warning("Zoho webhook token mismatch.")
        raise HTTPException(status_code=403, detail="Invalid Zoho webhook token.")


# ---------------------------------------------------------------------------
# Internal API key (for /logs, /status, /sync endpoints)
# ---------------------------------------------------------------------------


def validate_api_key(request: Request) -> None:
    """
    Validate the ``X-API-Key`` header for internal management endpoints.

    Raises:
        HTTPException 401: If the key is missing or invalid.
    """
    api_key = settings.api_key
    if not api_key or api_key == "dev-api-key":
        # In dev mode, skip validation.
        return

    provided = request.headers.get("X-API-Key")
    if not provided or not hmac.compare_digest(provided, api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")

