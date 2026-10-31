"""
Core synchronisation engine.

Orchestrates the full CRM → Zoho and Zoho → CRM sync workflows:

1. ``sync_closed_won_deal``: Handle a newly closed deal (HubSpot → Zoho Books).
   - Fetch deal, company, contact from HubSpot.
   - Find or create customer in Zoho Books.
   - Create Sales Order.
   - Create 3 milestone invoices.
   - Write Zoho IDs + computed metrics back to HubSpot.
   - Update deal_links DB row.

2. ``sync_zoho_event``: Handle a Zoho invoice/payment webhook event.
   - Look up deal from invoice ID in deal_links.
   - Fetch all invoice statuses from Zoho.
   - Compute financial metrics.
   - Patch HubSpot deal if values changed (change detection).

3. ``compute_metrics``: Pure function — calculate all derived metrics from
   a list of invoice dicts.

4. Edge-case handlers: deal re-opened, amount drift, duplicate events.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.hubspot_client import HubSpotClient
from app.models.link import DealLink
from app.zoho.books import (
    create_customer,
    create_milestone_invoices,
    create_sales_order,
    find_customer_by_email,
    get_invoice,
    get_sales_order,
    void_invoice,
)
from app.zoho.client import ZohoClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_closed_won_deal(deal_id: str, db: Session) -> dict[str, Any]:
    """
    Full CRM → Zoho Books provisioning for a newly closed deal.

    Idempotent: if a ``DealLink`` row already has ``zoho_so_id`` set,
    creation steps are skipped and only the metric sync is performed.

    Args:
        deal_id: HubSpot deal object ID.
        db:      Open SQLAlchemy session.

    Returns:
        Summary dict with created/updated Zoho IDs and computed metrics.

    Raises:
        Any exception from HubSpotClient or ZohoClient on unrecoverable errors.
    """
    hs = HubSpotClient(db, deal_id=deal_id, direction="crm_to_zoho")
    zoho = ZohoClient(db, deal_id=deal_id, direction="crm_to_zoho")

    # ── 1. Fetch deal data from HubSpot ──────────────────────────────────────
    deal_resp = hs.get_deal(deal_id)
    props = deal_resp.get("properties", {})
    associations = deal_resp.get("associations", {})

    amount_str = props.get("amount") or "0"
    try:
        deal_amount = float(amount_str)
    except (ValueError, TypeError):
        deal_amount = 0.0

    close_date = _parse_hs_date(props.get("closedate")) or date.today()
    currency = props.get("deal_currency_code") or "AED"
    deal_name = props.get("dealname") or f"Deal {deal_id}"
    owner_id = props.get("hubspot_owner_id")

    # Fetch company name
    company_name = "Unknown Company"
    company_ids = _extract_association_ids(associations, "companies")
    if company_ids:
        try:
            co_resp = hs.get_company(company_ids[0])
            company_name = co_resp.get("properties", {}).get("name") or company_name
        except Exception:
            logger.warning("Could not fetch company %s", company_ids[0])

    # Fetch contact email
    contact_email = ""
    contact_phone = ""
    contact_ids = _extract_association_ids(associations, "contacts")
    if contact_ids:
        try:
            ct_resp = hs.get_contact(contact_ids[0])
            ct_props = ct_resp.get("properties", {})
            contact_email = ct_props.get("email") or ""
            contact_phone = ct_props.get("phone") or ct_props.get("mobilephone") or ""
        except Exception:
            logger.warning("Could not fetch contact %s", contact_ids[0])

    if not contact_email:
        contact_email = f"deal-{deal_id}@placeholder.example"
        logger.warning(
            "No contact email for deal %s – using placeholder.", deal_id
        )

    # Fetch owner name
    salesperson_name = "Sales"
    if owner_id:
        try:
            owner_resp = hs.get_owner(owner_id)
            first = owner_resp.get("firstName") or ""
            last = owner_resp.get("lastName") or ""
            salesperson_name = f"{first} {last}".strip() or "Sales"
        except Exception:
            logger.warning("Could not fetch owner %s", owner_id)

    # ── 2. Idempotency check ──────────────────────────────────────────────────
    link = _get_or_create_link(db, deal_id)

    # ── 3. Find / create Zoho customer ───────────────────────────────────────
    if not link.zoho_customer_id:
        existing = find_customer_by_email(zoho, contact_email)
        if existing:
            customer_id = existing["contact_id"]
            logger.info(
                "Reusing existing Zoho customer id=%s for email=%s",
                customer_id,
                contact_email,
            )
        else:
            customer = create_customer(
                zoho,
                display_name=company_name,
                email=contact_email,
                phone=contact_phone or None,
                currency_code=currency,
            )
            customer_id = customer["contact_id"]
            logger.info("Created Zoho customer id=%s", customer_id)

        link.zoho_customer_id = customer_id
        db.commit()
    else:
        customer_id = link.zoho_customer_id

    # ── 4. Create Sales Order (idempotent) ────────────────────────────────────
    if not link.zoho_so_id:
        so = create_sales_order(
            zoho,
            customer_id=customer_id,
            reference_number=f"HS-{deal_id}",
            order_date=close_date,
            currency_code=currency,
            salesperson_name=salesperson_name,
            line_item_name=deal_name,
            line_item_rate=deal_amount,
            notes=deal_name,
        )
        so_id = so["salesorder_id"]
        so_number = so.get("salesorder_number", "")
        so_total = float(so.get("total", deal_amount))
        link.zoho_so_id = so_id
        link.zoho_so_number = so_number
        db.commit()
        logger.info("Created Zoho SO id=%s number=%s", so_id, so_number)
    else:
        so_id = link.zoho_so_id
        so_number = link.zoho_so_number or ""
        so_resp = get_sales_order(zoho, so_id)
        so_total = float(so_resp.get("total", deal_amount))

    # ── 5. Create milestone invoices (idempotent) ─────────────────────────────
    existing_inv_ids = link.get_invoice_ids()
    if not existing_inv_ids:
        invoices = create_milestone_invoices(
            zoho,
            customer_id=customer_id,
            salesorder_id=so_id,
            so_reference=f"HS-{deal_id}",
            deal_amount=deal_amount,
            close_date=close_date,
        )
        invoice_ids = [inv["invoice_id"] for inv in invoices]
        link.set_invoice_ids(invoice_ids)
        db.commit()
    else:
        invoice_ids = existing_inv_ids

    # ── 6. Compute and write metrics back to HubSpot ──────────────────────────
    invoice_details = [get_invoice(zoho, inv_id) for inv_id in invoice_ids]
    metrics = compute_metrics(invoice_details, so_total)

    hs_props: dict[str, Any] = {
        "zoho_so_id": so_id,
        "zoho_so_number": so_number,
        "zoho_customer_id": customer_id,
        "zoho_invoice_ids": ",".join(invoice_ids),
        "zoho_so_total": str(so_total),
        **_metrics_to_hs_props(metrics),
        "zoho_sync_status": "synced",
        "zoho_sync_error": "",
        "zoho_last_synced": datetime.now(tz=timezone.utc).isoformat(),
    }
    hs.patch_deal(deal_id, hs_props)

    link.set_sync_state(metrics)
    db.commit()

    logger.info("sync_closed_won_deal complete for deal_id=%s", deal_id)
    return {
        "deal_id": deal_id,
        "zoho_so_id": so_id,
        "zoho_so_number": so_number,
        "zoho_customer_id": customer_id,
        "invoice_ids": invoice_ids,
        "metrics": metrics,
    }


def sync_zoho_event(
    db: Session,
    *,
    invoice_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    so_id: Optional[str] = None,
    event_type: str = "unknown",
) -> Optional[dict[str, Any]]:
    """
    Handle a Zoho Books webhook event (invoice or payment change).

    Looks up the affected deal via ``invoice_id`` or ``so_id`` in
    ``deal_links``, then re-computes all financial metrics and patches
    HubSpot only if values changed.

    Args:
        db:         Open SQLAlchemy session.
        invoice_id: Zoho invoice ID from the webhook payload.
        payment_id: Zoho payment ID from the webhook payload.
        so_id:      Zoho SO ID (alternative lookup key).
        event_type: Human-readable event label for logging.

    Returns:
        Summary dict, or None if no matching deal was found.
    """
    # ── Find the deal link ────────────────────────────────────────────────────
    link: Optional[DealLink] = None

    if invoice_id:
        # Search all links whose invoice_ids JSON contains this invoice_id.
        from sqlalchemy import text as sa_text
        links = db.query(DealLink).filter(
            DealLink.zoho_invoice_ids.like(f"%{invoice_id}%")
        ).all()
        # Filter precisely (the LIKE can have false positives for substrings).
        for candidate in links:
            if invoice_id in candidate.get_invoice_ids():
                link = candidate
                break

    if link is None and so_id:
        link = db.query(DealLink).filter(DealLink.zoho_so_id == so_id).first()

    if link is None:
        logger.warning(
            "sync_zoho_event: no deal found for invoice_id=%s so_id=%s",
            invoice_id,
            so_id,
        )
        return None

    deal_id = link.hs_deal_id
    hs = HubSpotClient(db, deal_id=deal_id, direction="zoho_to_crm")
    zoho = ZohoClient(db, deal_id=deal_id, direction="zoho_to_crm")

    # ── Handle invoice void + new invoice events ──────────────────────────────
    if event_type == "invoice.created" and invoice_id:
        _handle_new_invoice(link, zoho, invoice_id, db)

    if event_type in ("invoice.status_changed", "invoice.void") and invoice_id:
        inv_detail = get_invoice(zoho, invoice_id)
        if inv_detail.get("status") == "void":
            _handle_void_invoice(link, invoice_id, db)

    # ── Fetch all invoice states and recompute metrics ────────────────────────
    inv_ids = link.get_invoice_ids()
    if not inv_ids:
        logger.info("Deal %s has no invoices in deal_links; skipping.", deal_id)
        return None

    so_resp = get_sales_order(zoho, link.zoho_so_id)
    so_total = float(so_resp.get("total", 0))

    invoice_details = [get_invoice(zoho, iid) for iid in inv_ids]
    metrics = compute_metrics(invoice_details, so_total)

    # ── Change detection ──────────────────────────────────────────────────────
    last_state = link.get_sync_state()
    if _no_change(last_state, metrics):
        logger.info(
            "No metric change detected for deal %s – skipping HubSpot write.",
            deal_id,
        )
        return {"deal_id": deal_id, "changed": False}

    # ── Patch HubSpot ─────────────────────────────────────────────────────────
    hs_props: dict[str, Any] = {
        **_metrics_to_hs_props(metrics),
        "zoho_invoice_ids": ",".join(inv_ids),
        "zoho_sync_status": "synced",
        "zoho_sync_error": "",
        "zoho_last_synced": datetime.now(tz=timezone.utc).isoformat(),
    }
    hs.patch_deal(deal_id, hs_props)

    link.set_sync_state(metrics)
    db.commit()

    logger.info(
        "sync_zoho_event complete for deal_id=%s event=%s", deal_id, event_type
    )
    return {"deal_id": deal_id, "changed": True, "metrics": metrics}


def handle_deal_reopened(deal_id: str, db: Session) -> dict[str, Any]:
    """
    Handle a HubSpot deal stage change away from Closed Won.

    - Auto-voids any draft invoices.
    - Flags the deal for manual review if sent invoices exist.
    - Updates HubSpot deal properties accordingly.

    Args:
        deal_id: HubSpot deal object ID.
        db:      Open SQLAlchemy session.

    Returns:
        Summary dict.
    """
    link = db.query(DealLink).filter(DealLink.hs_deal_id == deal_id).first()
    if not link or not link.zoho_so_id:
        return {"deal_id": deal_id, "action": "no_zoho_data"}

    hs = HubSpotClient(db, deal_id=deal_id, direction="zoho_to_crm")
    zoho = ZohoClient(db, deal_id=deal_id, direction="zoho_to_crm")

    inv_ids = link.get_invoice_ids()
    has_sent = False
    voided = []

    for inv_id in inv_ids:
        inv = get_invoice(zoho, inv_id)
        status = inv.get("status", "")
        if status in ("draft",):
            void_invoice(zoho, inv_id)
            voided.append(inv_id)
            logger.info("Voided draft invoice %s on deal reopen.", inv_id)
        elif status in ("sent", "overdue"):
            has_sent = True
            logger.warning(
                "Deal %s reopened but invoice %s is in %s status – manual review required.",
                deal_id,
                inv_id,
                status,
            )

    # Remove voided IDs from link
    remaining = [i for i in inv_ids if i not in voided]
    link.set_invoice_ids(remaining)
    db.commit()

    if has_sent:
        hs.patch_deal(
            deal_id,
            {
                "zoho_sync_status": "needs_review",
                "zoho_sync_error": (
                    "Deal reopened after invoice was sent. Manual review required."
                ),
                "zoho_last_synced": datetime.now(tz=timezone.utc).isoformat(),
            },
        )
        return {"deal_id": deal_id, "action": "needs_review", "voided": voided}

    hs.patch_deal(
        deal_id,
        {
            "zoho_sync_status": "pending",
            "zoho_sync_error": "Deal reopened; draft invoices voided.",
            "zoho_last_synced": datetime.now(tz=timezone.utc).isoformat(),
        },
    )
    return {"deal_id": deal_id, "action": "draft_invoices_voided", "voided": voided}


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


def compute_metrics(invoices: list[dict], so_total: float) -> dict[str, Any]:
    """
    Compute all financial metrics from a list of Zoho invoice dicts.

    Pure function — no side effects.

    Metrics:
        - ``pct_invoiced``      : sum(invoice.total) / so_total × 100
        - ``pct_collected``     : sum(invoice.payment_made) / so_total × 100
        - ``amount_unpaid``     : sum(invoice.balance) for sent/overdue invoices
        - ``amount_uninvoiced`` : so_total - sum(invoice.total) for all invoices
        - ``next_due_date``     : earliest due_date among open invoices
        - ``overdue``           : any invoice has status "overdue"

    Args:
        invoices: List of Zoho invoice dicts (full detail from GET /invoices/{id}).
        so_total: Sales order total amount.

    Returns:
        Dict with all metric keys.
    """
    total_invoiced = 0.0
    total_collected = 0.0
    total_unpaid = 0.0
    next_due: Optional[date] = None
    overdue = False

    for inv in invoices:
        status = inv.get("status", "")
        if status == "void":
            continue

        inv_total = _safe_float(inv.get("total"))
        payment_made = _safe_float(inv.get("payment_made"))
        balance = _safe_float(inv.get("balance"))
        due_date_str = inv.get("due_date") or ""

        total_invoiced += inv_total
        total_collected += payment_made

        if status in ("sent", "overdue"):
            total_unpaid += balance

        if status == "overdue":
            overdue = True

        if status not in ("paid", "void") and due_date_str:
            try:
                d = date.fromisoformat(due_date_str)
                if next_due is None or d < next_due:
                    next_due = d
            except ValueError:
                pass

    pct_invoiced = (total_invoiced / so_total * 100) if so_total > 0 else 0.0
    pct_collected = (total_collected / so_total * 100) if so_total > 0 else 0.0
    amount_uninvoiced = max(0.0, so_total - total_invoiced)

    return {
        "pct_invoiced": round(pct_invoiced, 2),
        "pct_collected": round(pct_collected, 2),
        "amount_unpaid": round(total_unpaid, 2),
        "amount_uninvoiced": round(amount_uninvoiced, 2),
        "next_due_date": next_due.isoformat() if next_due else None,
        "overdue": overdue,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_or_create_link(db: Session, deal_id: str) -> DealLink:
    """Return the existing DealLink or create a new pending one."""
    link = db.query(DealLink).filter(DealLink.hs_deal_id == deal_id).first()
    if link is None:
        link = DealLink(hs_deal_id=deal_id)
        db.add(link)
        db.commit()
        db.refresh(link)
    return link


def _parse_hs_date(value: Optional[str]) -> Optional[date]:
    """Parse a HubSpot date value (epoch ms string or ISO date)."""
    if not value:
        return None
    try:
        # HubSpot stores dates as epoch ms strings
        epoch_ms = int(value)
        return date.fromtimestamp(epoch_ms / 1000)
    except (ValueError, TypeError, OSError):
        pass
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _extract_association_ids(associations: dict, assoc_type: str) -> list[str]:
    """Extract a list of object IDs from HubSpot association response."""
    assoc = associations.get(assoc_type, {})
    results = assoc.get("results", [])
    return [r["id"] for r in results if "id" in r]


def _metrics_to_hs_props(metrics: dict[str, Any]) -> dict[str, Any]:
    """Convert computed metrics dict to HubSpot property name/value pairs."""
    props: dict[str, Any] = {
        "zoho_pct_invoiced": str(metrics["pct_invoiced"]),
        "zoho_pct_collected": str(metrics["pct_collected"]),
        "zoho_amount_unpaid": str(metrics["amount_unpaid"]),
        "zoho_amount_uninvoiced": str(metrics["amount_uninvoiced"]),
        "zoho_overdue": "true" if metrics["overdue"] else "false",
    }
    if metrics.get("next_due_date"):
        props["zoho_next_due_date"] = metrics["next_due_date"]
    return props


def _no_change(last: dict, current: dict) -> bool:
    """Return True if all metric values are unchanged from the last sync."""
    if not last:
        return False
    for key in ("pct_invoiced", "pct_collected", "amount_unpaid", "amount_uninvoiced", "overdue", "next_due_date"):
        if last.get(key) != current.get(key):
            return False
    return True


def _safe_float(value: Any) -> float:
    """Convert an arbitrary value to float safely."""
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def _handle_new_invoice(
    link: DealLink, zoho: ZohoClient, invoice_id: str, db: Session
) -> None:
    """Add a newly created Zoho invoice to the deal_links record."""
    inv_ids = link.get_invoice_ids()
    if invoice_id not in inv_ids:
        inv_ids.append(invoice_id)
        link.set_invoice_ids(inv_ids)
        db.commit()
        logger.info(
            "Added new invoice %s to deal_links for deal %s",
            invoice_id,
            link.hs_deal_id,
        )


def _handle_void_invoice(link: DealLink, invoice_id: str, db: Session) -> None:
    """Remove a voided invoice from the deal_links record."""
    inv_ids = link.get_invoice_ids()
    if invoice_id in inv_ids:
        inv_ids.remove(invoice_id)
        link.set_invoice_ids(inv_ids)
        db.commit()
        logger.info(
            "Removed voided invoice %s from deal_links for deal %s",
            invoice_id,
            link.hs_deal_id,
        )

