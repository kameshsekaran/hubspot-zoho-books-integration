# HubSpot ↔ Zoho Books Integration

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?logo=fastapi&logoColor=white)
![HubSpot](https://img.shields.io/badge/CRM-HubSpot-FF7A59?logo=hubspot&logoColor=white)
![Zoho Books](https://img.shields.io/badge/Accounting-Zoho%20Books-E42527?logo=zoho&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)
![Render](https://img.shields.io/badge/Deploy-Render.com-46E3B7?logo=render&logoColor=white)

---

## Description

This project is a production-quality, bidirectional integration service between **HubSpot CRM** and **Zoho Books**. When a sales deal is marked *Closed Won* in HubSpot, the service automatically creates a matching **Sales Order** and three **milestone invoices** (40% Signing / 40% Go-Live / 20% 90-Day) in Zoho Books — no manual Finance entry required. As invoices are sent and payments recorded in Zoho, computed financial metrics (`% collected`, `amount unpaid`, `next due date`, `overdue flag`) are synced back to the HubSpot deal record in near real-time via webhooks, giving both the sales team and management instant financial visibility without switching applications. The service is built on FastAPI for low-latency webhook acknowledgement, a persistent job queue with exponential-backoff retry, APScheduler for periodic reconciliation, and SQLAlchemy for idempotency tracking — all deployable to Render.com with a single `render.yaml`.

---

## Architecture

See **[docs/architecture.md](docs/architecture.md)** for the full component diagram and descriptions.

Quick overview:

```
HubSpot Webhook  ──►  FastAPI /webhooks/hubspot  ──►  Job Queue  ──►  Worker
                                                                         │
Zoho Webhook     ──►  FastAPI /webhooks/zoho     ──►  Job Queue  ──►   │
                                                                         ▼
                                                              HubSpot Client
                                                              Zoho Books Client
                                                              SQLite / PostgreSQL
```

---

## Why HubSpot?

HubSpot was chosen as the CRM for this integration for several compelling reasons:

| Criterion | HubSpot | Alternatives |
|---|---|---|
| **Cost** | Free forever (no credit card required for CRM + Private Apps) | Salesforce: $25+/user/mo; Pipedrive: $15+/user/mo |
| **API quality** | Comprehensive REST API with clear versioning (`/crm/v3/`), strong webhook support, and excellent documentation | Varies widely |
| **Webhooks** | Native outbound webhooks on any deal property change — no polling required | Some CRMs require polling or third-party middleware |
| **Custom Properties** | Can create deal properties via API (`/properties`) — automatable with a setup script | Some CRMs require UI configuration |
| **Developer Experience** | Interactive API explorer, great Postman collection, free sandbox | Salesforce sandbox is complex to set up |
| **Market position** | Industry standard for SMB/startup sales teams; realistic for a production assignment | |

The free HubSpot account provides everything needed: CRM records, deal pipelines, contact management, custom properties, webhook subscriptions, and Private App API keys — all without a credit card.

---

## Setup Instructions

### Prerequisites
- Python 3.11+
- A free HubSpot account
- A Zoho Books trial account

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/hubspot-zoho-integration.git
cd hubspot-zoho-integration
```

### 2. Create HubSpot Free Account + Private App

1. Go to [app.hubspot.com](https://app.hubspot.com) and sign up for a free account.
2. Navigate to **Settings → Integrations → Private Apps**.
3. Click **Create a private app**.
4. Give it a name: `Zoho Books Integration`.
5. Under **Scopes**, select:
   - `crm.objects.deals.read`
   - `crm.objects.deals.write`
   - `crm.objects.companies.read`
   - `crm.objects.contacts.read`
   - `crm.schemas.deals.write` *(for creating custom properties)*
6. Click **Create app** and copy the **Access Token**.

### 3. Create Zoho Books Trial + OAuth App

1. Sign up at [zoho.com/books](https://www.zoho.com/books/) (free 14-day trial, no credit card).
2. Go to [api-console.zoho.com](https://api-console.zoho.com).
3. Click **Add Client** → choose **Server-based Applications**.
4. Set the redirect URI to `http://localhost:8000/oauth/zoho/callback`.
5. Note your **Client ID** and **Client Secret**.
6. Follow the [OAuth flow below](#getting-the-zoho-refresh-token) to get your refresh token.
7. Find your **Organization ID** in Zoho Books under **Settings → Organization Profile**.

### 4. Configure Environment Variables

```bash
cp .env.example .env
```

Open `.env` and fill in all values:

```env
# HubSpot
HUBSPOT_ACCESS_TOKEN=pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
HUBSPOT_WEBHOOK_SECRET=your_hubspot_webhook_client_secret

# Zoho Books
ZOHO_CLIENT_ID=1000.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
ZOHO_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ZOHO_REFRESH_TOKEN=1000.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ZOHO_ORG_ID=xxxxxxxx
ZOHO_WEBHOOK_SECRET=your_chosen_random_secret_for_zoho_webhooks

# Database (SQLite for dev, PostgreSQL for prod)
DATABASE_URL=sqlite:///./integration.db
# DATABASE_URL=postgresql://user:password@host:5432/dbname

# Notifications (optional)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx/yyy/zzz
ALERT_EMAIL=finance@yourcompany.com

# App
LOG_LEVEL=INFO
ENVIRONMENT=development
```

### 5. Install Dependencies

```bash
pip install -r requirements.txt
```

### 6. Run Database Migrations

```bash
alembic upgrade head
```

This creates the `deal_links`, `sync_log`, and `job_queue` tables.

### 7. Create HubSpot Custom Properties

```bash
python scripts/setup_hubspot_properties.py
```

This idempotent script creates all 14 custom deal properties (`zoho_so_id`, `zoho_pct_collected`, etc.) in the `zoho_integration` property group. Safe to re-run.

### 8. Start the Integration Service

```bash
uvicorn app.main:app --reload --port 8000
```

### 9. View the Integration Dashboard

Open [http://localhost:8000](http://localhost:8000) in your browser. You'll see a live table of all sync events, deal statuses, and job queue depth.

---

## Getting the Zoho Refresh Token

The refresh token is a one-time OAuth exchange. Steps:

1. Ensure the app is running locally (`uvicorn app.main:app --reload`).

2. Visit this URL in your browser (replace `{CLIENT_ID}` and `{ORG_ID}`):
   ```
   https://accounts.zoho.com/oauth/v2/auth?scope=ZohoBooks.fullaccess.all&client_id={CLIENT_ID}&response_type=code&redirect_uri=http://localhost:8000/oauth/zoho/callback&access_type=offline
   ```

3. Log in to Zoho and click **Accept**.

4. Zoho redirects to `http://localhost:8000/oauth/zoho/callback?code=XXXX`.

5. The integration automatically exchanges the code for tokens and saves the refresh token to the database. You'll see:
   ```json
   {"status": "ok", "message": "Zoho refresh token saved successfully."}
   ```

6. Copy the refresh token from the response (also printed to the server log) and add it to your `.env` as `ZOHO_REFRESH_TOKEN` for backup.

> [!IMPORTANT]
> The refresh token does not expire unless revoked. Store it securely. Never commit it to version control.

---

## HubSpot Webhook Setup

1. Go to **HubSpot Settings → Integrations → Private Apps → [Your App] → Webhooks**.
2. Click **Create subscription**.
3. Set the target URL to your deployed service: `https://your-app.onrender.com/webhooks/hubspot`
   - For local testing, use [ngrok](https://ngrok.com): `ngrok http 8000` and use the HTTPS URL.
4. Subscribe to the following events:
   - **Object type**: Deal
   - **Event type**: `propertyChange`
   - **Properties**: `dealstage`, `amount`, `closedate`
5. Copy the **Client Secret** shown under the webhook settings and set it as `HUBSPOT_WEBHOOK_SECRET` in `.env`.

---

## Seeding Test Data

Seed a sample deal (TechNova Solutions LLC, AED 126,000) through the full lifecycle:

```bash
python scripts/seed_data.py
```

This script:
1. Creates a test company and contact in HubSpot (if they don't exist).
2. Creates a deal named "TechNova Solutions — ERP Implementation".
3. Sets the deal stage to `closedwon`, triggering the integration.
4. Waits 5 seconds, then checks the deal for `zoho_so_id` to confirm sync.

Expected output:
```
✓ Created HubSpot company: TechNova Solutions LLC (id: 12345678)
✓ Created HubSpot contact: Mohammed Al-Rashidi (id: 87654321)
✓ Created HubSpot deal: AED 126,000 (id: hs_deal_789)
✓ Set deal stage to closedwon
⏳ Waiting for sync...
✓ Sync complete! zoho_so_id=zb_so_001, zoho_so_number=SO-00042
```

---

## Running Tests

```bash
pytest tests/ -v
```

Key test files:

| File | Coverage |
|---|---|
| `tests/test_webhook_validation.py` | HMAC signature checks, replay attack prevention |
| `tests/test_idempotency.py` | Duplicate event handling, deal_links deduplication |
| `tests/test_zoho_client.py` | OAuth refresh, rate limiting, retry logic (mocked) |
| `tests/test_hubspot_client.py` | Deal fetch, property update, error handling |
| `tests/test_field_mapping.py` | All 23 field mappings, edge cases (missing fields) |
| `tests/test_edge_cases.py` | Q1–Q7 scenarios from the assignment |
| `tests/test_sync_engine.py` | Computed metric accuracy, change detection |

Run with coverage:
```bash
pytest tests/ -v --cov=app --cov-report=html
open htmlcov/index.html
```

---

## Deployment to Render.com

The repository includes a `render.yaml` for one-click deployment:

```bash
# Install Render CLI (optional)
npm install -g @render-com/cli

# Or just push to GitHub and connect the repo in Render dashboard
git push origin main
```

Render will:
1. Detect `render.yaml` and provision the web service + PostgreSQL database.
2. Run `pip install -r requirements.txt && alembic upgrade head` on each deploy.
3. Start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Set environment variables from the Render dashboard (Settings → Environment).

> [!TIP]
> Set `DATABASE_URL` in Render's environment to the **Internal Database URL** provided by the Render PostgreSQL service — it's faster and free within the same region.

---

## Documentation & Diagrams

| Document | Description |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System architecture with Mermaid flowchart |
| [docs/sequence-diagram.md](docs/sequence-diagram.md) | Full deal lifecycle sequence diagram |
| [docs/field-mapping.md](docs/field-mapping.md) | Complete HubSpot ↔ Zoho Books field mapping table |
| [docs/answers.md](docs/answers.md) | Detailed answers to all 7 edge-case questions |
| [docs/api-call-count.md](docs/api-call-count.md) | API call analysis and quota optimisation |

---

## Example Deal

The integration was designed and tested around this representative deal:

| Field | Value |
|---|---|
| Company | TechNova Solutions LLC |
| Contact | Mohammed Al-Rashidi, CTO |
| Email | cto@technova.ae |
| Deal Name | TechNova Solutions — ERP Implementation |
| Amount | AED 126,000 |
| Currency | AED (UAE Dirham) |
| Close Date | 2026-10-30 |
| Deal Owner | Alice Chen |
| **Milestone 1** | AED 50,400 (40%) — Contract Signing |
| **Milestone 2** | AED 50,400 (40%) — Go-Live (+30 days) |
| **Milestone 3** | AED 25,200 (20%) — 90-Day Post Go-Live |

---

## Reviewer Credentials

> [!NOTE]
> Credentials for the live demo environment will be shared separately in the submission email. The following is a placeholder for the reviewer.

| Service | URL | Credentials |
|---|---|---|
| Integration Dashboard | `https://hubspot-zoho-integration.onrender.com` | *(see email)* |
| HubSpot CRM (test account) | `https://app.hubspot.com` | *(see email)* |
| Zoho Books (test account) | `https://books.zoho.com` | *(see email)* |
| API Docs (Swagger UI) | `https://hubspot-zoho-integration.onrender.com/docs` | Public |

---

## API Endpoints Reference

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/` | Integration dashboard (HTML) | None |
| `GET` | `/health` | Health check — returns `{"status":"ok"}` | None |
| `GET` | `/docs` | Swagger UI / OpenAPI docs | None |
| `POST` | `/webhooks/hubspot` | Receives HubSpot deal webhooks | HMAC-SHA256 |
| `POST` | `/webhooks/zoho` | Receives Zoho Books webhooks | Shared secret |
| `GET` | `/logs` | Paginated sync event log | API key |
| `GET` | `/logs/{deal_id}` | Sync history for a specific deal | API key |
| `GET` | `/status` | Queue depth, last sync times, quota status | API key |
| `POST` | `/sync/{deal_id}` | Manually trigger sync for a deal | API key |
| `GET` | `/oauth/zoho/callback` | OAuth callback for initial token setup | None |

---

## Project Structure

```
hubspot-zoho-integration/
├── app/
│   ├── main.py              # FastAPI app, routes, startup/shutdown
│   ├── models.py            # SQLAlchemy models (deal_links, sync_log, job_queue)
│   ├── database.py          # DB engine and session factory
│   ├── worker.py            # Async job worker
│   ├── queue.py             # Job queue helpers
│   ├── scheduler.py         # APScheduler periodic sync
│   ├── hubspot_client.py    # HubSpot API client with retry
│   ├── zoho_client.py       # Zoho Books API client with OAuth + rate limiting
│   ├── sync_engine.py       # Core sync logic and field mapping
│   ├── security.py          # Webhook signature validation
│   └── config.py            # Pydantic settings from .env
├── scripts/
│   ├── setup_hubspot_properties.py  # Create all custom HubSpot properties
│   └── seed_data.py                 # Create test deal in HubSpot
├── tests/
│   ├── conftest.py
│   ├── test_webhook_validation.py
│   ├── test_idempotency.py
│   ├── test_zoho_client.py
│   ├── test_hubspot_client.py
│   ├── test_field_mapping.py
│   ├── test_edge_cases.py
│   └── test_sync_engine.py
├── alembic/                 # Database migrations
├── docs/                    # Documentation (you are here)
├── .env.example             # Template for environment variables
├── render.yaml              # Render.com deployment config
├── requirements.txt
└── README.md
```

---

*Built as a Full-Stack Developer Intern assignment submission. All code, tests, and documentation written from scratch.*

