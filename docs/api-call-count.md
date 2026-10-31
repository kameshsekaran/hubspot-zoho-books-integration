# API Call Count Analysis

## Overview

This document provides a precise breakdown of every Zoho Books and HubSpot API call made by the integration, across three scenarios: initial deal processing, steady-state sync, and the critical daily quota problem with its solution.

---

## Scenario 1: Initial Deal Processing (Closed Won → All Invoices Created)

Processing one deal from scratch through to all Zoho resources created and HubSpot updated.

| # | Direction | API Call | Endpoint | Required? | Notes |
|---|---|---|---|---|---|
| 1 | → HubSpot | GET deal | `GET /crm/v3/objects/deals/{id}?associations=companies,contacts` | Always | Fetches deal properties and association IDs |
| 2 | → HubSpot | GET company | `GET /crm/v3/objects/companies/{id}` | Always | Fetches company name for `contact.display_name` |
| 3 | → HubSpot | GET contact | `GET /crm/v3/objects/contacts/{id}` | Always | Fetches email, phone for Zoho customer creation |
| 4 | → Zoho | Search customer by email | `GET /contacts?email={email}` | Always | Determines create-or-reuse |
| 5 | → Zoho | Create customer | `POST /contacts` | Only if new | Skipped if customer already exists in Zoho |
| 6 | → Zoho | Create sales order | `POST /salesorders` | Always | Core provisioning step |
| 7 | → Zoho | Create invoice 1 (Signing, 40%) | `POST /invoices` | Always | Milestone 1 |
| 8 | → Zoho | Create invoice 2 (Go-Live, 40%) | `POST /invoices` | Always | Milestone 2 |
| 9 | → Zoho | Create invoice 3 (90-Day, 20%) | `POST /invoices` | Always | Milestone 3 |
| 10 | → HubSpot | Update deal properties | `PATCH /crm/v3/objects/deals/{id}` | Always | Writes all `zoho_*` fields |

### Totals — Initial Processing (New Customer)

| Category | Count |
|---|---|
| HubSpot API calls | **4** (3 GETs + 1 PATCH) |
| Zoho Books API calls | **6** (1 search + 1 create contact + 1 SO + 3 invoices) |
| **Total** | **10 calls per deal** |

### Totals — Initial Processing (Existing Customer)

| Category | Count |
|---|---|
| HubSpot API calls | **4** |
| Zoho Books API calls | **5** (1 search [found] + 1 SO + 3 invoices) |
| **Total** | **9 calls per deal** |

> [!NOTE]
> The customer search call always happens. If the contact is found, the `POST /contacts` call is skipped. This is the only conditional call in the initial flow.

---

## Scenario 2: Steady-State Sync (Nothing Has Changed)

The 15-minute periodic sync checks the status of all deals to catch anything webhooks may have missed. Here is the call breakdown *per deal* when no data has changed:

| # | Direction | API Call | Endpoint | Notes |
|---|---|---|---|---|
| 1 | → Zoho | GET sales order | `GET /salesorders/{zoho_so_id}` | Checks SO status and total |
| 2 | → Zoho | GET invoices for SO | `GET /invoices?salesorder_id={so_id}` | Returns all invoices (paginated if > 25) |
| 3 | → Zoho | GET payments for invoice 1 | `GET /invoices/{inv_id}/payments` | Checks payment status |
| 4 | → Zoho | GET payments for invoice 2 | `GET /invoices/{inv_id}/payments` | Checks payment status |
| 5 | → Zoho | GET payments for invoice 3 | `GET /invoices/{inv_id}/payments` | Checks payment status |
| — | → HubSpot | Update deal | `PATCH /crm/v3/objects/deals/{id}` | **Skipped** — change detection finds no difference |

### Per-Deal Sync Totals (No Change)

| Category | Count |
|---|---|
| Zoho Books API calls | **5** |
| HubSpot API calls | **0** (change detection prevents unnecessary writes) |
| **Total** | **5 calls per deal** |

> [!TIP]
> The HubSpot PATCH is only made if the computed values differ from the last-known values stored in `deal_links`. This is critical for keeping within the daily API quota.

---

## Scenario 3: Scale Analysis — 20 Deals, 15-Minute Sync Cycle

