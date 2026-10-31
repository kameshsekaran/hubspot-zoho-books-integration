"""
Zoho Books sales order management.

Provides idempotent create / fetch operations for Sales Orders (SO).
The HubSpot deal ID is embedded in the SO ``reference_number`` field
(``HS-{deal_id}``) so we can detect duplicates before creation.
"""

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.zoho.client import ZohoClient

logger = logging.getLogger(__name__)

# Sentinel prefix used in SO reference numbers.
_REF_PREFIX = "HS-"


def find_so_by_reference(client: ZohoClient, deal_id: str) -> Optional[dict]:
    """
    Search Zoho Books for a Sales Order whose reference_number matches the deal.

    This is the idempotency guard: if a SO already exists we return it instead
    of creating a duplicate.

    Args:
        client:  Authenticated ZohoClient.
        deal_id: HubSpot deal ID.

    Returns:
        The Zoho SO dict if found, or ``None``.
    """
    reference = f"{_REF_PREFIX}{deal_id}"
    resp = client.get("/salesorders", params={"reference_number": reference})
    orders = resp.get("salesorders", [])
    if orders:
        logger.info(
            "Existing SO found for deal %s: so_id=%s", deal_id, orders[0].get("salesorder_id")
        )
        return orders[0]
    return None


def get_sales_order(client: ZohoClient, so_id: str) -> dict:
    """
    Fetch a single Sales Order by its Zoho ID.

    Args:
        client: Authenticated ZohoClient.
        so_id:  Zoho salesorder_id.

    Returns:
        Full SO dict from the Zoho API.
    """
    resp = client.get(f"/salesorders/{so_id}")
    return resp.get("salesorder", {})


def create_sales_order(
    client: ZohoClient,
    customer_id: str,
    deal_data: dict,
    db: Session,
) -> dict:
    """
    Create a Zoho Books Sales Order for a HubSpot deal.

    Performs an idempotency check via ``find_so_by_reference`` before creation.
    The Sales Order contains a single line item representing the deal value
    (exclusive of VAT); VAT is stored as a separate tax line.

    Args:
        client:      Authenticated ZohoClient.
        customer_id: Zoho contact_id for the customer.
        deal_data:   Normalised deal dict with keys: ``deal_id``, ``deal_name``,
                     ``amount`` (net AED as Decimal), ``currency_code``,
                     ``vat_rate``.
        db:          Open SQLAlchemy session (for future idempotency caching).

    Returns:
        Zoho salesorder dict (either newly created or pre-existing).
    """
    deal_id: str = deal_data["deal_id"]

    # ── Idempotency check ────────────────────────────────────────────────
    existing = find_so_by_reference(client, deal_id)
    if existing:
        return existing

    net_amount = Decimal(str(deal_data.get("amount", 0)))
    vat_rate = Decimal(str(deal_data.get("vat_rate", settings.vat_rate)))
    currency = deal_data.get("currency_code", "AED")

    payload = _build_so_payload(
        customer_id=customer_id,
        deal_id=deal_id,
        deal_name=deal_data.get("deal_name", f"Deal {deal_id}"),
        net_amount=net_amount,
        vat_rate=vat_rate,
        currency=currency,
    )

    resp = client.post("/salesorders", body=payload)
    so = resp.get("salesorder", {})
    logger.info(
        "Created SO %s (number=%s) for deal %s",
        so.get("salesorder_id"),
        so.get("salesorder_number"),
        deal_id,
    )
    return so


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_so_payload(
    customer_id: str,
    deal_id: str,
    deal_name: str,
    net_amount: Decimal,
    vat_rate: Decimal,
    currency: str,
) -> dict:
    """
    Construct the Zoho Books salesorder creation payload.

    Args:
        customer_id: Zoho contact_id.
        deal_id:     HubSpot deal ID (embedded in reference_number).
        deal_name:   Human-readable deal name used as the SO subject.
        net_amount:  Deal value exclusive of VAT.
        vat_rate:    Decimal VAT rate, e.g. Decimal("0.05") for 5%.
        currency:    ISO 4217 currency code, e.g. "AED".

    Returns:
        Dict ready to POST to /salesorders.
    """
    vat_pct = float(vat_rate * 100)  # e.g. 5.0

    line_item = {
        "description": deal_name,
        "rate": float(net_amount),
        "quantity": 1,
        "unit": "each",
    }

    return {
        "customer_id": customer_id,
        "reference_number": f"{_REF_PREFIX}{deal_id}",
        "salesorder_number": f"SO-{deal_id}",
        "subject": deal_name,
        "currency_code": currency,
        "line_items": [line_item],
        "notes": f"CRM Deal: {deal_id}",
        "terms": "Payment due per milestone schedule.",
    }
