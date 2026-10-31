"""
FastAPI application — entry point and route definitions.

Exports the ``app`` instance that uvicorn loads.

Routes:
    GET  /                  — Integration dashboard (HTML table of recent syncs)
    GET  /health            — Health check
    GET  /docs              — Swagger UI (built-in to FastAPI)
    POST /webhooks/hubspot  — HubSpot webhook receiver
    POST /webhooks/zoho     — Zoho Books webhook receiver
    GET  /oauth/zoho/callback — Zoho OAuth2 callback (initial setup)
    GET  /logs              — Paginated sync event log (JSON)
    GET  /logs/{deal_id}    — Sync history for one deal
    GET  /status            — Queue depth + quota status
    POST /sync/{deal_id}    — Manually trigger a deal sync
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.database import get_db, init_db, verify_db_connection
from app.models.job import JobType
from app.models.link import DealLink
from app.models.log import IntegrationLog
from app.queue import enqueue, get_queue_depth
from app.scheduler import start_scheduler, stop_scheduler
from app.security import validate_hubspot_signature, validate_zoho_signature
from app.worker import run_worker, stop_worker
from app.zoho.auth import exchange_code_for_tokens, get_authorization_url
from app.zoho.client import get_daily_call_count

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="HubSpot ↔ Zoho Books Integration",
    description=(
        "Bidirectional synchronisation between HubSpot CRM and Zoho Books. "
        "Triggered by webhooks; queued for reliable async processing."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Background worker task handle
_worker_task: Optional[asyncio.Task] = None
_worker_stop: Optional[asyncio.Event] = None


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    """Initialise DB, start scheduler, and launch worker."""
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    logger.info("Starting HubSpot ↔ Zoho Books integration service...")

    init_db()

    # Start periodic scheduler
    start_scheduler()

    # Start background worker
    global _worker_task, _worker_stop
    _worker_stop = asyncio.Event()
    _worker_task = asyncio.create_task(run_worker(_worker_stop))
    logger.info("Integration service started.")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Gracefully stop the worker and scheduler."""
    logger.info("Shutting down integration service...")
    stop_scheduler()
    await stop_worker()
    if _worker_task:
        try:
            await asyncio.wait_for(_worker_task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Worker did not stop within timeout; cancelling.")
            _worker_task.cancel()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Health"])
async def health() -> dict:
    """Return a simple health check response."""
    db_ok = verify_db_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "version": "1.0.0",
        "environment": settings.environment,
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
async def dashboard(db=Depends(get_db)) -> HTMLResponse:
    """
    Serve a simple HTML dashboard showing recent sync events and queue depth.
    """
    logs = (
        db.query(IntegrationLog)
        .order_by(IntegrationLog.created_at.desc())
        .limit(50)
        .all()
    )
    depths = get_queue_depth(db)
    links = db.query(DealLink).order_by(DealLink.updated_at.desc()).limit(20).all()

    rows = "".join(
        f"<tr>"
        f"<td>{l.created_at.strftime('%Y-%m-%d %H:%M:%S') if l.created_at else ''}</td>"
        f"<td>{l.deal_id or ''}</td>"
        f"<td>{l.system}</td>"
        f"<td>{l.method} {l.endpoint.split('/')[-1]}</td>"
        f"<td>{'✅' if l.success else '❌'} {l.status_code or ''}</td>"
        f"<td>{l.duration_ms or ''}ms</td>"
        f"<td>{(l.error_message or '')[:80]}</td>"
        f"</tr>"
        for l in logs
    )

    deal_rows = "".join(
        f"<tr>"
        f"<td>{d.hs_deal_id}</td>"
        f"<td>{d.zoho_so_number or ''}</td>"
        f"<td>{d.zoho_so_id or ''}</td>"
        f"<td>{', '.join(d.get_invoice_ids())}</td>"
        f"<td>{d.updated_at.strftime('%Y-%m-%d %H:%M') if d.updated_at else ''}</td>"
        f"</tr>"
        for d in links
    )

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>HubSpot ↔ Zoho Books Integration</title>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="30">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
            h1 {{ color: #2c3e50; }}
            h2 {{ color: #34495e; margin-top: 30px; }}
            table {{ border-collapse: collapse; width: 100%; background: white;
                     box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 4px; }}
            th {{ background: #2c3e50; color: white; padding: 8px 12px; text-align: left; }}
            td {{ padding: 6px 12px; border-bottom: 1px solid #eee; font-size: 13px; }}
            tr:hover {{ background: #f9f9f9; }}
            .badge {{ padding: 2px 8px; border-radius: 3px; font-size: 11px; }}
            .ok {{ background: #d4edda; color: #155724; }}
            .warn {{ background: #fff3cd; color: #856404; }}
            .stat {{ display: inline-block; background: white; padding: 15px 25px;
                     margin: 5px; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        </style>
    </head>
    <body>
        <h1>🔄 HubSpot ↔ Zoho Books Integration</h1>
        <p>Environment: <strong>{settings.environment}</strong> &nbsp;|&nbsp;
           Auto-refreshes every 30 seconds.</p>

        <h2>📊 Queue Status</h2>
        {"".join(f'<div class="stat"><strong>{k.title()}</strong><br>{v} jobs</div>' for k, v in depths.items())}
        <div class="stat"><strong>Zoho API calls today</strong><br>{get_daily_call_count()}</div>

        <h2>🤝 Active Deals</h2>
        <table>
            <thead><tr>
                <th>HubSpot Deal ID</th><th>SO Number</th><th>Zoho SO ID</th>
                <th>Invoice IDs</th><th>Last Updated</th>
            </tr></thead>
            <tbody>{deal_rows}</tbody>
        </table>

        <h2>📋 Recent Sync Events (last 50)</h2>
        <table>
            <thead><tr>
                <th>Timestamp</th><th>Deal ID</th><th>System</th>
                <th>Action</th><th>Status</th><th>Duration</th><th>Error</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Webhook endpoints
# ---------------------------------------------------------------------------


@app.post("/webhooks/hubspot", status_code=200, tags=["Webhooks"])
async def hubspot_webhook(request: Request, db=Depends(get_db)) -> dict:
    """
    Receive and enqueue HubSpot webhook events.

    Validates the HMAC-SHA256 signature, then enqueues a job for the worker.
    Always returns 200 immediately (work is done asynchronously).
    """
    await validate_hubspot_signature(request)

    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    # HubSpot sends an array of events
    if not isinstance(payload, list):
        payload = [payload]

    enqueued = 0
    for event in payload:
        deal_id = str(event.get("objectId") or event.get("object_id") or "")
        if not deal_id:
            continue

        prop = event.get("propertyName") or event.get("property_name") or ""
        new_value = event.get("propertyValue") or event.get("property_value") or ""

        # Only handle stage changes and amount changes
        if prop not in ("dealstage", "amount", "closedate", ""):
            continue

        # Determine job type
        if prop == "dealstage" and new_value == settings.hubspot_closed_won_stage_id:
            job_type = JobType.CRM_TO_ZOHO
        elif prop == "dealstage" and new_value:
            job_type = JobType.CRM_TO_ZOHO  # re-open handled by worker
        else:
            job_type = JobType.CRM_TO_ZOHO

        enqueue(
            db,
            deal_id=deal_id,
            job_type=job_type,
            metadata={
                "event": "deal.propertyChange",
                "property": prop,
                "new_value": new_value,
                "event_id": event.get("eventId"),
            },
        )
        enqueued += 1

    return {"status": "queued", "events": enqueued}


@app.post("/webhooks/zoho", status_code=200, tags=["Webhooks"])
async def zoho_webhook(request: Request, db=Depends(get_db)) -> dict:
    """
    Receive and enqueue Zoho Books webhook events.

    Validates the shared-secret token, then enqueues a job for the worker.
    Always returns 200 immediately.
    """
    await validate_zoho_signature(request)

    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    event_type = payload.get("event_type") or payload.get("event") or "unknown"
    invoice_id = payload.get("invoice_id") or payload.get("Invoice", {}).get("invoice_id")
    payment_id = payload.get("payment_id") or payload.get("Payment", {}).get("payment_id")
    so_id = payload.get("salesorder_id")

    # Find deal_id from invoice or SO
    deal_id = ""
    if invoice_id:
        link = db.query(DealLink).filter(
            DealLink.zoho_invoice_ids.like(f"%{invoice_id}%")
        ).first()
        if link:
            deal_id = link.hs_deal_id
    if not deal_id and so_id:
        link = db.query(DealLink).filter(DealLink.zoho_so_id == so_id).first()
        if link:
            deal_id = link.hs_deal_id

    if not deal_id:
        deal_id = f"zoho-{invoice_id or so_id or 'unknown'}"

    enqueue(
        db,
        deal_id=deal_id,
        job_type=JobType.ZOHO_TO_CRM,
        metadata={
            "event_type": event_type,
            "invoice_id": invoice_id,
            "payment_id": payment_id,
            "so_id": so_id,
        },
    )
    return {"status": "queued", "event_type": event_type}


# ---------------------------------------------------------------------------
# OAuth callback
# ---------------------------------------------------------------------------


@app.get("/oauth/zoho/callback", tags=["OAuth"])
async def zoho_oauth_callback(
    code: str = Query(..., description="Authorization code from Zoho"),
    db=Depends(get_db),
) -> dict:
    """
    Exchange a Zoho OAuth2 authorization code for tokens.

    Visit the Zoho authorization URL to trigger this callback:
        ``/oauth/zoho/authorize``
    """
    token = exchange_code_for_tokens(code, db)
    logger.info(
        "Zoho OAuth tokens stored. Refresh token: %s…",
        token.refresh_token[:20],
    )
    return {
        "status": "ok",
        "message": "Zoho refresh token saved successfully.",
        "refresh_token_prefix": token.refresh_token[:20] + "…",
    }


@app.get("/oauth/zoho/authorize", tags=["OAuth"])
async def zoho_oauth_authorize() -> dict:
    """Return the Zoho authorization URL for the initial OAuth flow."""
    url = get_authorization_url()
    return {"authorization_url": url, "instructions": "Open this URL in your browser."}


# ---------------------------------------------------------------------------
# Logs & monitoring
# ---------------------------------------------------------------------------


@app.get("/logs", tags=["Monitoring"])
async def get_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db=Depends(get_db),
) -> dict:
    """Return a paginated list of sync event log entries."""
    offset = (page - 1) * page_size
    total = db.query(IntegrationLog).count()
    logs = (
        db.query(IntegrationLog)
        .order_by(IntegrationLog.created_at.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_log_to_dict(l) for l in logs],
    }


@app.get("/logs/{deal_id}", tags=["Monitoring"])
async def get_deal_logs(deal_id: str, db=Depends(get_db)) -> dict:
    """Return all sync log entries for a specific deal."""
    logs = (
        db.query(IntegrationLog)
        .filter(IntegrationLog.deal_id == deal_id)
        .order_by(IntegrationLog.created_at.desc())
        .all()
    )
    return {"deal_id": deal_id, "count": len(logs), "items": [_log_to_dict(l) for l in logs]}


@app.get("/status", tags=["Monitoring"])
async def get_status(db=Depends(get_db)) -> dict:
    """Return current queue depth, API call counts, and last sync times."""
    depths = get_queue_depth(db)
    daily_calls = get_daily_call_count()

    last_log = (
        db.query(IntegrationLog)
        .filter(IntegrationLog.success == True)
        .order_by(IntegrationLog.created_at.desc())
        .first()
    )

    active_deals = db.query(DealLink).filter(DealLink.zoho_so_id.isnot(None)).count()

    return {
        "status": "ok",
        "queue_depth": depths,
        "zoho_daily_calls": daily_calls,
        "zoho_daily_limit": settings.zoho_daily_limit,
        "active_deals": active_deals,
        "last_successful_sync": last_log.created_at.isoformat() if last_log else None,
    }


# ---------------------------------------------------------------------------
# Manual sync trigger
# ---------------------------------------------------------------------------


@app.post("/sync/{deal_id}", tags=["Operations"])
async def manual_sync(deal_id: str, db=Depends(get_db)) -> dict:
    """
    Manually trigger a Zoho → HubSpot sync for a specific deal.

    Useful for recovering from errors or testing without waiting for the
    periodic scheduler.
    """
    link = db.query(DealLink).filter(DealLink.hs_deal_id == deal_id).first()
    if not link:
        raise HTTPException(
            status_code=404, detail=f"Deal {deal_id} not found in deal_links."
        )

    job = enqueue(
        db,
        deal_id=deal_id,
        job_type=JobType.ZOHO_TO_CRM,
        metadata={"event_type": "manual_sync"},
        deduplicate=False,
    )
    return {
        "status": "queued",
        "deal_id": deal_id,
        "job_id": job.id if job else None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_to_dict(log: IntegrationLog) -> dict[str, Any]:
    """Convert an IntegrationLog ORM object to a JSON-serialisable dict."""
    return {
        "id": log.id,
        "deal_id": log.deal_id,
        "direction": log.direction,
        "system": log.system,
        "method": log.method,
        "endpoint": log.endpoint,
        "status_code": log.status_code,
        "duration_ms": log.duration_ms,
        "success": log.success,
        "error_message": log.error_message,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }

