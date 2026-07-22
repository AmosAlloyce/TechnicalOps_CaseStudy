"""
lambdas/escalation/handler.py

After-hours enterprise escalation Lambda.
Triggered by TicketTriaged events where routing == "escalate".
Pages the on-call rotation when enterprise + high priority + outside business hours.

AWS Lambda entry point: handler(event, context)
Local dev entry point:  uvicorn app (FastAPI wrapper on port 8013)
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

MOCK_SLACK_URL       = os.environ.get("MOCK_SLACK_URL", "http://localhost:8004")
ONCALL_SCHEDULE_PATH = os.environ.get("ONCALL_SCHEDULE_PATH", "data/oncall_schedule.json")
BUSINESS_HOURS_START = int(os.environ.get("BUSINESS_HOURS_START", "8"))
BUSINESS_HOURS_END   = int(os.environ.get("BUSINESS_HOURS_END", "18"))
BUSINESS_HOURS_TZ    = os.environ.get("BUSINESS_HOURS_TZ", "America/New_York")
REPAGE_INTERVAL_MIN  = int(os.environ.get("REPAGE_INTERVAL_MIN", "30"))
# Set FORCE_AFTER_HOURS=true to always trigger escalation (useful for demos)
FORCE_AFTER_HOURS    = os.environ.get("FORCE_AFTER_HOURS", "false").lower() == "true"

_pool: psycopg2.pool.SimpleConnectionPool | None = None


def get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1, maxconn=5,
            host=DB_HOST, port=DB_PORT,
            dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
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
# Business hours check
# ─────────────────────────────────────────────

def is_outside_business_hours(dt: datetime | None = None) -> bool:
    """
    Returns True if current time is outside business hours.
    Set FORCE_AFTER_HOURS=true to always return True (demo mode).
    Business hours: BUSINESS_HOURS_START–BUSINESS_HOURS_END in BUSINESS_HOURS_TZ.
    """
    if FORCE_AFTER_HOURS:
        return True
    tz = ZoneInfo(BUSINESS_HOURS_TZ)
    now = (dt or datetime.now(timezone.utc)).astimezone(tz)
    # Also treat weekends as after-hours
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return True
    return not (BUSINESS_HOURS_START <= now.hour < BUSINESS_HOURS_END)


# ─────────────────────────────────────────────
# On-call schedule
# ─────────────────────────────────────────────

def get_on_call_agents() -> list[dict]:
    """
    Returns the agents currently on call based on the week number.
    Falls back to first entry if schedule not found.
    """
    path = Path(ONCALL_SCHEDULE_PATH)
    if not path.exists():
        return [{"name": "On-Call Agent", "slack_id": "U000", "email": "oncall@canvasly.io"}]
    with open(path) as f:
        schedule = json.load(f)
    week_num = datetime.now().isocalendar()[1]
    entry = schedule[(week_num - 1) % len(schedule)]
    return entry.get("agents", [])


# ─────────────────────────────────────────────
# Deduplication check
# ─────────────────────────────────────────────

def already_paged(ticket_id: str) -> bool:
    """
    Returns True if this ticket has already been paged within REPAGE_INTERVAL_MIN.
    Prevents duplicate pages for the same incident.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT paged_at FROM escalation_log
                WHERE ticket_id = %s
                  AND paged_at > now() - interval '%s minutes'
                ORDER BY paged_at DESC
                LIMIT 1
                """,
                (ticket_id, REPAGE_INTERVAL_MIN),
            )
            return cur.fetchone() is not None


# ─────────────────────────────────────────────
# Notification
# ─────────────────────────────────────────────

def send_slack_alert(ticket: dict, agents: list[dict]) -> None:
    mentions = " ".join(f"<@{a['slack_id']}>" for a in agents)
    account_name = ticket.get("account_name", "Unknown Account")
    ticket_id    = ticket["ticket_id"]
    category     = ticket.get("category", "")
    seat_count   = ticket.get("account_seat_count", "?")
    arr          = ticket.get("account_arr", "?")

    text = (
        f":rotating_light: *AFTER-HOURS ENTERPRISE ESCALATION* :rotating_light:\n"
        f"{mentions}\n"
        f"*Ticket:* {ticket_id}  |  *Account:* {account_name}  |  "
        f"*Seats:* {seat_count}  |  *ARR:* ${arr:,.0f}\n"
        f"*Issue:* {category}\n"
        f"*Notes:* {ticket.get('internal_notes', '')[:200]}"
    )

    payload = {
        "channel": "#enterprise-oncall",
        "text": text,
        "metadata": {
            "ticket_id": ticket_id,
            "account_name": account_name,
            "priority": ticket.get("priority"),
        },
    }

    try:
        with httpx.Client(timeout=5) as client:
            resp = client.post(f"{MOCK_SLACK_URL}/api/chat.postMessage", json=payload)
            resp.raise_for_status()
            logger.info("Slack alert sent for ticket %s", ticket_id)
    except Exception as exc:
        logger.error("Failed to send Slack alert for %s: %s", ticket_id, exc)
        raise


def log_escalation(ticket_id: str, agents: list[dict]) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            for agent in agents:
                cur.execute(
                    """
                    INSERT INTO escalation_log (ticket_id, on_call_agent, channel, payload)
                    VALUES (%s, %s, 'slack', %s)
                    """,
                    (ticket_id, agent["name"], json.dumps({"agents": agents})),
                )
            cur.execute(
                "UPDATE tickets SET escalation_paged = TRUE, escalation_paged_at = now() WHERE ticket_id = %s",
                (ticket_id,),
            )
    logger.info("Escalation logged for ticket %s", ticket_id)


# ─────────────────────────────────────────────
# Core escalation logic
# ─────────────────────────────────────────────

def process_escalation(ticket: dict) -> dict:
    ticket_id    = ticket["ticket_id"]
    is_ent       = bool(ticket.get("is_enterprise", False))
    priority     = (ticket.get("priority") or "").lower()

    # Use current time — we want to know if the ticket is arriving OOH right now,
    # not whether it was created OOH (seed data has historical daytime timestamps).
    outside_hours = is_outside_business_hours()

    logger.info(
        "Escalation check: ticket=%s enterprise=%s priority=%s outside_hours=%s",
        ticket_id, is_ent, priority, outside_hours,
    )

    # Only page if: enterprise + high priority + outside hours
    if not (is_ent and priority == "high" and outside_hours):
        return {
            "ticket_id": ticket_id,
            "paged": False,
            "reason": f"enterprise={is_ent}, priority={priority}, outside_hours={outside_hours}",
        }

    # Deduplication
    if already_paged(ticket_id):
        return {
            "ticket_id": ticket_id,
            "paged": False,
            "reason": f"already paged within {REPAGE_INTERVAL_MIN} minutes",
        }

    agents = get_on_call_agents()
    send_slack_alert(ticket, agents)
    log_escalation(ticket_id, agents)

    return {
        "ticket_id":  ticket_id,
        "paged":      True,
        "on_call":    [a["name"] for a in agents],
    }


# ─────────────────────────────────────────────
# AWS Lambda entry point
# ─────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    try:
        detail = event.get("detail", event)
        result = process_escalation(detail)
        return {"statusCode": 200, "body": json.dumps(result)}
    except Exception as exc:
        logger.exception("Escalation handler error")
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}


# ─────────────────────────────────────────────
# Local dev FastAPI wrapper
# ─────────────────────────────────────────────

app = FastAPI(title="Escalation Lambda")

@app.post("/invoke")
async def invoke(request: Request):
    body = await request.json()
    detail = body.get("detail", body)
    try:
        result = process_escalation(detail)
        return JSONResponse(result)
    except Exception as exc:
        logger.exception("Escalation error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/health")
def health():
    return {"status": "ok", "service": "escalation"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8013, log_level="info")
