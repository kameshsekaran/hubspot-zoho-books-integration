# Edge Case Answers

Detailed answers to the seven edge-case scenarios posed in the assignment. Each answer reflects how the integration is actually implemented and why those design decisions were made.

---

## Q1: Zoho Books is unreachable for six hours on the last day of the month. What happens?

### Scenario
A critical Zoho Books outage occurs from, say, 18:00 to 24:00 UTC on the last day of the month. Several deals close during this window.

### What Happens — Step by Step

**1. Webhook acknowledged immediately.**
When HubSpot fires the Closed Won webhook, the integration endpoint returns `200 OK` in under 50 ms and enqueues a `sync_deal` job. HubSpot considers delivery successful. No data is lost at the inbound edge.

**2. Job worker retries with exponential back-off.**
The worker attempts to create the Zoho customer/SO/invoices and receives connection errors or `503` responses. Each failure increments the retry count and pushes the `run_after` timestamp forward:

| Attempt | Wait Before Retry |
|---|---|
| 1st failure | 2 minutes |
| 2nd failure | 4 minutes |
| 3rd failure | 8 minutes |
| 4th failure | 16 minutes |
| … | … (cap at 6 hours) |

**3. HubSpot deal shows `zoho_sync_status = "pending"`.**
The Sales Rep sees the sync field hasn't turned green, but the deal itself is unaffected. The `zoho_sync_error` field shows the last error message (e.g., `"Zoho Books unreachable: ConnectionError"`). No stale or incorrect financial data is written.

**4. Finance Team sees nothing in Zoho.**
No Sales Order or invoices exist yet in Zoho Books — but since nothing was created, there is no *wrong* data. Finance knows to wait; Slack/email alerts fire after 3 consecutive failures per job.

**5. On recovery (Zoho comes back online at midnight).**
The worker picks up all queued jobs normally. Jobs whose `run_after` has passed are processed in order. Each deal is created in Zoho Books with the correct data. HubSpot is updated with Zoho IDs and financial metrics.

**6. Month-end implications.**
Any deals that closed on the last day of the month but failed to sync will have their Sales Orders dated that day (the `closedate` from HubSpot is used as `salesorder.date`). Zoho Books will book the SO under the correct month. However, if Zoho's month-end *lock* is applied before the integration catches up, Finance may need to unlock the period briefly. The integration logs every job with its original enqueue timestamp and the deal's close date, providing a clear audit trail for Finance to reconcile.

> [!IMPORTANT]
> Jobs are **never discarded** during an outage. The persistent `job_queue` table ensures all work is recovered exactly once when connectivity is restored.

---

## Q2: The deal is reopened after the first invoice was sent. What should happen?

### Scenario
A Closed Won deal has its stage changed back to (e.g.) `appointmentscheduled` after `INV-0071` (Milestone 1, 40%) has already been sent to the customer.

### Detection Mechanism
The integration subscribes to `dealstage` property change webhooks. When the stage change arrives, the worker looks up `deal_links` for that deal. If a `zoho_so_id` exists and `zoho_invoice_ids` is non-empty, the worker checks the status of each invoice via the Zoho API.

### What Happens to Invoice 1 (Already Sent)
**We do NOT auto-void Invoice 1.** Voiding a sent invoice is an irreversible accounting action that can have legal and tax implications. Finance must make that decision manually. The integration:
- Sets `zoho_sync_status = "needs_review"` on the HubSpot deal.
- Sets `zoho_sync_error = "Deal reopened after INV-0071 sent. Manual review required."`.
- Sends an alert to the configured Slack channel / email with both the HubSpot deal link and the Zoho SO link.

### What Happens to Invoices 2 and 3 (Draft)
Invoices that are still in `draft` status are safe to void automatically. The integration:
1. Calls `POST /invoices/{invoice_id}/void` for each draft invoice.
2. Removes their IDs from `zoho_invoice_ids` in `deal_links`.
3. Updates `zoho_amount_uninvoiced` and `zoho_pct_invoiced` accordingly.

### How Deal Fields Update
```
zoho_pct_invoiced    = 40%   (only INV-0071 remains active)
zoho_pct_collected   = 0%    (payment not yet received)
zoho_amount_unpaid   = 50400 (INV-0071 sent but unpaid)
zoho_amount_uninvoiced = 75600 (invoices 2 and 3 voided)
zoho_sync_status     = "needs_review"
zoho_sync_error      = "Deal reopened after invoice sent ..."
```

### Manual Override Options
Finance can:
- Void INV-0071 manually in Zoho → integration webhook will detect the void and update HubSpot.
- Re-close the deal in HubSpot → integration will create fresh invoices 2 and 3 (skipping 1, since the SO already exists).
- Contact the integration admin to reset `deal_links.sync_status` to `pending` for a full re-sync.

> [!CAUTION]
> Never auto-void a sent invoice without explicit Finance approval. Sent invoices may have already been received by the customer and could be disputed or form part of a formal contract.

---

