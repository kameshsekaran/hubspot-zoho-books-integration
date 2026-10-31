"""
Zoho Books invoice management.

Creates milestone-based invoices tied to a Sales Order, and provides helpers
to fetch invoice details and associated payments.

Milestone schedule (hard-coded per business rules):
  50% on Signing
  30% at Go-live
  20% at 90-day post go-live
"""

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.zoho.client import ZohoClient

logger = logging.getLogger(__name__)

# Milestone definitions: (percentage_of_net, label)
MILESTONES: list[tuple[Decimal, str]] = [
    (Decimal("0.50"), "Signing"),
    (Decimal("0.30"), "Go-live"),
    (Decimal("0.20"), "90-day post go-live"),
]

# Sentinel prefix used in invoice reference numbers for idempotency.
_INV_REF_PREFIX = "HS-INV"


def get_invoice(client: ZohoClient, invoice_id: str) -> dict:
    """
    Fetch a single Zoho Books invoice by ID.

    Args:
        client:     Authenticated ZohoClient.
        invoice_id: Zoho invoice_id string.

    Returns:
        Invoice dict from the Zoho API.
    """
    resp = client.get(f"/invoices/{invoice_id}")
    return resp.get("invoice", {})


def list_invoices_for_so(client: ZohoClient, so_id: str) -> list[dict]:
    """
    Return all invoices associated with the given Sales Order.

    Args:
        client: Authenticated ZohoClient.
        so_id:  Zoho salesorder_id.

    Returns:
        List of invoice summary dicts.
    """
    resp = client.get("/invoices", params={"salesorder_id": so_id})
    return resp.get("invoices", [])


def get_invoice_payments(client: ZohoClient, invoice_id: str) -> list[dict]:
    """
    Fetch all payment records applied to an invoice.

    Args:
        client:     Authenticated ZohoClient.
        invoice_id: Zoho invoice_id string.

    Returns:
        List of payment dicts.
    """
    resp = client.get(f"/invoices/{invoice_id}/payments")
    return resp.get("payments", [])


def find_invoice_by_reference(
    client: ZohoClient, reference: str
) -> Optional[dict]:
    """
    Search for an invoice with the given reference_number (idempotency check).

    Args:
        client:    Authenticated ZohoClient.
        reference: Expected reference_number string.

    Returns:
        Invoice summary dict if found, else None.
    """
    resp = client.get("/invoices", params={"reference_number": reference})
    invoices = resp.get("invoices", [])
    return invoices[0] if invoices else None


def create_milestone_invoices(
    client: ZohoClient,
    so_id: str,
    so_total: Decimal,
    deal_id: str,
    customer_id: str,
    db: Session,
) -> list[dict]:
    """
    Create (or retrieve existing) milestone invoices for a Sales Order.

    Three invoices are created: 50% at Signing, 30% at Go-live, 20% at 90 days.
    Each invoice is idempotent: if an invoice with the same reference number
    already exists it is returned as-is without creating a duplicate.

    Args:
        client:      Authenticated ZohoClient.
        so_id:       Zoho salesorder_id these invoices belong to.
        so_total:    Net total (exclusive of VAT) of the Sales Order as Decimal.
        deal_id:     HubSpot deal ID (embedded in reference numbers).
        customer_id: Zoho contact_id for the customer.
        db:          Open SQLAlchemy session.

    Returns:
        List of three invoice dicts in milestone order.
    """
    vat_rate = Decimal(str(settings.vat_rate))
    created_invoices: list[dict] = []

    for idx, (pct, label) in enumerate(MILESTONES, start=1):
        reference = f"{_INV_REF_PREFIX}-{deal_id}-{idx}"

        # ── Idempotency check ────────────────────────────────────────────
        existing = find_invoice_by_reference(client, reference)
        if existing:
            logger.info(
                "Invoice %s already exists (id=%s) – skipping creation.",
                reference,
                existing.get("invoice_id"),
            )
            created_invoices.append(existing)
            continue

        net = (so_total * pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vat = (net * vat_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gross = net + vat

        payload = _build_invoice_payload(
            customer_id=customer_id,
            so_id=so_id,
            deal_id=deal_id,
            reference=reference,
            net=net,
            vat=vat,
            gross=gross,
            label=label,
            milestone_index=idx,
        )

        resp = client.post("/invoices", body=payload)
        inv = resp.get("invoice", {})
        logger.info(
            "Created milestone invoice %d/%d for deal %s: id=%s net=%s vat=%s",
            idx,
            len(MILESTONES),
            deal_id,
            inv.get("invoice_id"),
            net,
            vat,
        )
        created_invoices.append(inv)

    return created_invoices


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_invoice_payload(
    customer_id: str,
    so_id: str,
    deal_id: str,
    reference: str,
    net: Decimal,
    vat: Decimal,
    gross: Decimal,
    label: str,
    milestone_index: int,
) -> dict:
    """
    Build the Zoho Books invoice creation payload for a single milestone.

    Args:
        customer_id:     Zoho contact_id.
        so_id:           Zoho salesorder_id.
        deal_id:         HubSpot deal ID.
        reference:       Invoice reference number (used for idempotency).
        net:             Net amount (excl. VAT) as Decimal.
        vat:             VAT amount as Decimal.
        gross:           Total amount (net + VAT) as Decimal.
        label:           Human-readable milestone label.
        milestone_index: 1-based index.

    Returns:
        Dict ready to POST to /invoices.
    """
    line_item = {
        "description": f"Milestone {milestone_index}: {label}",
        "rate": float(net),
        "quantity": 1,
        "unit": "each",
    }

    return {
        "customer_id": customer_id,
        "salesorder_id": so_id,
        "reference_number": reference,
        "line_items": [line_item],
        "notes": f"CRM Deal: {deal_id} | Milestone {milestone_index}: {label}",
        "terms": f"Due on {label}.",
        "is_inclusive_tax": False,
    }
