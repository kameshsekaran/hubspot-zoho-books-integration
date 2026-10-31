"""
Zoho Books customer management.

Provides idempotent find-or-create logic with deduplication based on email
address and company name matching.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.zoho.client import ZohoClient

logger = logging.getLogger(__name__)

# Zoho Books contact type for B2B customers.
_CUSTOMER_TYPE = "business"


def find_customer(
    client: ZohoClient,
    name: Optional[str] = None,
    email: Optional[str] = None,
) -> list[dict]:
    """
    Search Zoho Books contacts by email (preferred) then by name.

    We search by email first because it is unique and unambiguous.  If no
    email match is found we fall back to a name search which may return
    multiple candidates (ambiguous case).

    Args:
        client: Authenticated ZohoClient instance.
        name:   Company / contact display name to search for.
        email:  Primary email address to search for.

    Returns:
        List of matching Zoho contact dicts (may be empty).
    """
    candidates: list[dict] = []

    # ── Email search (exact match in Zoho) ────────────────────────────────
    if email:
        resp = client.get("/contacts", params={"email": email, "contact_type": "customer"})
        hits = resp.get("contacts", [])
        if hits:
            logger.debug("Found %d customer(s) by email %s", len(hits), email)
            return hits

    # ── Name search (may be fuzzy) ─────────────────────────────────────────
    if name:
        resp = client.get(
            "/contacts",
            params={"contact_name_contains": name, "contact_type": "customer"},
        )
        candidates = resp.get("contacts", [])
        logger.debug("Found %d customer(s) by name %s", len(candidates), name)

    return candidates


def create_customer(client: ZohoClient, data: dict) -> dict:
    """
    Create a new Zoho Books customer contact.

    Args:
        client: Authenticated ZohoClient instance.
        data:   Dict containing at minimum ``contact_name``; may also include
                ``email``, ``billing_address``, ``currency_code``, etc.

    Returns:
        The full Zoho contact dict as returned by the API.

    Raises:
        ZohoPermanentError: If Zoho rejects the create request.
    """
    payload = {
        "contact_type": _CUSTOMER_TYPE,
        **data,
    }
    resp = client.post("/contacts", body=payload)
    contact = resp.get("contact", {})
    logger.info(
        "Created Zoho customer '%s' (id=%s)",
        contact.get("contact_name"),
        contact.get("contact_id"),
    )
    return contact


def find_or_create_customer(
    client: ZohoClient,
    deal_data: dict,
    db: Session,  # kept for future DB-side dedup caching
) -> tuple[str, bool, bool, list[dict]]:
    """
    Find an existing Zoho customer or create a new one.

    The function checks the ``deal_links`` table first (fastest path), then
    searches Zoho by email, then by company name.  Creation only happens when
    no match is found.

    Args:
        client:    Authenticated ZohoClient instance.
        deal_data: Dict with keys: ``company_name``, ``contact_email``,
                   ``contact_name``, ``contact_phone``, ``billing_address``,
                   ``currency_code``.
        db:        Open SQLAlchemy session (for future caching).

    Returns:
        A 4-tuple of:
        - ``customer_id``  (str): Zoho contact_id.
        - ``created``      (bool): True if a new contact was just created.
        - ``ambiguous``    (bool): True if multiple name matches were found and
                                   we picked the first one.
        - ``candidates``   (list): Raw candidate contacts from Zoho (for review).
    """
    company_name: str = deal_data.get("company_name", "")
    email: str = deal_data.get("contact_email", "")

    candidates = find_customer(client, name=company_name, email=email)

    # ── Exact email match – unambiguous ────────────────────────────────────
    if candidates and email:
        email_lower = email.lower()
        exact = [
            c for c in candidates
            if (c.get("email") or "").lower() == email_lower
        ]
        if exact:
            chosen = exact[0]
            return chosen["contact_id"], False, False, exact

    # ── Name match (possibly ambiguous) ────────────────────────────────────
    if candidates:
        ambiguous = len(candidates) > 1
        chosen = candidates[0]
        logger.info(
            "Matched customer by name '%s' (id=%s, ambiguous=%s)",
            company_name,
            chosen["contact_id"],
            ambiguous,
        )
        return chosen["contact_id"], False, ambiguous, candidates

    # ── No match – create new ──────────────────────────────────────────────
    logger.info("No existing customer found for '%s' – creating.", company_name)
    create_payload = _build_customer_payload(deal_data)
    new_contact = create_customer(client, create_payload)
    return new_contact["contact_id"], True, False, [new_contact]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_customer_payload(deal_data: dict) -> dict:
    """
    Construct the Zoho contact creation payload from HubSpot deal data.

    Args:
        deal_data: Normalised deal dictionary produced by the sync layer.

    Returns:
        Dict suitable for passing to ``create_customer``.
    """
    payload: dict = {
        "contact_name": deal_data.get("company_name", "Unknown Company"),
        "currency_code": deal_data.get("currency_code", "AED"),
    }

    contact_persons = []
    if deal_data.get("contact_name") or deal_data.get("contact_email"):
        person: dict = {}
        if deal_data.get("contact_name"):
            parts = deal_data["contact_name"].split(" ", 1)
            person["first_name"] = parts[0]
            person["last_name"] = parts[1] if len(parts) > 1 else ""
        if deal_data.get("contact_email"):
            person["email"] = deal_data["contact_email"]
        if deal_data.get("contact_phone"):
            person["phone"] = deal_data["contact_phone"]
        person["is_primary_contact"] = True
        contact_persons.append(person)

    if contact_persons:
        payload["contact_persons"] = contact_persons

    if deal_data.get("billing_address"):
        payload["billing_address"] = deal_data["billing_address"]

    return payload
