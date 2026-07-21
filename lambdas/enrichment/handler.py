"""
lambdas/enrichment/handler.py

Ticket enrichment Lambda.
Triggered by TicketCreated events from EventBridge.
Fetches account context from Salesforce and Canvasly admin portal,
flags enterprise accounts, stores enriched ticket in Postgres.

AWS Lambda entry point: handler(event, context)
Local dev entry point:  uvicorn app (FastAPI wrapper on port 8011)
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import psycopg2
import psycopg2.pool
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
DB_HOST        = os.environ.get("DB_HOST", "localhost")
DB_PORT        = int(os.environ.get("DB_PORT", "5432"))
DB_NAME        = os.environ.get("DB_NAME", "canvasly")
DB_USER        = os.environ.get("DB_USER", "canvasly")
DB_PASSWORD    = os.environ.get("DB_PASSWORD", "canvasly_dev")

SALESFORCE_API_URL      = os.environ.get("SALESFORCE_API_URL", "http://localhost:8002")
CANVASLY_ADMIN_API_URL  = os.environ.get("CANVASLY_ADMIN_API_URL", "http://localhost:8003")

ENTERPRISE_SEAT_THRESHOLD = int(os.environ.get("ENTERPRISE_SEAT_THRESHOLD", "100"))
ENTERPRISE_ARR_THRESHOLD  = float(os.environ.get("ENTERPRISE_ARR_THRESHOLD", "50000"))

# Connection pool — max 5 conns per Lambda instance
# Mirrors RDS Proxy pooling behaviour in production
_pool: psycopg2.pool.SimpleConnectionPool | None = None


def get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=5,
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
        )
    return _pool


@contextmanager
def get_conn():
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ─────────────────────────────────────────────
# External API fetchers
# ─────────────────────────────────────────────

def fetch_salesforce_account(ticket_id: str) -> dict:
    """
    Fetches account data from Salesforce by ticket ID.
    Returns empty dict on failure — enrichment continues with partial data.
    """
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(
                f"{SALESFORCE_API_URL}/services/data/v58.0/sobjects/Account/byTicket/{ticket_id}"
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("Salesforce fetch failed for %s: %s", ticket_id, exc)
        return {}


def fetch_admin_portal_account(account_id: str) -> dict:
    """
    Fetches account data from Canvasly admin portal.
    Returns empty dict on failure.
    """
    if not account_id:
        return {}
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(
                f"{CANVASLY_ADMIN_API_URL}/api/accounts/{account_id}"
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("Admin portal fetch failed for account %s: %s", account_id, exc)
        return {}


# ─────────────────────────────────────────────
# Enterprise flagging
# ─────────────────────────────────────────────

def is_enterprise(sf_data: dict) -> bool:
    """
    Returns True if account meets enterprise thresholds.
    Defaults to False (conservative) if data is unavailable.
    """
    seat_count = sf_data.get("seat_count", 0) or 0
    arr        = float(sf_data.get("arr", 0) or 0)
    tier       = sf_data.get("tier", "")
    return (
        seat_count >= ENTERPRISE_SEAT_THRESHOLD
        or arr >= ENTERPRISE_ARR_THRESHOLD
        or tier == "enterprise"
    )


# ─────────────────────────────────────────────
# Database write
# ─────────────────────────────────────────────

INSERT_TICKET_SQL = """
INSERT INTO tickets (
    ticket_id, created_at, channel, category, priority_assigned,
    first_response_min, resolution_min, agent_name, escalated,
    csat_score, agent_internal_notes,
    account_id, account_name, is_enterprise, account_arr,
    account_tier, account_seat_count, renewal_date, account_owner
)
VALUES (
    %(ticket_id)s, %(created_at)s, %(channel)s, %(category)s, %(priority_assigned)s,
    %(first_response_min)s, %(resolution_min)s, %(agent_name)s, %(escalated)s,
    %(csat_score)s, %(agent_internal_notes)s,
    %(account_id)s, %(account_name)s, %(is_enterprise)s, %(account_arr)s,
    %(account_tier)s, %(account_seat_count)s, %(renewal_date)s, %(account_owner)s
)
ON CONFLICT (ticket_id) DO UPDATE SET
    account_id          = EXCLUDED.account_id,
    account_name        = EXCLUDED.account_name,
    is_enterprise       = EXCLUDED.is_enterprise,
    account_arr         = EXCLUDED.account_arr,
    account_tier        = EXCLUDED.account_tier,
    account_seat_count  = EXCLUDED.account_seat_count,
    renewal_date        = EXCLUDED.renewal_date,
    account_owner       = EXCLUDED.account_owner,
    updated_at          = now();
