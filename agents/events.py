"""
agents/events.py

Shared event publishing utility.
Used by enrichment, triage_agent, and escalation Lambdas to publish
events to EventBridge (production) or the local event bus (dev).

Extracted here to avoid circular imports between Lambda handlers.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

EVENTBRIDGE_MODE     = os.environ.get("EVENTBRIDGE_MODE", "aws")
LOCAL_EVENT_BUS_URL  = os.environ.get("LOCAL_EVENT_BUS_URL", "http://event-bus:8020/events")
EVENTBRIDGE_BUS_NAME = os.environ.get("EVENTBRIDGE_BUS_NAME", "canvasly-tickets")
AWS_REGION           = os.environ.get("AWS_REGION", "us-east-1")


def publish_event(detail_type: str, detail: dict) -> None:
    """
    Publishes a structured event to EventBridge (prod) or local bus (dev).
    Source is derived from the detail_type for routing clarity.
    """
    source_map = {
        "TicketCreated":  "canvasly.webhook_receiver",
        "TicketEnriched": "canvasly.enrichment",
        "TicketTriaged":  "canvasly.triage_agent",
    }
    source = source_map.get(detail_type, "canvasly.system")

    if EVENTBRIDGE_MODE == "local":
        payload = {
            "source": source,
            "detail_type": detail_type,
            "detail": detail,
        }
        try:
            resp = httpx.post(LOCAL_EVENT_BUS_URL, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Published %s to local event bus", detail_type)
        except Exception as exc:
            logger.error("Failed to publish %s to local event bus: %s", detail_type, exc)
            raise
    else:
        import boto3  # noqa: PLC0415
        client = boto3.client("events", region_name=AWS_REGION)
        resp = client.put_events(
            Entries=[
                {
                    "Source": source,
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail),
                    "EventBusName": EVENTBRIDGE_BUS_NAME,
                }
            ]
        )
        if resp.get("FailedEntryCount", 0) > 0:
            raise RuntimeError(f"EventBridge PutEvents failed: {resp['Entries']}")
        logger.info("Published %s to EventBridge bus %s", detail_type, EVENTBRIDGE_BUS_NAME)
