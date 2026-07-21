"""
mocks/event_bus/main.py

Local EventBridge simulator for Docker Compose development.
Receives events from the webhook_receiver Lambda and fans them out
to enrichment, triage_agent, and escalation services.

In production this is replaced by AWS EventBridge with rules defined
in infra/template.yaml.
"""

import logging
import os
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Local Event Bus (EventBridge simulator)")

ENRICHMENT_URL   = os.environ.get("ENRICHMENT_URL",   "http://enrichment:8011/invoke")
TRIAGE_URL       = os.environ.get("TRIAGE_URL",       "http://triage-agent:8012/invoke")
ESCALATION_URL   = os.environ.get("ESCALATION_URL",   "http://escalation:8013/invoke")

event_log: list[dict] = []


class Event(BaseModel):
    source: str
    detail_type: str  # TicketCreated | TicketEnriched | TicketTriaged
    detail: dict[str, Any]


async def forward(url: str, payload: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info("Forwarded %s to %s → %s", payload.get("detail_type"), url, resp.status_code)
    except Exception as exc:
        logger.error("Failed to forward event to %s: %s", url, exc)


@app.post("/events")
async def receive_event(event: Event, background_tasks: BackgroundTasks):
    """
    Receives an event and fans it out to downstream services based on detail_type.
    Mirrors AWS EventBridge rule routing logic.
    """
    payload = event.model_dump()
    event_log.append(payload)

    if event.detail_type == "TicketCreated":
        background_tasks.add_task(forward, ENRICHMENT_URL, payload)

    elif event.detail_type == "TicketEnriched":
        background_tasks.add_task(forward, TRIAGE_URL, payload)

    elif event.detail_type == "TicketTriaged":
        if event.detail.get("routing") == "escalate":
            background_tasks.add_task(forward, ESCALATION_URL, payload)

    return {"status": "accepted", "detail_type": event.detail_type}


@app.get("/events")
def get_event_log(limit: int = 100):
    return {"events": event_log[-limit:], "total": len(event_log)}


@app.get("/health")
def health():
    return {"status": "ok", "service": "local-event-bus"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8020, log_level="info")
