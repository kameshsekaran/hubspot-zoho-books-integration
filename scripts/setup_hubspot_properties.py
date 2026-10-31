"""
Setup script: Create all custom HubSpot deal properties for the Zoho integration.

Run once after creating the HubSpot Private App:

    python scripts/setup_hubspot_properties.py

Idempotent — safe to re-run if properties already exist.

Properties created (all in the ``zoho_integration`` group):
    zoho_so_id, zoho_so_number, zoho_customer_id, zoho_invoice_ids,
    zoho_so_total, zoho_pct_invoiced, zoho_pct_collected, zoho_amount_unpaid,
    zoho_amount_uninvoiced, zoho_next_due_date, zoho_overdue,
    zoho_sync_status, zoho_sync_error, zoho_last_synced
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.database import SessionLocal
from app.hubspot_client import HubSpotClient, HubSpotPermanentError

GROUP_NAME = "zoho_integration"
GROUP_LABEL = "Zoho Books Integration"

PROPERTIES = [
    {
        "name": "zoho_so_id",
        "label": "Zoho SO ID",
        "type": "string",
        "fieldType": "text",
        "description": "Internal Zoho Books Sales Order GUID",
    },
    {
        "name": "zoho_so_number",
        "label": "Zoho SO Number",
        "type": "string",
        "fieldType": "text",
        "description": "Human-readable Zoho Sales Order number (e.g. SO-00042)",
    },
    {
        "name": "zoho_customer_id",
        "label": "Zoho Customer ID",
        "type": "string",
        "fieldType": "text",
        "description": "Zoho Books contact/customer GUID",
    },
    {
        "name": "zoho_invoice_ids",
        "label": "Zoho Invoice IDs",
        "type": "string",
        "fieldType": "text",
        "description": "Comma-separated list of Zoho invoice GUIDs",
    },
    {
        "name": "zoho_so_total",
        "label": "Zoho SO Total",
        "type": "number",
        "fieldType": "number",
        "description": "Confirmed total from Zoho Sales Order",
    },
    {
        "name": "zoho_pct_invoiced",
        "label": "% Invoiced",
        "type": "number",
        "fieldType": "number",
        "description": "Percentage of deal value invoiced (0–100)",
    },
    {
        "name": "zoho_pct_collected",
        "label": "% Collected",
        "type": "number",
        "fieldType": "number",
        "description": "Percentage of deal value collected (0–100)",
    },
    {
        "name": "zoho_amount_unpaid",
        "label": "Amount Unpaid (AED)",
        "type": "number",
        "fieldType": "number",
        "description": "Outstanding balance on all sent invoices",
    },
    {
        "name": "zoho_amount_uninvoiced",
        "label": "Amount Uninvoiced (AED)",
        "type": "number",
        "fieldType": "number",
        "description": "Deal value not yet covered by any invoice",
    },
    {
        "name": "zoho_next_due_date",
        "label": "Next Invoice Due Date",
        "type": "date",
        "fieldType": "date",
        "description": "Earliest due date across all open invoices",
    },
    {
        "name": "zoho_overdue",
        "label": "Invoice Overdue?",
        "type": "bool",
        "fieldType": "booleancheckbox",
        "description": "True if at least one invoice is overdue",
    },
    {
        "name": "zoho_sync_status",
        "label": "Zoho Sync Status",
        "type": "string",
        "fieldType": "text",
        "description": "Current sync status: pending/synced/error/paused/needs_review",
    },
    {
        "name": "zoho_sync_error",
        "label": "Zoho Sync Error",
        "type": "string",
        "fieldType": "text",
        "description": "Last error message if sync_status = error",
    },
    {
        "name": "zoho_last_synced",
        "label": "Zoho Last Synced",
        "type": "datetime",
        "fieldType": "date",
        "description": "UTC timestamp of the last successful sync",
    },
]


def main() -> None:
    if not settings.hubspot_access_token:
        print("❌ HUBSPOT_ACCESS_TOKEN is not set. Copy .env.example to .env and fill in the values.")
        sys.exit(1)

    db = SessionLocal()
    hs = HubSpotClient(db)

    # ── 1. Create property group ──────────────────────────────────────────────
    print(f"Creating property group '{GROUP_NAME}'...")
    try:
        hs.create_property_group(GROUP_NAME, GROUP_LABEL)
        print(f"  ✓ Created group '{GROUP_NAME}'")
    except HubSpotPermanentError as exc:
        if exc.status_code in (409, 422) or "already exists" in str(exc.body or "").lower():
            print(f"  ℹ  Group '{GROUP_NAME}' already exists — skipping.")
        else:
            print(f"  ⚠  Group creation returned: {exc}")

    # ── 2. Create properties ──────────────────────────────────────────────────
    created = 0
    skipped = 0
    for prop_def in PROPERTIES:
        name = prop_def["name"]

        # Check if property already exists
        existing = hs.get_property(name)
        if existing:
            print(f"  ℹ  Property '{name}' already exists — skipping.")
            skipped += 1
            continue

        payload = {
            "name": name,
            "label": prop_def["label"],
            "type": prop_def["type"],
            "fieldType": prop_def["fieldType"],
            "groupName": GROUP_NAME,
            "description": prop_def.get("description", ""),
        }

        try:
            hs.create_property(payload)
            print(f"  ✓ Created property '{name}' ({prop_def['fieldType']})")
            created += 1
        except HubSpotPermanentError as exc:
            if exc.status_code in (409, 422):
                print(f"  ℹ  Property '{name}' already exists (409) — skipping.")
                skipped += 1
            else:
                print(f"  ❌ Failed to create '{name}': {exc}")

    db.close()
    print(f"\n✅ Done. {created} properties created, {skipped} already existed.")


if __name__ == "__main__":
    main()

