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

    Uses HMAC-SHA256 over:
        HTTP_METHOD + decoded_URL + raw_body + request_timestamp

    The expected signature is Base64 encoded.
    """
    import base64

    secret = settings.hubspot_webhook_secret
    if not secret:
        logger.error("HUBSPOT_WEBHOOK_SECRET is not configured.")
        raise HTTPException(
            status_code=503,
            detail="Webhook signature validation is not configured."
        )

    signature = request.headers.get("X-HubSpot-Signature-v3")
    timestamp = request.headers.get("X-HubSpot-Request-Timestamp")

    if not signature or not timestamp:
        raise HTTPException(
            status_code=400,
            detail="Missing HubSpot signature headers."
        )

    try:
        timestamp_ms = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid timestamp header.")

    now_ms = int(time.time() * 1000)
    age_seconds = abs(now_ms - timestamp_ms) / 1000

    if age_seconds > _MAX_TIMESTAMP_AGE_S:
        raise HTTPException(
            status_code=403,
            detail="Webhook timestamp too old."
        )

    body = await request.body()
    method = request.method.upper()
    uri = str(request.url)
    import re
    uri = re.sub(
        r"%(3A|2F|3F|40|21|24|27|28|29|2A|2C|3B)",
        lambda match: bytes.fromhex(match.group(1)).decode("ascii"),
        uri,
        flags=re.IGNORECASE
    )

    signature_input = (
        method + uri + body.decode("utf-8") + timestamp
    )

    digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=signature_input.encode("utf-8"),
        digestmod=hashlib.sha256
    ).digest()

    expected_signature = base64.b64encode(digest).decode("utf-8")

    if not hmac.compare_digest(signature, expected_signature):
        logger.warning("HubSpot signature mismatch.")
        raise HTTPException(
            status_code=403,
            detail="Invalid HubSpot webhook signature."
        )


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