## Q3: The deal amount changes after the sales order exists but before the second invoice.

### Scenario
A sales rep edits the `amount` field on a HubSpot deal from AED 126,000 to AED 140,000 after the Sales Order and all three draft invoices have already been created in Zoho Books.

### Detection
The integration subscribes to `amount` property change webhooks. On receiving the change, the worker:
1. Fetches `zoho_so_id` from `deal_links`.
2. Calls `GET /salesorders/{zoho_so_id}` to retrieve `salesorder.total`.
3. Compares `hubspot.amount` vs `zoho_so.total`. If they differ by more than AED 0.01 (floating-point tolerance), an "amount drift" is flagged.

### Why We Don't Auto-Update the Sales Order
Updating a Zoho Sales Order that already has invoices raised against it would:
- Invalidate the line-item totals on existing invoices.
- Potentially cause Invoice 1 (if already sent) to no longer match the SO, creating accounting discrepancies.
- Require recalculating all three milestone amounts and re-issuing any sent invoices — an action that requires Finance judgement.

### What the Integration Does Instead
1. Sets `zoho_sync_status = "amount_drift"` on the HubSpot deal.
2. Writes `zoho_sync_error = "HubSpot amount (140000) differs from Zoho SO total (126000). Manual update required."`.
3. Writes the *actual Zoho SO total* to `zoho_so_total` so HubSpot reflects the confirmed booked value.
4. Fires a Slack/email alert to Finance with a direct link to both records.

### Manual Process to Update
Finance should:
1. Open the Zoho Books Sales Order and edit the line item total.
2. Re-issue or credit-note any affected sent invoices.
3. Create corrected draft invoices for remaining milestones.
4. Once Zoho is updated, the next sync cycle will detect the new `salesorder.total` and clear the `amount_drift` status automatically.

> [!WARNING]
> Auto-updating a Sales Order with live invoices is a breaking change in the accounting record. The integration deliberately **never modifies an existing SO** once invoices exist.

---

## Q4: The CRM sends the same Closed Won event three times, out of order with an earlier stage change.

### Scenario
HubSpot delivers:
- Event A: `dealstage = appointmentscheduled` (timestamp T1)
- Event B: `dealstage = closedwon` (timestamp T2, delivered first due to network delay)
- Event C: `dealstage = closedwon` (duplicate, timestamp T2)
- Event A: `dealstage = appointmentscheduled` delivered late (timestamp T1 < T2, but arrives after B and C)

### Idempotency Check (deal_links)
When Event B is processed first:
1. Worker checks `deal_links` for `hubspot_deal_id`. Not found → proceeds to create Zoho resources.
2. Inserts row into `deal_links` with `sync_status = "synced"`.

When Event C (duplicate Closed Won) arrives:
1. Worker checks `deal_links`. Row found with `zoho_so_id` already set.
2. Worker skips all creation steps. Runs a **sync-only** pass (fetch current Zoho state, update HubSpot fields).
3. No duplicate SO or invoices are created. **Idempotency guaranteed.**

### Job Deduplication in the Queue
Before inserting a new `sync_deal` job, the worker checks for an existing `pending` or `processing` job with the same `deal_id`. Duplicates are dropped at enqueue time, before they even reach the worker.

### Handling Out-of-Order Stage Changes
When Event A (`appointmentscheduled`, T1) arrives late after Event B (`closedwon`, T2):
1. The worker notes the stage change is to a non-closedwon stage.
2. It calls `GET /crm/v3/objects/deals/{id}` to fetch the **current live deal stage** from HubSpot.
3. If the current stage is `closedwon` (T2), the out-of-order stage change is discarded.
4. If the current stage is `appointmentscheduled` (the deal was re-opened after the duplicate processing), the "deal reopened" logic from Q2 applies.

### Result
**Exactly one** Sales Order and one set of three invoices are created, regardless of delivery order or duplication count.

---

## Q5: An invoice is voided in Zoho Books and re-issued under a new number.

### Scenario
Finance voids `INV-0071` (Milestone 1) and creates a corrected invoice `INV-0071b` under a new Zoho invoice ID.

### Zoho Webhook for Void Event
Zoho Books fires a `invoice.status_changed` (to `void`) webhook when the void action is performed.

**Integration receives the void webhook:**
1. Looks up `deal_links` where `zoho_invoice_ids` contains the old invoice ID.
2. Removes the old ID from the comma-separated list.
3. Logs the void event in `sync_log` with the original invoice ID, amount, and timestamp.
4. Recomputes `zoho_pct_invoiced`, `zoho_amount_unpaid`, `zoho_amount_uninvoiced`, `zoho_next_due_date`.
5. Updates HubSpot with new computed values + `zoho_sync_status = "invoice_voided_pending_reissue"`.

**When the new invoice is created in Zoho:**
Zoho fires an `invoice.created` webhook. The integration:
1. Checks whether the new invoice's `salesorder_id` matches a known SO in `deal_links`.
2. If yes, adds the new invoice ID to `zoho_invoice_ids`.
3. Re-fetches all invoice statuses, recomputes all metrics, patches HubSpot.
4. Logs both the void and the re-issue event for full audit trail.

