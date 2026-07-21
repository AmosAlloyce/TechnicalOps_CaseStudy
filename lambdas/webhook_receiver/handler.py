"""
lambdas/webhook_receiver/handler.py

Zendesk webhook receiver.
Parses incoming Zendesk ticket payloads, validates HMAC (mocked in dev),
and publishes a TicketCreated event to EventBridge (or local event bus).

AWS Lambda entry point: handler(event, context)
Local dev entry point:  uvicorn app (FastAPI wrapper on port 8010)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

import boto3
import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
EVENTBRIDGE_MODE     = os.environ.get("EVENTBRIDGE_MODE", "aws")   # aws | local
LOCAL_EVENT_BUS_URL  = os.environ.get("LOCAL_EVENT_BUS_URL", "http://event-bus:8020/events")
EVENTBRIDGE_BUS_NAME = os.environ.get("EVENTBRIDGE_BUS_NAME", "canvasly-tickets")
AWS_REGION           = os.environ.get("AWS_REGION", "us-east-1")
ZENDESK_WEBHOOK_SECRET = os.environ.get("ZENDESK_WEBHOOK_SECRET", "dev-secret-mock")


# ─────────────────────────────────────────────
# HMAC validation
# ─────────────────────────────────────────────

def validate_hmac(body: bytes, signature: str | None) -> bool:
    """
    Validates the Zendesk webhook HMAC-SHA256 signature.
    In dev mode (ZENDESK_WEBHOOK_SECRET=dev-secret-mock), always passes.
    """
    if ZENDESK_WEBHOOK_SECRET == "dev-secret-mock":
        return True
    if not signature:
        return False
    expected = hmac.new(
        ZENDESK_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ─────────────────────────────────────────────
# Event publishing
# ─────────────────────────────────────────────

def publish_event(detail_type: str, detail: dict) -> None:
    """
    Publishes a structured event to EventBridge (prod) or local bus (dev).
    """
    if EVENTBRIDGE_MODE == "local":
        payload = {
            "source": "canvasly.webhook_receiver",
            "detail_type": detail_type,
            "detail": detail,
        }
        try:
            resp = httpx.post(LOCAL_EVENT_BUS_URL, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Published %s to local event bus", detail_type)
        except Exception as exc:
            logger.error("Failed to publish to local event bus: %s", exc)
            raise
    else:
        client = boto3.client("events", region_name=AWS_REGION)
        resp = client.put_events(
            Entries=[
                {
                    "Source": "canvasly.webhook_receiver",
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail),
                    "EventBusName": EVENTBRIDGE_BUS_NAME,
                }
            ]
        )
        if resp.get("FailedEntryCount", 0) > 0:
            raise RuntimeError(f"EventBridge PutEvents failed: {resp['Entries']}")
        logger.info("Published %s to EventBridge bus %s", detail_type, EVENTBRIDGE_BUS_NAME)


# ─────────────────────────────────────────────
# Ticket payload parsing
# ─────────────────────────────────────────────

def parse_zendesk_payload(payload: dict) -> dict:
    """
    Normalises a Zendesk webhook payload into the canonical ticket schema
    used by all downstream services.
    """
    ticket = payload.get("ticket", payload)  # handle both wrapped and flat

    return {
        "ticket_id":          str(ticket.get("id", "")),
        "created_at":         ticket.get("created_at", datetime.now(timezone.utc).isoformat()),
        "channel":            ticket.get("channel", "unknown"),
        "category":           ticket.get("category") or ticket.get("subject", ""),
        "priority":           ticket.get("priority", ""),
        "agent_name":         (ticket.get("agent") or {}).get("name", ""),
        "csat_score":         ticket.get("csat_score"),
        "escalated":          bool(ticket.get("escalated", False)),
        "internal_notes":     ticket.get("internal_notes") or ticket.get("comment", {}).get("body", ""),
        "first_response_min": ticket.get("first_response_min"),
        "resolution_min":     ticket.get("resolution_min"),
        "raw":                ticket,
    }


# ─────────────────────────────────────────────
# Core handler logic (shared by Lambda + FastAPI)
# ─────────────────────────────────────────────

def process_webhook(body: bytes, signature: str | None = None) -> dict:
    if not validate_hmac(body, signature):
        raise PermissionError("Invalid HMAC signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON payload: {exc}") from exc

    ticket = parse_zendesk_payload(payload)

    if not ticket["ticket_id"]:
        raise ValueError("Payload missing ticket id")

    publish_event("TicketCreated", ticket)

    return {
        "status": "accepted",
        "ticket_id": ticket["ticket_id"],
        "detail_type": "TicketCreated",
    }


# ─────────────────────────────────────────────
# AWS Lambda entry point
# ─────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    """AWS Lambda handler — triggered by API Gateway."""
    try:
        body = event.get("body", "{}")
        if isinstance(body, str):
            body = body.encode()
        elif isinstance(body, dict):
            body = json.dumps(body).encode()

        signature = (event.get("headers") or {}).get("x-zendesk-webhook-signature")
        result = process_webhook(body, signature)
        return {
            "statusCode": 200,
            "body": json.dumps(result),
        }
    except PermissionError as exc:
        logger.warning("HMAC validation failed: %s", exc)
        return {"statusCode": 401, "body": json.dumps({"error": str(exc)})}
    except ValueError as exc:
        logger.error("Invalid payload: %s", exc)
        return {"statusCode": 400, "body": json.dumps({"error": str(exc)})}
    except Exception as exc:
        logger.exception("Unexpected error in webhook_receiver")
        return {"statusCode": 500, "body": json.dumps({"error": "internal server error"})}


# ─────────────────────────────────────────────
# Local dev FastAPI wrapper (docker compose)
# ─────────────────────────────────────────────

app = FastAPI(title="Webhook Receiver")

@app.post("/webhook/ticket")
async def webhook_endpoint(
    request: Request,
    x_zendesk_webhook_signature: str | None = Header(default=None),
):
    body = await request.body()
    try:
        result = process_webhook(body, x_zendesk_webhook_signature)
        return JSONResponse(content=result, status_code=200)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/health")
def health():
    return {"status": "ok", "service": "webhook-receiver"}


@app.post("/invoke")
async def invoke(request: Request):
    """EventBridge-style local invoke endpoint."""
    return await webhook_endpoint(request)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8010, log_level="info")
