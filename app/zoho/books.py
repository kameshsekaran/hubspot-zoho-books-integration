"""
Zoho Books domain operations.

High-level functions that wrap ``ZohoClient`` for specific Zoho Books
resources used by this integration:

* Customers (Contacts)
* Sales Orders
* Invoices
* Payments

Each function handles the resource-specific URL paths, request payloads, and
response parsing so that ``sync_engine.py`` can call them without knowing
Zoho Books' API structure.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.zoho.client import ZohoClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


def find_customer_by_email(
    client: ZohoClient, email: str
) -> Optional[dict]:
    """
    Search for a Zoho Books contact by email address.

    Args:
        client: Authenticated ``ZohoClient`` instance.
        email:  Email address to search for.

    Returns:
        The first matching contact dict, or None if not found.
    """
    resp = client.get("/contacts", params={"email": email, "contact_type": "customer"})
    contacts = resp.get("contacts", [])
    return contacts[0] if contacts else None


def create_customer(
    client: ZohoClient,
    *,
    display_name: str,
    email: str,
    phone: Optional[str] = None,
    currency_code: str = "AED",
) -> dict:
    """
    Create a new customer (contact) in Zoho Books.

    Args:
        client:        Authenticated ``ZohoClient`` instance.
        display_name:  Legal entity name as it should appear on invoices.
        email:         Contact email (used as the unique key).
        phone:         Optional phone number.
        currency_code: ISO 4217 currency code (default: AED).

    Returns:
        The created Zoho ``contact`` dict.
    """
    body: dict = {
        "contact_name": display_name,
        "contact_type": "customer",
        "currency_code": currency_code,
        "email": email,
        "contact_persons": [
            {
                "email": email,
                "is_primary_contact": True,
            }
        ],
    }
    if phone:
        body["phone"] = phone
        body["contact_persons"][0]["phone"] = phone

    resp = client.post("/contacts", body=body)
    return resp.get("contact", resp)


def get_customer(client: ZohoClient, customer_id: str) -> dict:
    """
    Retrieve a Zoho Books customer by ID.

    Args:
        client:      Authenticated ``ZohoClient`` instance.
        customer_id: Zoho contact ID.

    Returns:
        The Zoho ``contact`` dict.
    """
    resp = client.get(f"/contacts/{customer_id}")
    return resp.get("contact", resp)


# ---------------------------------------------------------------------------
# Sales Orders
# ---------------------------------------------------------------------------


def create_sales_order(
    client: ZohoClient,
    *,
    customer_id: str,
    reference_number: str,
    order_date: date,
    currency_code: str,
    salesperson_name: str,
    line_item_name: str,
    line_item_rate: float,
    notes: Optional[str] = None,
) -> dict:
    """
    Create a Sales Order in Zoho Books.

    Args:
        client:           Authenticated ``ZohoClient`` instance.
        customer_id:      Zoho contact ID for the customer.
        reference_number: External reference (``HS-{deal_id}``).
        order_date:       Date of the deal close (ISO date).
        currency_code:    ISO 4217 currency code.
        salesperson_name: Deal owner name.
        line_item_name:   Description for the single line item.
        line_item_rate:   Deal amount (before VAT).
        notes:            Optional notes / deal name.

    Returns:
        The created Zoho ``salesorder`` dict.
    """
    body: dict = {
        "customer_id": customer_id,
        "reference_number": reference_number,
        "date": order_date.isoformat(),
        "currency_code": currency_code,
        "salesperson_name": salesperson_name,
        "line_items": [
            {
                "name": line_item_name,
                "rate": line_item_rate,
                "quantity": 1,
                "unit": "pcs",
            }
        ],
    }
    if notes:
        body["notes"] = notes

    resp = client.post("/salesorders", body=body)
    return resp.get("salesorder", resp)


def get_sales_order(client: ZohoClient, so_id: str) -> dict:
    """
    Retrieve a Sales Order by ID.

    Args:
        client: Authenticated ``ZohoClient`` instance.
        so_id:  Zoho sales order ID.

    Returns:
        The Zoho ``salesorder`` dict.
    """
    resp = client.get(f"/salesorders/{so_id}")
    return resp.get("salesorder", resp)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


def create_invoice(
    client: ZohoClient,
    *,
    customer_id: str,
    salesorder_id: str,
    due_date: date,
    line_item_name: str,
    line_item_rate: float,
    notes: Optional[str] = None,
    reference_number: Optional[str] = None,
) -> dict:
    """
    Create a milestone Invoice in Zoho Books linked to a Sales Order.

    Args:
        client:          Authenticated ``ZohoClient`` instance.
        customer_id:     Zoho customer ID.
        salesorder_id:   Zoho SO ID to link this invoice to.
        due_date:        Payment due date.
        line_item_name:  Description of the milestone.
        line_item_rate:  Invoice amount.
        notes:           Optional invoice notes.
        reference_number: Optional external reference string.

    Returns:
        The created Zoho ``invoice`` dict.
    """
    body: dict = {
        "customer_id": customer_id,
        "salesorder_id": salesorder_id,
        "due_date": due_date.isoformat(),
        "line_items": [
            {
                "name": line_item_name,
                "rate": line_item_rate,
                "quantity": 1,
            }
        ],
    }
    if notes:
        body["notes"] = notes
    if reference_number:
        body["reference_number"] = reference_number

    resp = client.post("/invoices", body=body)
    return resp.get("invoice", resp)


def get_invoice(client: ZohoClient, invoice_id: str) -> dict:
    """
    Retrieve an invoice by ID.

    Args:
        client:     Authenticated ``ZohoClient`` instance.
        invoice_id: Zoho invoice ID.

    Returns:
        The Zoho ``invoice`` dict.
    """
    resp = client.get(f"/invoices/{invoice_id}")
    return resp.get("invoice", resp)


def list_invoices_for_so(client: ZohoClient, so_id: str) -> list[dict]:
    """
    List all invoices associated with a Sales Order.

    Args:
        client: Authenticated ``ZohoClient`` instance.
        so_id:  Zoho sales order ID.

    Returns:
        List of invoice summary dicts.
    """
    resp = client.get("/invoices", params={"salesorder_id": so_id})
    return resp.get("invoices", [])


def void_invoice(client: ZohoClient, invoice_id: str) -> dict:
    """
    Void an invoice in Zoho Books.

    Only draft/sent invoices can be voided — the caller is responsible for
    checking status before calling this function.

    Args:
        client:     Authenticated ``ZohoClient`` instance.
        invoice_id: Zoho invoice ID to void.

    Returns:
        Zoho API response dict.
    """
    return client.post(f"/invoices/{invoice_id}/void", body={})


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def list_payments_for_invoice(client: ZohoClient, invoice_id: str) -> list[dict]:
    """
    List payments applied to a specific invoice.

    Args:
        client:     Authenticated ``ZohoClient`` instance.
        invoice_id: Zoho invoice ID.

    Returns:
        List of payment dicts.
    """
    resp = client.get(f"/invoices/{invoice_id}/payments")
    return resp.get("payments", [])


# ---------------------------------------------------------------------------
# Milestone invoice helpers
# ---------------------------------------------------------------------------


def create_milestone_invoices(
    client: ZohoClient,
    *,
    customer_id: str,
    salesorder_id: str,
    so_reference: str,
    deal_amount: float,
    close_date: date,
) -> list[dict]:
    """
    Create the three milestone invoices for a newly synced deal.

    Milestones:
        1. Contract Signing  — 40% — due on close date
        2. Go-Live           — 40% — due 30 days after close date
        3. 90-Day Post Go-Live — 20% — due 90 days after close date

    Args:
        client:       Authenticated ``ZohoClient`` instance.
        customer_id:  Zoho customer ID.
        salesorder_id: Zoho SO ID.
        so_reference: HubSpot deal ID reference string (``HS-{deal_id}``).
        deal_amount:  Total deal amount.
        close_date:   HubSpot deal close date.

    Returns:
        List of three created invoice dicts.
    """
    milestones = [
        {
            "pct": 0.40,
            "label": "Milestone 1: Contract Signing",
            "due_date": close_date,
        },
        {
            "pct": 0.40,
            "label": "Milestone 2: Go-Live",
            "due_date": close_date + timedelta(days=30),
        },
        {
            "pct": 0.20,
            "label": "Milestone 3: 90-Day Post Go-Live",
            "due_date": close_date + timedelta(days=90),
        },
    ]

    invoices = []
    for i, m in enumerate(milestones, start=1):
        amount = round(deal_amount * m["pct"], 2)
        inv = create_invoice(
            client,
            customer_id=customer_id,
            salesorder_id=salesorder_id,
            due_date=m["due_date"],
            line_item_name=m["label"],
            line_item_rate=amount,
            notes=m["label"],
            reference_number=f"{so_reference}-M{i}",
        )
        invoices.append(inv)
        logger.info(
            "Created invoice %s: %s (%.2f)", i, m["label"], amount
        )

    return invoices

