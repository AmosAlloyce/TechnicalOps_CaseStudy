"""
data/seed.py

Seeds the local database by replaying the 30-ticket sample CSV
through the full pipeline via the webhook_receiver endpoint.

Run automatically by the `seed` Docker Compose service on first start.
Can also be run manually: python data/seed.py
"""

import csv
import os
import time
from pathlib import Path

import httpx

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://localhost:8010/webhook/ticket")
SEED_CSV    = Path(os.environ.get("SEED_DATA_PATH", "data/canvasly_tickets.csv"))
DELAY_SEC   = float(os.environ.get("SEED_DELAY_SEC", "0.5"))


def wait_for_service(url: str, retries: int = 20, delay: float = 3.0) -> None:
    # Derive health endpoint from base URL (strip path entirely)
    from urllib.parse import urlparse
    parsed = urlparse(url)
    health_url = f"{parsed.scheme}://{parsed.netloc}/health"
    for i in range(retries):
        try:
            resp = httpx.get(health_url, timeout=5)
            if resp.status_code == 200:
                print(f"[seed] Service ready: {health_url}")
                return
        except Exception:
            pass
        print(f"[seed] Waiting for service ({i+1}/{retries})...")
        time.sleep(delay)
    raise RuntimeError(f"Service not ready after {retries} retries: {health_url}")


def seed() -> None:
    print(f"[seed] Loading tickets from {SEED_CSV}")
    if not SEED_CSV.exists():
        raise FileNotFoundError(f"Seed CSV not found: {SEED_CSV}")

    with open(SEED_CSV) as f:
        tickets = list(csv.DictReader(f))

    print(f"[seed] Found {len(tickets)} tickets. Waiting for webhook receiver...")
    wait_for_service(WEBHOOK_URL)

    sent = 0
    errors = 0
    for ticket in tickets:
        payload = {
            "ticket": {
                "id": ticket["ticket_id"],
                "created_at": ticket["created_at"],
                "channel": ticket["channel"],
                "category": ticket["category"],
                "priority": ticket["priority_assigned"],
                "agent": {"name": ticket.get("agent_name", "")},
                "csat_score": int(ticket["csat_score"]) if ticket.get("csat_score") else None,
                "internal_notes": ticket.get("agent_internal_notes", ""),
                "escalated": ticket.get("escalated", "No").lower() == "yes",
                "first_response_min": int(ticket["first_response_min"]) if ticket.get("first_response_min") else None,
                "resolution_min": int(ticket["resolution_min"]) if ticket.get("resolution_min") else None,
            }
        }
        try:
            resp = httpx.post(WEBHOOK_URL, json=payload, timeout=30)
            resp.raise_for_status()
            print(f"[seed] ✓ {ticket['ticket_id']} ({ticket['category']})")
            sent += 1
        except Exception as exc:
            print(f"[seed] ✗ {ticket['ticket_id']}: {exc}")
            errors += 1

        time.sleep(DELAY_SEC)

    print(f"\n[seed] Done. Sent: {sent}  Errors: {errors}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    seed()
