"""
Zoho Books → CRM sync: pull invoice/payment state into HubSpot deal properties.

Only properties that have actually changed since the last sync are written
to HubSpot to minimise API usage and avoid unnecessary audit noise.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.crm.client import HubSpotClient
from app.models.link import DealLink
from app.sync.milestone import compute_invoicing_summary
from app.zoho.client import ZohoClient
from app.zoho.invoices import get_invoice_payments, list_invoices_for_so
from app.zoho.sales_orders import get_sales_order

logger = logging.getLogger(__name__)


async def sync_deal_status(deal_id: str, db: Session) -> dict:
    """
    Pull current invoice and payment state from Zoho Books and write any
    changes back to the HubSpot deal.

    Only fields whose values have changed since the last sync are PATCHed to
    HubSpot, avoiding unnecessary API calls.

    Args:
        deal_id: HubSpot deal ID.
        db:      Open SQLAlchemy session.

    Returns:
        Dict with sync results::

            {
                "deal_id": str,
                "updated_properties": dict,   # only changed fields
                "no_changes": bool,
                "error": Optional[str],
            }
    """
    link: Optional[DealLink] = (
        db.query(DealLink).filter(DealLink.hs_deal_id == deal_id).first()
    )
    if link is None or not link.zoho_so_id:
        logger.debug("No DealLink/SO for deal %s – skipping zoho→crm sync.", deal_id)
        return {"deal_id": deal_id, "no_changes": True, "updated_properties": {}, "error": None}

    zoho_client = ZohoClient(db, deal_id=deal_id, direction="zoho_to_crm")
    hs_client = HubSpotClient(db, deal_id=deal_id, direction="zoho_to_crm")

    try:
        # ── Fetch SO + invoices ──────────────────────────────────────────
        so = get_sales_order(zoho_client, link.zoho_so_id)
        so_total = Decimal(str(so.get("total", 0)))

        invoices = list_invoices_for_so(zoho_client, link.zoho_so_id)
        # Fetch full invoice details with balance
        detailed_invoices = []
        for inv_summary in invoices:
            inv_id = inv_summary.get("invoice_id")
            if inv_id:
                from app.zoho.invoices import get_invoice
                full_inv = get_invoice(zoho_client, inv_id)
                detailed_invoices.append(full_inv)
            else:
                detailed_invoices.append(inv_summary)

        # ── Compute summary ──────────────────────────────────────────────
        summary = compute_invoicing_summary(detailed_invoices, so_total)

        # ── Collect payment IDs ──────────────────────────────────────────
        all_payment_ids: list[str] = []
        for inv in detailed_invoices:
            inv_id = inv.get("invoice_id")
            if inv_id:
                payments = get_invoice_payments(zoho_client, inv_id)
                for p in payments:
                    pid = p.get("payment_id")
                    if pid:
                        all_payment_ids.append(str(pid))

        # Update payment IDs in link
        link.set_payment_ids(all_payment_ids)
        db.commit()

        # ── Build HubSpot properties dict ────────────────────────────────
        invoice_ids = link.get_invoice_ids()
        new_props = _build_hubspot_properties(summary, invoice_ids, link)

        # ── Diff against last sync state ─────────────────────────────────
        last_state = link.get_sync_state()
        changed = {k: v for k, v in new_props.items() if str(last_state.get(k)) != str(v)}

        if not changed:
            logger.debug("No property changes for deal %s – skipping HubSpot update.", deal_id)
            return {
                "deal_id": deal_id,
                "no_changes": True,
                "updated_properties": {},
                "error": None,
            }

        # ── PATCH HubSpot (changed fields only) ─────────────────────────
        hs_client.update_deal_properties(deal_id, changed)

        # ── Persist new sync state ───────────────────────────────────────
        link.set_sync_state(new_props)
        db.commit()

        logger.info(
            "zoho→crm sync for deal %s: updated %d properties: %s",
            deal_id,
            len(changed),
            list(changed.keys()),
        )
        return {
            "deal_id": deal_id,
            "no_changes": False,
            "updated_properties": changed,
            "error": None,
        }

    except Exception as exc:
        logger.error("zoho→crm sync failed for deal %s: %s", deal_id, exc, exc_info=True)
        # Write error status back to HubSpot.
        try:
            hs_client.update_deal_properties(
                deal_id,
                {
                    "zoho_sync_status": "error",
                    "zoho_sync_error": str(exc)[:500],
                },
            )
        except Exception:
            pass
        return {"deal_id": deal_id, "no_changes": True, "updated_properties": {}, "error": str(exc)}


async def sync_all_deals(db: Session) -> dict:
    """
    Sync all HubSpot deals that have a Zoho Books link.

    Designed to run every 15 minutes from the APScheduler job.  Individual
    deal failures are logged but do not abort the rest of the batch.

    Args:
        db: Open SQLAlchemy session.

    Returns:
        Dict with ``total``, ``updated``, ``errors`` counts.
    """
    links: list[DealLink] = db.query(DealLink).filter(DealLink.zoho_so_id != None).all()
    total = len(links)
    updated = 0
    errors = 0

    logger.info("sync_all_deals: processing %d deal(s).", total)

    for link in links:
        try:
            result = await sync_deal_status(link.hs_deal_id, db)
            if not result.get("no_changes"):
                updated += 1
            if result.get("error"):
                errors += 1
        except Exception as exc:
            logger.error("sync_all_deals: unexpected error for deal %s: %s", link.hs_deal_id, exc)
            errors += 1

    logger.info(
        "sync_all_deals complete: total=%d updated=%d errors=%d", total, updated, errors
    )
    return {"total": total, "updated": updated, "errors": errors}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_hubspot_properties(
    summary: dict, invoice_ids: list[str], link: DealLink
) -> dict:
    """
    Convert a computed invoicing summary into a HubSpot-compatible properties dict.

    Args:
        summary:     Output of ``compute_invoicing_summary``.
        invoice_ids: List of Zoho invoice IDs.
        link:        DealLink row (for last-synced timestamp).

    Returns:
        Dict mapping HubSpot property names → string values.
    """
    props: dict[str, str] = {
        "zoho_pct_invoiced": str(float(summary["pct_invoiced"])),
        "zoho_pct_collected": str(float(summary["pct_collected"])),
        "zoho_amount_unpaid": str(float(summary["amount_unpaid"])),
        "zoho_amount_uninvoiced": str(float(summary["amount_uninvoiced"])),
        "zoho_overdue": "true" if summary["overdue"] else "false",
        "zoho_sync_status": "ok",
        "zoho_sync_error": "",
        "zoho_last_synced": datetime.utcnow().isoformat(),
        "zoho_invoice_ids": ",".join(invoice_ids),
    }

    next_due = summary.get("next_due_date")
    if next_due:
        props["zoho_next_due_date"] = next_due.isoformat()
    else:
        props["zoho_next_due_date"] = ""

    return props
