"""
Seed script: Create a test deal in HubSpot and verify the integration syncs it.

Usage:
    python scripts/seed_data.py

This script:
1. Creates a test company and contact in HubSpot (idempotent by email).
2. Creates a deal named "TechNova Solutions — ERP Implementation".
3. Sets the deal stage to closedwon, triggering the integration webhook.
4. Waits for sync and verifies zoho_so_id is set on the deal.

Expected output:
    ✓ Created HubSpot company: TechNova Solutions LLC (id: 12345678)
    ✓ Created HubSpot contact: Mohammed Al-Rashidi (id: 87654321)
    ✓ Created HubSpot deal: AED 126,000 (id: hs_deal_789)
    ✓ Set deal stage to closedwon
    ⏳ Waiting for sync...
    ✓ Sync complete! zoho_so_id=zb_so_001, zoho_so_number=SO-00042
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.database import SessionLocal
from app.hubspot_client import HubSpotClient, HubSpotPermanentError

COMPANY_NAME = "TechNova Solutions LLC"
CONTACT_EMAIL = "cto@technova.ae"
CONTACT_FIRST = "Mohammed"
CONTACT_LAST = "Al-Rashidi"
DEAL_NAME = "TechNova Solutions — ERP Implementation"
DEAL_AMOUNT = "126000"
DEAL_CURRENCY = "AED"
CLOSE_DATE = "2026-10-30"


def main() -> None:
    if not settings.hubspot_access_token or settings.hubspot_access_token.startswith("your_"):
        print("❌ HUBSPOT_ACCESS_TOKEN not configured. Edit .env first.")
        sys.exit(1)

    db = SessionLocal()
    hs = HubSpotClient(db)

    # ── 1. Create / find company ──────────────────────────────────────────────
    print(f"Creating company '{COMPANY_NAME}'...")
    try:
        co = hs.create_company({
            "name": COMPANY_NAME,
            "phone": "+971-4-000-0000",
            "city": "Dubai",
            "country": "United Arab Emirates",
        })
        company_id = co.get("id") or co.get("objectId", "")
        print(f"  ✓ Created HubSpot company: {COMPANY_NAME} (id: {company_id})")
    except HubSpotPermanentError as exc:
        print(f"  ⚠  Company creation failed (may already exist): {exc}")
        company_id = ""

    # ── 2. Create / find contact ──────────────────────────────────────────────
    print(f"Creating contact '{CONTACT_FIRST} {CONTACT_LAST}'...")
    try:
        ct = hs.create_contact({
            "firstname": CONTACT_FIRST,
            "lastname": CONTACT_LAST,
            "email": CONTACT_EMAIL,
            "phone": "+971-50-000-0000",
            "jobtitle": "CTO",
        })
        contact_id = ct.get("id") or ct.get("objectId", "")
        print(f"  ✓ Created HubSpot contact: {CONTACT_FIRST} {CONTACT_LAST} (id: {contact_id})")
    except HubSpotPermanentError as exc:
        print(f"  ⚠  Contact creation failed (may already exist): {exc}")
        contact_id = ""

    # ── 3. Create deal ────────────────────────────────────────────────────────
    print(f"Creating deal '{DEAL_NAME}'...")
    import datetime
    close_ts = str(int(datetime.datetime(2026, 10, 30).timestamp() * 1000))

    deal = hs.create_deal({
        "dealname": DEAL_NAME,
        "amount": DEAL_AMOUNT,
        "deal_currency_code": DEAL_CURRENCY,
        "closedate": close_ts,
        "dealstage": "appointmentscheduled",
        "pipeline": "default",
    })
    deal_id = deal.get("id") or deal.get("objectId", "")
    print(f"  ✓ Created HubSpot deal: AED {DEAL_AMOUNT} (id: {deal_id})")

    # Associate with company and contact
    if company_id and deal_id:
        try:
            hs.associate_deal_with_company(deal_id, company_id)
        except Exception as exc:
            print(f"  ⚠  Association with company failed: {exc}")

    if contact_id and deal_id:
        try:
            hs.associate_deal_with_contact(deal_id, contact_id)
        except Exception as exc:
            print(f"  ⚠  Association with contact failed: {exc}")

    # ── 4. Set stage to closedwon ─────────────────────────────────────────────
    print("Setting deal stage to closedwon...")
    hs.patch_deal(deal_id, {"dealstage": settings.hubspot_closed_won_stage_id})
    print(f"  ✓ Set deal stage to closedwon")

    # ── 5. Wait and verify ────────────────────────────────────────────────────
    print("⏳ Waiting for sync (15 seconds)...")
    time.sleep(15)

    deal_current = hs.get_deal(deal_id)
    props = deal_current.get("properties", {})
    zoho_so_id = props.get("zoho_so_id")
    zoho_so_number = props.get("zoho_so_number")

    if zoho_so_id:
        print(f"  ✓ Sync complete! zoho_so_id={zoho_so_id}, zoho_so_number={zoho_so_number}")
    else:
        print(f"  ⏳ Sync not complete yet. Check /status and /logs for details.")
        print(f"     Deal ID: {deal_id}")

    db.close()
    print("\nDone.")


if __name__ == "__main__":
    main()

