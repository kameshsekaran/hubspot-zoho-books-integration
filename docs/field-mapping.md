# Field Mapping Reference

## Overview

This document defines every field mapped between HubSpot CRM and Zoho Books. Custom HubSpot properties (prefixed `zoho_`) are created during setup by running `python scripts/setup_hubspot_properties.py`.

**Direction key:**
- `→` CRM → Zoho (Integration reads HubSpot, writes Zoho)
- `←` Zoho → CRM (Integration reads Zoho, writes HubSpot)
- `↔` Bidirectional (read from both, reconciled by integration)

---

## Core Field Mappings

| CRM Field (HubSpot) | Zoho Books Field | Data Type | Owner | Direction | Notes |
|---|---|---|---|---|---|
| `dealname` | `salesorder.notes` | `string` | Sales Rep | → | Full deal name used as the SO description/notes field |
| `company.name` | `contact.display_name` | `string` | Sales Rep | → | Legal entity name as it appears on invoices |
| `contact.email` | `contact.email` | `string` | Sales Rep | → | Used as the unique key to find/create Zoho customer; must be present |
| `contact.phone` | `contact.phone` | `string` | Sales Rep | → | Copied to Zoho contact; formatted as international if possible |
| `amount` | `salesorder.sub_total` | `decimal` | Sales Rep | → | Raw deal value; currency assumed AED unless overridden |
| `deal_currency_code` *(default: AED)* | `salesorder.currency_code` | `string (ISO 4217)` | Sales Rep | → | Zoho org must have the currency enabled; defaults to `AED` |
| `closedate` | `salesorder.date` | `date (YYYY-MM-DD)` | Sales Rep | → | Epoch ms from HubSpot converted to ISO date |
| `hubspot_owner_id` *(resolved to name)* | `salesorder.salesperson_name` | `string` | HubSpot | → | Owner name fetched via `/crm/v3/owners/{id}` |
| `hs_object_id` | `salesorder.reference_number` | `string` | Integration | → | Stored as `"HS-{deal_id}"` for traceability in Zoho |

---

## Write-Back Fields (Zoho → HubSpot)

These are **custom HubSpot deal properties** created by the setup script. All are read-only from the HubSpot UI perspective — only the integration writes to them.

| CRM Field (HubSpot) | Zoho Books Field | Data Type | Owner | Direction | Notes |
|---|---|---|---|---|---|
| `zoho_so_id` | `salesorder.salesorder_id` | `string` | Integration | ← | Internal Zoho Books GUID for the sales order |
| `zoho_so_number` | `salesorder.salesorder_number` | `string` | Integration | ← | Human-readable Zoho SO number, e.g. `SO-00042` |
| `zoho_customer_id` | `contact.contact_id` | `string` | Integration | ← | Zoho Books contact/customer GUID |
| `zoho_invoice_ids` | `invoice.invoice_id` (×3) | `string` | Integration | ← | Comma-separated list of all invoice GUIDs, e.g. `"zb_inv_001,zb_inv_002,zb_inv_003"` |
| `zoho_so_total` | `salesorder.total` | `decimal` | Integration | ← | Confirmed total from Zoho (may differ from HubSpot amount after discounts/taxes) |

---

## Computed Financial Metrics (Zoho → HubSpot)

These fields are **computed by the integration** from live Zoho Books data on every sync. They give the sales team and management real-time financial visibility inside HubSpot without accessing Zoho.

| CRM Field (HubSpot) | Source / Formula | Data Type | Owner | Direction | Notes |
|---|---|---|---|---|---|
| `zoho_pct_invoiced` | `sum(invoice.total) / salesorder.total × 100` | `decimal (0–100)` | Integration | ← | Percentage of deal value that has been invoiced; includes draft invoices |
| `zoho_pct_collected` | `sum(invoice.payment_made) / salesorder.total × 100` | `decimal (0–100)` | Integration | ← | Percentage of deal value actually collected; used in revenue reporting |
| `zoho_amount_unpaid` | `sum(invoice.balance)` where `invoice.status IN ('sent','overdue')` | `decimal` | Integration | ← | Outstanding amount on all *sent* invoices only; excludes drafts to avoid overstating AR |
| `zoho_amount_uninvoiced` | `salesorder.total − sum(invoice.total)` | `decimal` | Integration | ← | Value not yet covered by any invoice; useful for forecasting remaining billing events |
| `zoho_next_due_date` | `min(invoice.due_date)` where `invoice.status NOT IN ('paid','void')` | `date` | Integration | ← | Earliest upcoming payment due date across all open invoices |
| `zoho_overdue` | `any(invoice.status == 'overdue')` | `boolean` | Integration | ← | `true` if at least one invoice is overdue; used to trigger HubSpot workflow alerts to Finance |

---

## Status & Audit Fields (Integration-Managed)

| CRM Field (HubSpot) | Set By | Data Type | Direction | Notes |
|---|---|---|---|---|
| `zoho_sync_status` | Integration Service | `string (enum)` | ← | Values: `pending`, `synced`, `error`, `paused`, `error_fatal` |
| `zoho_sync_error` | Integration Service | `string` | ← | Last error message if `sync_status = error`; cleared on successful sync |
| `zoho_last_synced` | Integration Service | `datetime (ISO 8601)` | ← | UTC timestamp of last successful sync; used to detect stale data |

---

## Field Creation Reference (HubSpot Property Types)

| HubSpot Property Name | HubSpot Type | Group |
|---|---|---|
| `zoho_so_id` | `single_line_text` | `zoho_integration` |
| `zoho_so_number` | `single_line_text` | `zoho_integration` |
| `zoho_customer_id` | `single_line_text` | `zoho_integration` |
| `zoho_invoice_ids` | `single_line_text` | `zoho_integration` |
| `zoho_so_total` | `number` | `zoho_integration` |
| `zoho_pct_invoiced` | `number` | `zoho_integration` |
| `zoho_pct_collected` | `number` | `zoho_integration` |
| `zoho_amount_unpaid` | `number` | `zoho_integration` |
| `zoho_amount_uninvoiced` | `number` | `zoho_integration` |
| `zoho_next_due_date` | `date` | `zoho_integration` |
| `zoho_overdue` | `booleancheckbox` | `zoho_integration` |
| `zoho_sync_status` | `single_line_text` | `zoho_integration` |
| `zoho_sync_error` | `single_line_text` | `zoho_integration` |
| `zoho_last_synced` | `datetime` | `zoho_integration` |

> [!NOTE]
> All custom properties belong to the `zoho_integration` property group. The setup script creates this group and all properties in a single idempotent run — safe to re-run if properties already exist.

---

## Invoice Milestone Mapping

| Milestone | % of Total | Trigger | HubSpot Field Contribution |
|---|---|---|---|
| Invoice 1 — Contract Signing | 40% | Created immediately on Closed Won | Included in `zoho_invoice_ids`, contributes to `zoho_pct_invoiced` |
| Invoice 2 — Go-Live | 40% | Created immediately; due date = close date + 30 days | Same as above |
| Invoice 3 — 90-Day Post Go-Live | 20% | Created immediately; due date = close date + 90 days | Same as above |

> [!TIP]
> All three invoices are created **at deal sync time** rather than on a schedule. This avoids scheduling complexity and race conditions. Finance controls when each invoice is *sent* to the customer from within Zoho Books.

