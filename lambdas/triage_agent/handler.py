"""
lambdas/triage_agent/handler.py

Triage agent Lambda wrapper.
Triggered by TicketEnriched events from EventBridge.
Invokes the LangGraph triage agent, persists decisions, publishes TicketTriaged event.

Reserved concurrency = 10 in dev (protects Groq free tier rate limits: 30 req/min).
Remove reserved concurrency in production with a paid/hosted LLM provider.
"""

from __future__ import annotations

import json
import logging
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agents.triage_agent import mark_auto_resolved, triage
from agents.events import publish_event

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MOCK_ZENDESK_URL = os.environ.get("MOCK_ZENDESK_URL", "http://localhost:8001")


def auto_resolve_ticket(ticket_id: str, draft_response: str) -> None:
    """Send the reply and close the ticket only after both API calls succeed."""
    import httpx
    with httpx.Client(timeout=10) as client:
        reply = client.post(
            f"{MOCK_ZENDESK_URL}/api/v2/tickets/{ticket_id}/reply",
            json={"body": draft_response, "public": True},
        )
        reply.raise_for_status()
        status = client.put(
            f"{MOCK_ZENDESK_URL}/api/v2/tickets/{ticket_id}",
            json={"status": "solved"},
        )
        status.raise_for_status()
    mark_auto_resolved(ticket_id)
    logger.info("Auto-resolved ticket %s", ticket_id)


def process_triage(ticket: dict) -> dict:
    result = triage(ticket)
    ticket_id = result["ticket_id"]
    routing   = result.get("routing", "standard_queue")

    # Auto-resolve: send reply immediately, no agent needed
    if routing == "auto_resolve" and result.get("draft_response"):
        auto_resolve_ticket(ticket_id, result["draft_response"])

    # Publish TicketTriaged for escalation Lambda to consume
    publish_event("TicketTriaged", {
        **ticket,
        "routing":            routing,
        "is_enterprise":      result.get("is_enterprise", False),
        "priority":           result.get("priority", ""),
        "account_name":       result.get("account_name"),
        "account_seat_count": result.get("account_seat_count"),
        "account_arr":        result.get("account_arr"),
        "internal_notes":     result.get("internal_notes"),
    })

    return result


def handler(event: dict, context) -> dict:
    """AWS Lambda handler — triggered by EventBridge TicketEnriched rule."""
    try:
        detail = event.get("detail", event)
        result = process_triage(detail)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "ticket_id": result["ticket_id"],
                "routing":   result.get("routing"),
                "fallback":  result.get("fallback_used", False),
            }),
        }
    except Exception as exc:
        logger.exception("Triage agent handler error")
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}


# ─────────────────────────────────────────────
# Local dev FastAPI wrapper
# ─────────────────────────────────────────────

app = FastAPI(title="Triage Agent Lambda")

@app.post("/invoke")
async def invoke(request: Request):
    body = await request.json()
    detail = body.get("detail", body)
    try:
        result = process_triage(detail)
        return JSONResponse({
            "ticket_id": result["ticket_id"],
            "routing":   result.get("routing"),
            "fallback":  result.get("fallback_used", False),
        })
    except Exception as exc:
        logger.exception("Triage error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/health")
def health():
    return {"status": "ok", "service": "triage-agent"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8012, log_level="info")
