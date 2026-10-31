# Deal Flow Sequence Diagram

## Overview

This document traces the complete lifecycle of a single deal — from the moment a sales rep marks it *Closed Won* in HubSpot, through all Zoho Books provisioning steps, milestone invoice milestones, payment syncs, and the periodic reconciliation pass.

---

## Full Sequence: Closed Won → Paid

```mermaid
sequenceDiagram
    participant Sales as Sales Rep
    participant HS as HubSpot CRM
    participant IS as Integration Service
    participant DB as Database
    participant ZB as Zoho Books
    participant Finance as Finance Team

    %% ─── 1. SALES REP CLOSES THE DEAL ───────────────────────────────────────
    Sales->>HS: Set deal stage = "closedwon"
    Note over HS: Deal: TechNova Solutions LLC<br/>Amount: AED 126,000

    %% ─── 2. HUBSPOT FIRES WEBHOOK ───────────────────────────────────────────
    HS->>IS: POST /webhooks/hubspot<br/>{ event: "deal.propertyChange",<br/>  property: "dealstage",<br/>  value: "closedwon",<br/>  objectId: "hs_deal_789" }

    %% ─── 3. VALIDATE + ENQUEUE (return 200 immediately) ─────────────────────
    IS->>IS: Validate HMAC-SHA256 signature
    IS->>IS: Check timestamp < 5 min old
    IS->>DB: INSERT job_queue<br/>{ type: "sync_deal",<br/>  deal_id: "hs_deal_789",<br/>  status: "pending" }
    IS-->>HS: 200 OK (< 50 ms)

    %% ─── 4. WORKER PICKS UP JOB ─────────────────────────────────────────────
    Note over IS: Async worker polls queue
    IS->>DB: SELECT next pending job
    DB-->>IS: job { type: "sync_deal", deal_id: "hs_deal_789" }
    IS->>DB: UPDATE job status = "processing"

    %% ─── 5. IDEMPOTENCY CHECK ────────────────────────────────────────────────
    IS->>DB: SELECT * FROM deal_links<br/>WHERE hubspot_deal_id = "hs_deal_789"
    DB-->>IS: (no row found — first time)
    IS->>DB: INSERT deal_links<br/>{ hubspot_deal_id: "hs_deal_789",<br/>  sync_status: "pending" }

    %% ─── 6. FETCH DEAL + COMPANY + CONTACT FROM HUBSPOT ─────────────────────
    IS->>HS: GET /crm/v3/objects/deals/hs_deal_789?associations=companies,contacts
    HS-->>IS: { dealname, amount: 126000,<br/>  closedate, dealowner, currency: "AED",<br/>  associations: { company_id, contact_id } }

    IS->>HS: GET /crm/v3/objects/companies/{company_id}
    HS-->>IS: { name: "TechNova Solutions LLC",<br/>  phone, address }

    IS->>HS: GET /crm/v3/objects/contacts/{contact_id}
    HS-->>IS: { email: "cto@technova.ae",<br/>  firstname, lastname, phone }

    %% ─── 7. FIND / CREATE CUSTOMER IN ZOHO BOOKS ────────────────────────────
    IS->>ZB: GET /contacts?email=cto@technova.ae
    ZB-->>IS: { contacts: [] }  ── not found

    IS->>ZB: POST /contacts<br/>{ display_name: "TechNova Solutions LLC",<br/>  email: "cto@technova.ae",<br/>  phone, currency_code: "AED" }
    ZB-->>IS: { contact_id: "zb_cust_456",<br/>  contact_name: "TechNova Solutions LLC" }

    IS->>DB: UPDATE deal_links SET zoho_customer_id = "zb_cust_456"

    %% ─── 8. CREATE SALES ORDER ───────────────────────────────────────────────
    IS->>ZB: POST /salesorders<br/>{ customer_id: "zb_cust_456",<br/>  reference_number: "HS-hs_deal_789",<br/>  date: "2026-10-30",<br/>  currency_code: "AED",<br/>  salesperson_name: "Alice Chen",<br/>  line_items: [{ name: "TechNova Project",<br/>    rate: 126000, quantity: 1 }] }
    ZB-->>IS: { salesorder_id: "zb_so_001",<br/>  salesorder_number: "SO-00042",<br/>  total: 126000 }

    IS->>DB: UPDATE deal_links SET zoho_so_id = "zb_so_001"

    %% ─── 9. CREATE 3 MILESTONE INVOICES ─────────────────────────────────────
    Note over IS,ZB: Milestone 1 — Signing (40%, AED 50,400)
    IS->>ZB: POST /invoices<br/>{ salesorder_id: "zb_so_001",<br/>  due_date: today+0,<br/>  line_items: [{ rate: 50400 }],<br/>  notes: "Milestone 1: Contract Signing" }
    ZB-->>IS: { invoice_id: "zb_inv_001",<br/>  invoice_number: "INV-0071" }

    Note over IS,ZB: Milestone 2 — Go-Live (40%, AED 50,400)<br/>Created immediately; due_date = go-live date
    IS->>ZB: POST /invoices<br/>{ salesorder_id: "zb_so_001",<br/>  due_date: closedate+30d,<br/>  line_items: [{ rate: 50400 }],<br/>  notes: "Milestone 2: Go-Live" }
    ZB-->>IS: { invoice_id: "zb_inv_002",<br/>  invoice_number: "INV-0072" }

    Note over IS,ZB: Milestone 3 — 90-Day Support (20%, AED 25,200)
    IS->>ZB: POST /invoices<br/>{ salesorder_id: "zb_so_001",<br/>  due_date: closedate+90d,<br/>  line_items: [{ rate: 25200 }],<br/>  notes: "Milestone 3: 90-Day Post Go-Live" }
    ZB-->>IS: { invoice_id: "zb_inv_003",<br/>  invoice_number: "INV-0073" }

    IS->>DB: UPDATE deal_links<br/>SET zoho_invoice_ids = "zb_inv_001,zb_inv_002,zb_inv_003",<br/>    sync_status = "synced"

    %% ─── 10. WRITE ZOHO IDs BACK TO HUBSPOT ─────────────────────────────────
    IS->>HS: PATCH /crm/v3/objects/deals/hs_deal_789<br/>{ zoho_so_id: "zb_so_001",<br/>  zoho_so_number: "SO-00042",<br/>  zoho_customer_id: "zb_cust_456",<br/>  zoho_invoice_ids: "zb_inv_001,zb_inv_002,zb_inv_003",<br/>  zoho_so_total: 126000,<br/>  zoho_pct_invoiced: 100,<br/>  zoho_pct_collected: 0,<br/>  zoho_amount_unpaid: 126000,<br/>  zoho_amount_uninvoiced: 0,<br/>  zoho_next_due_date: "2026-10-30",<br/>  zoho_sync_status: "synced",<br/>  zoho_last_synced: "2026-10-30T12:14:00Z" }
    HS-->>IS: 200 OK

    IS->>DB: UPDATE job_queue SET status = "done"
    IS->>DB: INSERT sync_log { event: "initial_sync_complete", deal_id, ... }

    %% ─── 11. FINANCE MARKS INVOICE 1 AS SENT ────────────────────────────────
    Note over Finance,ZB: Some time later...
    Finance->>ZB: Mark INV-0071 as "Sent"

    %% ─── 12. ZOHO WEBHOOK FIRES ──────────────────────────────────────────────
    ZB->>IS: POST /webhooks/zoho<br/>{ event: "invoice.status_changed",<br/>  invoice_id: "zb_inv_001",<br/>  status: "sent" }

    IS->>IS: Validate Zoho shared-secret token
    IS->>DB: INSERT job_queue<br/>{ type: "sync_zoho_event",<br/>  invoice_id: "zb_inv_001" }
    IS-->>ZB: 200 OK

    %% ─── 13. SYNC BACK TO HUBSPOT ────────────────────────────────────────────
    IS->>DB: SELECT deal_links WHERE "zb_inv_001" IN zoho_invoice_ids
    DB-->>IS: { hubspot_deal_id: "hs_deal_789", ... }

    IS->>ZB: GET /invoices/zb_inv_001
    ZB-->>IS: { status: "sent", balance: 50400, total: 50400 }

    IS->>ZB: GET /invoices/zb_inv_002
    ZB-->>IS: { status: "draft", balance: 50400, total: 50400 }

    IS->>ZB: GET /invoices/zb_inv_003
    ZB-->>IS: { status: "draft", balance: 25200, total: 25200 }

    IS->>HS: PATCH /crm/v3/objects/deals/hs_deal_789<br/>{ zoho_amount_unpaid: 50400,<br/>  zoho_pct_collected: 0,<br/>  zoho_next_due_date: "2026-10-30",<br/>  zoho_sync_status: "synced",<br/>  zoho_last_synced: "2026-10-30T..." }
    HS-->>IS: 200 OK

    %% ─── 14. FINANCE RECORDS PAYMENT FOR INVOICE 1 ──────────────────────────
    Finance->>ZB: Record payment of AED 50,400<br/>against INV-0071

    ZB->>IS: POST /webhooks/zoho<br/>{ event: "payment.created",<br/>  invoice_id: "zb_inv_001",<br/>  amount: 50400 }

    IS->>DB: INSERT job_queue { type: "sync_zoho_event", invoice_id: "zb_inv_001" }
    IS-->>ZB: 200 OK

    %% ─── 15. SYNC BACK SHOWS 40% COLLECTED ──────────────────────────────────
    IS->>ZB: GET /invoices/zb_inv_001
    ZB-->>IS: { status: "paid", balance: 0, total: 50400 }

    Note over IS: pct_collected = 50400 / 126000 × 100 ≈ 40%<br/>amount_unpaid = 0 (inv001 paid) + 50400 (inv002 sent) + 25200 (draft) = 75,600

    IS->>HS: PATCH /crm/v3/objects/deals/hs_deal_789<br/>{ zoho_pct_collected: 40,<br/>  zoho_amount_unpaid: 75600,<br/>  zoho_next_due_date: "2026-11-29",<br/>  zoho_sync_status: "synced" }
    HS-->>IS: 200 OK

    IS->>DB: INSERT sync_log { event: "payment_synced", amount: 50400 }

    %% ─── 16. PERIODIC SYNC SAFETY NET ───────────────────────────────────────
    Note over IS: Every 15 minutes — catches anything webhooks missed
    IS->>DB: SELECT all deal_links WHERE sync_status != "error_fatal"
    DB-->>IS: [ { hubspot_deal_id: "hs_deal_789", zoho_so_id: "zb_so_001", ... }, ... ]

    IS->>ZB: GET /salesorders/zb_so_001
    ZB-->>IS: { status: "open", total: 126000 }

    IS->>ZB: GET /invoices?salesorder_id=zb_so_001
    ZB-->>IS: [ inv_001 (paid), inv_002 (sent), inv_003 (draft) ]

    Note over IS: No change detected → no HubSpot write needed<br/>(change-detection prevents redundant API calls)

    IS->>DB: UPDATE deal_links SET last_polled_at = now()
```

---

## Key Design Decisions Illustrated

| Decision | Where in Diagram |
|---|---|
| **Acknowledge immediately, work async** | Step 3: IS returns 200 before any Zoho calls |
| **Idempotency before all Zoho writes** | Step 5: deal_links check before any create |
| **All 3 invoices created upfront** | Steps 9a–9c: avoids race conditions with milestone triggers |
| **Webhook-primary, poll-secondary** | Steps 12–15 (webhook path) + Step 16 (periodic fallback) |
| **Change detection in periodic sync** | Step 16: only writes to HubSpot if values differ |