"""


def upsert_ticket(ticket: dict, sf: dict, admin: dict) -> None:
    renewal_raw = sf.get("renewal_date")
    renewal_date = None
    if renewal_raw:
        try:
            renewal_date = datetime.strptime(renewal_raw, "%Y-%m-%d").date()
        except ValueError:
            pass

    params = {
        "ticket_id":          ticket["ticket_id"],
        "created_at":         ticket.get("created_at"),
        "channel":            ticket.get("channel"),
        "category":           ticket.get("category"),
        "priority_assigned":  ticket.get("priority"),
        "first_response_min": ticket.get("first_response_min"),
        "resolution_min":     ticket.get("resolution_min"),
        "agent_name":         ticket.get("agent_name"),
        "escalated":          bool(ticket.get("escalated", False)),
        "csat_score":         ticket.get("csat_score"),
        "agent_internal_notes": ticket.get("internal_notes"),
        "account_id":         sf.get("account_id"),
        "account_name":       sf.get("account_name"),
        "is_enterprise":      is_enterprise(sf),
        "account_arr":        sf.get("arr"),
        "account_tier":       sf.get("tier"),
        "account_seat_count": sf.get("seat_count"),
        "renewal_date":       renewal_date,
        "account_owner":      sf.get("account_owner"),
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(INSERT_TICKET_SQL, params)
    logger.info("Upserted ticket %s (enterprise=%s)", ticket["ticket_id"], params["is_enterprise"])


# ─────────────────────────────────────────────
# Core enrichment logic
# ─────────────────────────────────────────────

def enrich_ticket(ticket: dict) -> dict:
    ticket_id = ticket["ticket_id"]
    logger.info("Enriching ticket %s", ticket_id)

    sf   = fetch_salesforce_account(ticket_id)
    admin = fetch_admin_portal_account(sf.get("account_id", ""))
    upsert_ticket(ticket, sf, admin)

    enriched = {
        **ticket,
        "account_id":         sf.get("account_id"),
        "account_name":       sf.get("account_name"),
        "is_enterprise":      is_enterprise(sf),
        "account_arr":        sf.get("arr"),
        "account_tier":       sf.get("tier"),
        "account_seat_count": sf.get("seat_count"),
        "renewal_date":       sf.get("renewal_date"),
        "account_owner":      sf.get("account_owner"),
        "feature_flags":      admin.get("feature_flags", {}),
        "usage":              admin.get("usage", {}),
    }

    # Publish TicketEnriched event for triage_agent to consume
    from agents.events import publish_event  # shared utility — avoids circular import
    publish_event("TicketEnriched", enriched)

    return enriched


# ─────────────────────────────────────────────
# AWS Lambda entry point
# ─────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    """AWS Lambda handler — triggered by EventBridge TicketCreated rule."""
    try:
        detail = event.get("detail", event)
        result = enrich_ticket(detail)
        return {"statusCode": 200, "body": json.dumps({"status": "enriched", "ticket_id": result["ticket_id"]})}
    except Exception as exc:
        logger.exception("Enrichment failed for event: %s", event)
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}


# ─────────────────────────────────────────────
# Local dev FastAPI wrapper
# ─────────────────────────────────────────────

app = FastAPI(title="Enrichment Lambda")

@app.post("/invoke")
async def invoke(request: Request):
    body = await request.json()
    detail = body.get("detail", body)
    try:
        result = enrich_ticket(detail)
        return JSONResponse({"status": "enriched", "ticket_id": result["ticket_id"]})
    except Exception as exc:
        logger.exception("Enrichment error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/health")
def health():
    return {"status": "ok", "service": "enrichment"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8011, log_level="info")
