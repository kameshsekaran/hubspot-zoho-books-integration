"""
HubSpot custom property setup.

Defines and creates the 14 custom deal properties used by this integration
to write Zoho Books data back into HubSpot deals.

Usage::

    from app.crm.properties import setup_custom_properties
    from app.crm.client import HubSpotClient

    client = HubSpotClient(db)
    setup_custom_properties(client)
"""

import logging
from typing import Optional

from app.crm.client import HubSpotClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Property definitions
# ---------------------------------------------------------------------------

# Each dict is a HubSpot property definition payload.
# fieldType / type combinations:
#   text  / string   → free-text single line
#   textarea / string → multi-line
#   number / number  → numeric
#   booleancheckbox / enumeration → true/false
#   date / date      → ISO date

CUSTOM_PROPERTY_DEFINITIONS: list[dict] = [
    {
        "name": "zoho_so_id",
        "label": "Zoho SO ID",
        "type": "string",
        "fieldType": "text",
        "groupName": "zoho_integration",
        "description": "Zoho Books Sales Order internal ID.",
    },
    {
        "name": "zoho_so_number",
        "label": "Zoho SO Number",
        "type": "string",
        "fieldType": "text",
        "groupName": "zoho_integration",
        "description": "Human-readable Zoho Books Sales Order number.",
    },
    {
        "name": "zoho_customer_id",
        "label": "Zoho Customer ID",
        "type": "string",
        "fieldType": "text",
        "groupName": "zoho_integration",
        "description": "Zoho Books Contact (customer) internal ID.",
    },
    {
        "name": "zoho_invoice_ids",
        "label": "Zoho Invoice IDs",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "zoho_integration",
        "description": "Comma-separated list of Zoho Books invoice IDs.",
    },
    {
        "name": "zoho_so_total",
        "label": "Zoho SO Total (AED)",
        "type": "number",
        "fieldType": "number",
        "groupName": "zoho_integration",
        "description": "Total value of the Zoho Sales Order (incl. VAT).",
    },
    {
        "name": "zoho_pct_invoiced",
        "label": "Zoho % Invoiced",
        "type": "number",
        "fieldType": "number",
        "groupName": "zoho_integration",
        "description": "Percentage of deal value that has been invoiced (0–100).",
    },
    {
        "name": "zoho_pct_collected",
        "label": "Zoho % Collected",
        "type": "number",
        "fieldType": "number",
        "groupName": "zoho_integration",
        "description": "Percentage of deal value that has been collected (0–100).",
    },
    {
        "name": "zoho_amount_unpaid",
        "label": "Zoho Amount Unpaid (AED)",
        "type": "number",
        "fieldType": "number",
        "groupName": "zoho_integration",
        "description": "Total outstanding (invoiced but unpaid) amount in AED.",
    },
    {
        "name": "zoho_amount_uninvoiced",
        "label": "Zoho Amount Uninvoiced (AED)",
        "type": "number",
        "fieldType": "number",
        "groupName": "zoho_integration",
        "description": "Total not-yet-invoiced amount in AED.",
    },
    {
        "name": "zoho_next_due_date",
        "label": "Zoho Next Due Date",
        "type": "date",
        "fieldType": "date",
        "groupName": "zoho_integration",
        "description": "Due date of the next unpaid invoice.",
    },
    {
        "name": "zoho_overdue",
        "label": "Zoho Overdue",
        "type": "enumeration",
        "fieldType": "booleancheckbox",
        "groupName": "zoho_integration",
        "description": "True if any invoice is past its due date and unpaid.",
        "options": [
            {"label": "Yes", "value": "true", "displayOrder": 1},
            {"label": "No", "value": "false", "displayOrder": 2},
        ],
    },
    {
        "name": "zoho_sync_status",
        "label": "Zoho Sync Status",
        "type": "string",
        "fieldType": "text",
        "groupName": "zoho_integration",
        "description": "Latest sync status: ok | error | pending.",
    },
    {
        "name": "zoho_sync_error",
        "label": "Zoho Sync Error",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "zoho_integration",
        "description": "Error message from the last failed sync attempt.",
    },
    {
        "name": "zoho_last_synced",
        "label": "Zoho Last Synced",
        "type": "string",
        "fieldType": "text",
        "groupName": "zoho_integration",
        "description": "ISO-8601 timestamp of the last successful sync.",
    },
]

# Name of the property group that contains all zoho_* properties.
_GROUP_NAME = "zoho_integration"
_GROUP_LABEL = "Zoho Books Integration"
_GROUPS_URL = "https://api.hubapi.com/crm/v3/properties/deals/groups"


def setup_custom_properties(client: HubSpotClient) -> dict:
    """
    Ensure all 14 Zoho custom deal properties exist in HubSpot.

    The function is idempotent: existing properties are left unchanged.  A
    ``zoho_integration`` property group is created if it does not exist.

    Args:
        client: Authenticated HubSpotClient instance.

    Returns:
        Dict with ``created`` and ``existing`` lists of property names.
    """
    _ensure_property_group(client)

    existing_names = _get_existing_property_names(client)
    created: list[str] = []
    skipped: list[str] = []

    for prop_def in CUSTOM_PROPERTY_DEFINITIONS:
        name = prop_def["name"]
        if name in existing_names:
            logger.debug("Property '%s' already exists – skipping.", name)
            skipped.append(name)
            continue

        try:
            client.create_property(prop_def)
            logger.info("Created HubSpot property: %s", name)
            created.append(name)
        except Exception as exc:
            logger.warning("Failed to create property '%s': %s", name, exc)

    return {"created": created, "existing": skipped}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_existing_property_names(client: HubSpotClient) -> set[str]:
    """
    Return the set of all existing deal property names in HubSpot.

    Args:
        client: Authenticated HubSpotClient.

    Returns:
        Set of property name strings.
    """
    props = client.list_properties()
    return {p["name"] for p in props}


def _ensure_property_group(client: HubSpotClient) -> None:
    """
    Create the ``zoho_integration`` property group if it does not exist.

    Args:
        client: Authenticated HubSpotClient.
    """
    try:
        resp = client._request("GET", _GROUPS_URL)
        groups = resp.get("results", [])
        if any(g.get("name") == _GROUP_NAME for g in groups):
            return
        client._request(
            "POST",
            _GROUPS_URL,
            body={"name": _GROUP_NAME, "label": _GROUP_LABEL, "displayOrder": 99},
        )
        logger.info("Created HubSpot property group '%s'.", _GROUP_NAME)
    except Exception as exc:
        logger.warning("Could not ensure property group: %s", exc)
