"""
CRM → Zoho Books sync: process a Closed-Won HubSpot deal.

Full idempotent flow:
1. Guard against duplicate processing via deal_links table.
2. Fetch deal, company, and contact from HubSpot.
3. Find or create the customer in Zoho Books.
4. Create the Sales Order (reference_number dedup check).
5. Create 3 milestone invoices (reference_number dedup each).
6. Write Zoho IDs back to the HubSpot deal.
7. Persist the DealLink row.
8. Add a note to the HubSpot deal.
"""

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.crm.client import HubSpotClient
from app.models.link import DealLink
from app.zoho.auth import get_valid_token
from app.zoho.client import ZohoClient
from app.zoho.customers import find_or_create_customer
from app.zoho.invoices import create_milestone_invoices
from app.zoho.sales_orders import create_sales_order

logger = logging.getLogger(__name__)


async def process_closed_won_deal(deal_id: str, db: Session) -> dict:
    """
    Fully idempotent CRM→Zoho sync for a single Closed-Won deal.

    Creates (or retrieves existing) Zoho Books objects and writes their IDs
    back to HubSpot.  Safe to call multiple times for the same deal.

    Args:
        deal_id: HubSpot deal ID string.
        db:      Open SQLAlchemy session.

    Returns:
        Dict summarising all created/found IDs::

            {
                "deal_id": str,
                "customer_id": str,
                "customer_created": bool,
                "so_id": str,
                "so_number": str,
                "invoice_ids": list[str],
                "already_existed": bool,
            }

    Raises:
        Exception: Re-raises any unexpected error after logging.
    """
    # ── Step 1: Idempotency guard ────────────────────────────────────────
    existing_link: Optional[DealLink] = (
        db.query(DealLink).filter(DealLink.hs_deal_id == deal_id).first()
    )
    if existing_link and existing_link.zoho_so_id:
        logger.info(
            "Deal %s already processed (SO=%s) – returning cached result.",
            deal_id,
            existing_link.zoho_so_id,
        )
        return {
            "deal_id": deal_id,
            "customer_id": existing_link.zoho_customer_id,
            "customer_created": False,
            "so_id": existing_link.zoho_so_id,
            "so_number": existing_link.zoho_so_number,
            "invoice_ids": existing_link.get_invoice_ids(),
            "already_existed": True,
        }

    # ── Step 2: Fetch deal data from HubSpot ────────────────────────────
    hs_client = HubSpotClient(db, deal_id=deal_id, direction="crm_to_zoho")
    deal_resp = hs_client.get_deal(deal_id)
    props = deal_resp.get("properties", {})

    company = hs_client.get_deal_company(deal_id) or {}
    contact = hs_client.get_deal_contact(deal_id) or {}

    company_props = company.get("properties", {})
    contact_props = contact.get("properties", {})

    deal_data = _build_deal_data(deal_id, props, company_props, contact_props)

    # ── Step 3: Find or create Zoho customer ────────────────────────────
    zoho_client = ZohoClient(db, deal_id=deal_id, direction="crm_to_zoho")

    customer_id, customer_created, ambiguous, candidates = find_or_create_customer(
        zoho_client, deal_data, db
    )
    if ambiguous:
        logger.warning(
            "Ambiguous customer match for deal %s (name=%s) – using first candidate.",
            deal_id,
            deal_data.get("company_name"),
        )

    # ── Step 4: Create Sales Order ───────────────────────────────────────
    so = create_sales_order(zoho_client, customer_id, deal_data, db)
    so_id: str = so["salesorder_id"]
    so_number: str = so.get("salesorder_number", "")

    # ── Step 5: Create milestone invoices ────────────────────────────────
    net_total = deal_data["amount"]
    invoices = create_milestone_invoices(
        zoho_client, so_id, net_total, deal_id, customer_id, db
    )
    invoice_ids = [inv["invoice_id"] for inv in invoices if inv.get("invoice_id")]

    # ── Step 6: Persist DealLink ─────────────────────────────────────────
    if existing_link is None:
        existing_link = DealLink(hs_deal_id=deal_id)
        db.add(existing_link)

    existing_link.zoho_customer_id = customer_id
    existing_link.zoho_so_id = so_id
    existing_link.zoho_so_number = so_number
    existing_link.set_invoice_ids(invoice_ids)
    db.commit()

    # ── Step 7: Write Zoho IDs back to HubSpot ──────────────────────────
    hubspot_props = {
        "zoho_customer_id": customer_id,
        "zoho_so_id": so_id,
        "zoho_so_number": so_number,
        "zoho_invoice_ids": json.dumps(invoice_ids),
        "zoho_sync_status": "ok",
        "zoho_sync_error": "",
        "zoho_last_synced": datetime.utcnow().isoformat(),
        "zoho_so_total": str(float(net_total)),
    }
    hs_client.update_deal_properties(deal_id, hubspot_props)

    # ── Step 8: Add a deal note ──────────────────────────────────────────
    note_body = (
        f"✅ Zoho Books integration complete.\n"
        f"Customer ID: {customer_id} (created={customer_created})\n"
        f"Sales Order: {so_number} (id={so_id})\n"
        f"Invoices: {', '.join(invoice_ids)}"
    )
    try:
        hs_client.create_deal_note(deal_id, note_body)
    except Exception as note_exc:
        logger.warning("Failed to add deal note for %s: %s", deal_id, note_exc)

    logger.info(
        "CRM→Zoho sync complete for deal %s: SO=%s, invoices=%s",
        deal_id,
        so_id,
        invoice_ids,
    )

    return {
        "deal_id": deal_id,
        "customer_id": customer_id,
        "customer_created": customer_created,
        "so_id": so_id,
        "so_number": so_number,
        "invoice_ids": invoice_ids,
        "already_existed": False,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_deal_data(
    deal_id: str,
    props: dict,
    company_props: dict,
    contact_props: dict,
) -> dict:
    """
    Assemble a normalised deal data dict from raw HubSpot API response dicts.

    Args:
        deal_id:       HubSpot deal ID.
        props:         Deal properties dict.
        company_props: Associated company properties dict.
        contact_props: Associated contact properties dict.

    Returns:
        Normalised dict with keys: ``deal_id``, ``deal_name``, ``amount``,
        ``currency_code``, ``vat_rate``, ``company_name``, ``contact_name``,
        ``contact_email``, ``contact_phone``, ``billing_address``.
    """
    # Amount from HubSpot is a string; convert to Decimal.
    raw_amount = props.get("amount") or "0"
    try:
        net_amount = Decimal(str(raw_amount))
    except Exception:
        net_amount = Decimal("0")

    first = contact_props.get("firstname", "")
    last = contact_props.get("lastname", "")
    contact_name = f"{first} {last}".strip() or None

    billing_address: Optional[dict] = None
    if company_props.get("address"):
        billing_address = {
            "address": company_props.get("address", ""),
            "city": company_props.get("city", ""),
            "country": company_props.get("country", ""),
            "zip": company_props.get("zip", ""),
        }

    return {
        "deal_id": deal_id,
        "deal_name": props.get("dealname") or f"Deal {deal_id}",
        "amount": net_amount,
        "currency_code": "AED",
        "vat_rate": Decimal(str(settings.vat_rate)),
        "company_name": company_props.get("name") or props.get("dealname") or f"Company-{deal_id}",
        "contact_name": contact_name,
        "contact_email": contact_props.get("email"),
        "contact_phone": contact_props.get("phone"),
        "billing_address": billing_address,
    }