**Finance sees:**
The `sync_log` table (and the `GET /logs` UI) shows a clear sequence: `invoice_voided → invoice_created`. Both the old and new invoice numbers appear in the log. HubSpot `zoho_invoice_ids` reflects only the live invoice after the swap.

> [!NOTE]
> The integration never assumes a fixed number of invoices per deal. `zoho_invoice_ids` is a dynamic list — invoices can be added or removed by Zoho webhook events throughout the deal lifecycle.

---

## Q6: One payment is recorded against two invoices that belong to two different deals.

### Scenario
Finance applies a lump payment in Zoho Books that is split across `INV-0071` (Deal A) and `INV-0081` (Deal B).

### How Zoho Books Handles Split Payments
Zoho Books records the payment as a single **payment record** but associates it with multiple invoices via individual `payment_applied` line items. Each invoice still has its own `balance` and `payment_made` values updated independently.

### How the Integration Handles This
Zoho fires a `payment.created` webhook. The webhook payload includes the payment ID but may reference multiple invoices.

**For each invoice in the payment:**
1. The worker looks up `deal_links` for the invoice ID.
2. If the invoice belongs to a known deal, it fetches the full invoice state from Zoho.
3. It recomputes that deal's financial metrics independently.
4. It patches HubSpot for that specific deal.

Since each invoice's sync is **independent and deal-scoped**, Deal A's HubSpot fields update based on Deal A's invoices only, and Deal B's fields update based on Deal B's invoices only. Neither deal "sees" the other's payment data.

**Cross-deal payment flag:**
If the integration detects that a single payment ID has been applied to invoices from more than one deal, it:
1. Logs a `cross_deal_payment` event in `sync_log` with both deal IDs, invoice IDs, and the payment ID.
2. Sends a Slack/email alert to Finance flagging this for review (it may indicate a customer overpaying or an accounting error).

> [!TIP]
> Each deal's `zoho_pct_collected` and `zoho_amount_unpaid` are always computed from that deal's invoice set only, never from global payment records. This makes the sync logic fully deal-isolated.

---

## Q7: You reach Zoho Books' daily API limit in the afternoon. What does Finance see?

### Scenario
Zoho Books enforces a **daily API call quota** (e.g., 2,000 calls/day on the standard plan). The integration hits this limit at, say, 14:00 UTC.

### How the Rate Limiter Detects This
Zoho returns HTTP `429` with an error body of `{"code": 6041, "message": "Daily API limit exceeded"}`. This is **distinct** from a per-minute throttle (`429` with `Retry-After`). The Zoho client checks the error code and distinguishes between:
- `code 429 + Retry-After` → transient throttle → wait and retry
- `code 6041` (daily quota) → **pause all jobs until midnight UTC**

### What the Integration Does
1. The worker sets a global `api_quota_exhausted` flag in the DB with `resume_at = next midnight UTC`.
2. All pending and future jobs have their `run_after` timestamp set to `resume_at`.
3. No Zoho API calls are attempted until the quota resets.
4. An alert is sent to the integration admin via Slack/email: *"Zoho daily API limit reached at 14:00 UTC. All syncs paused until 00:00 UTC. N jobs queued."*

### What HubSpot Shows
- `zoho_sync_status = "paused"` on all deals that were pending a sync.
- `zoho_sync_error = "Sync paused - API limit reached. Will resume at 00:00 UTC."` on affected deals.
- Previously synced deals retain their **last known values** — stale but visible and clearly labelled with `zoho_last_synced` timestamp.
- Deals that were fully up-to-date before the quota hit show accurate financial data; only deals with pending changes show potentially stale data.

### What Finance Sees
Finance can still see all previously synced data in HubSpot. The `zoho_last_synced` field tells them exactly how old the data is. For deals where `zoho_sync_status = "paused"`, Finance is advised to check Zoho Books directly for the most current information.

### On Recovery (Midnight UTC)
The worker's startup routine checks the `api_quota_exhausted` flag. If `resume_at` is in the past, it:
1. Clears the flag.
2. Processes all queued jobs in priority order (oldest first).
3. Clears `zoho_sync_error` and sets `zoho_sync_status = "synced"` on each deal as it processes.

### No Data is Lost
All HubSpot webhook events received during the pause are sitting in the `job_queue` table. Zoho webhook deliveries that fail during the pause are retried by Zoho (Zoho retries webhooks for up to 3 days). The net result: all events are eventually processed, no data is lost.

> [!IMPORTANT]
> The long-term fix for quota exhaustion is the **change-detection optimisation**: only calling the Zoho API for deals whose data has actually changed (detected via webhooks), rather than polling all deals every 15 minutes. With 20 active deals and webhooks as the primary sync mechanism, daily Zoho API calls drop from ~9,600 to well under 500. See `docs/api-call-count.md` for the full analysis.