### Naïve Polling (No Change Detection)

| Metric | Calculation | Value |
|---|---|---|
| Zoho calls per sync cycle | 20 deals × 5 calls | **100 calls** |
| HubSpot writes per sync cycle | 20 deals × 1 write | **20 calls** |
| Total per sync cycle | | **120 calls** |
| Sync cycles per hour | 60 min / 15 min | **4 cycles/hour** |
| Total calls per hour | 120 × 4 | **480 calls/hour** |
| Total calls per day | 480 × 24 | **11,520 calls/day** |

> [!CAUTION]
> **11,520 calls/day vastly exceeds Zoho Books' standard daily quota of 2,000 calls.** Naïve polling is completely unviable at 20+ deals.

### Optimised Approach (Webhooks Primary + Change Detection)

The solution has two layers:

**Layer 1 — Webhooks as Primary Sync**
Every invoice status change and payment in Zoho triggers a webhook to the integration. Each webhook triggers a targeted sync of *only that deal*. Typical webhook volume for 20 active deals: ~5–15 events/day.

| Metric | Calculation | Value |
|---|---|---|
| Zoho calls per webhook event | 5 (SO + 3 invoices + payment check) | **5 calls** |
| HubSpot write per webhook | 1 PATCH (always needed for event) | **1 call** |
| Estimated events/day (20 deals) | ~10 events (invoice sends, payments) | |
| Webhook-triggered calls/day | 10 × 6 | **~60 calls/day** |

**Layer 2 — Periodic Sync with Change Detection**
The 15-minute poll runs for all deals but uses **change detection** — it compares the hash of computed values against the stored hash. If nothing changed, zero HubSpot writes are made.

| Metric | Calculation | Value |
|---|---|---|
| Zoho polling calls per cycle | 20 deals × 5 calls | **100 calls** |
| Cycles per day | 4/hour × 24 hours | **96 cycles** |
| Total polling Zoho calls/day | 100 × 96 | **9,600 calls** |

**This is still too high for pure polling.** The fix:

**Layer 3 — Adaptive Polling**
Replace the fixed 15-minute full-sweep with:
1. **Webhook-only for active deals** (deals with open invoices): real-time via Zoho webhooks.
2. **Daily reconciliation sweep** for all deals (once per day, e.g. at 02:00 UTC): catches any webhook delivery failures.

| Metric | Calculation | Value |
|---|---|---|
| Daily reconciliation calls | 20 deals × 5 Zoho + 1 HubSpot | **120 calls/day** |
| Webhook event calls/day | ~10 events × 6 calls | **~60 calls/day** |
| Initial deal processing/day | ~2 new deals × 10 calls | **~20 calls/day** |
| **Total optimised calls/day** | 120 + 60 + 20 | **~200 calls/day** |

### Summary Comparison

| Strategy | Daily Zoho Calls | Within 2,000 Limit? |
|---|---|---|
| Naïve 15-min polling, 20 deals | ~11,520 | ❌ |
| 15-min polling + change detection (no HubSpot writes) | ~9,600 Zoho + ~60 HubSpot | ❌ (Zoho calls) |
| Webhooks primary + daily reconciliation sweep | **~200** | ✅ **Well under limit** |

> [!IMPORTANT]
> The production-grade solution is **webhooks as the primary sync mechanism** with a once-daily reconciliation pass. The 15-minute periodic sync is retained as a configurable option for lower deal volumes (< 5 active deals) where the call count is acceptable.

---

## Zoho Books API Quota Reference

| Plan | Daily Call Limit | Per-Minute Limit |
|---|---|---|
| Free (1 user) | 2,000 | Not published |
| Standard | 2,000 | ~100 |
| Professional | 5,000 | ~100 |
| Premium | 10,000 | ~100 |

*Limits as of 2024; verify current limits in Zoho Books API documentation.*

---

## HubSpot API Quota Reference

| Plan | Daily Call Limit | Per-10-Second Burst |
|---|---|---|
| Free (Private App) | 250,000 | 100 |
| Starter | 500,000 | 150 |

HubSpot's quota is generous enough that it is **never a bottleneck** for this integration at the described scale. Rate-limiting handling is still implemented for robustness (respect `Retry-After` on 429 responses).

