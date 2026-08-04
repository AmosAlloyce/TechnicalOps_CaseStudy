"""
mocks/main.py

Mock external API server for local development.
Runs four FastAPI apps on separate ports:
  8001 — Mock Zendesk
  8002 — Mock Salesforce
  8003 — Mock Canvasly Admin Portal
  8004 — Mock Slack notifications

Seeded with the 30-ticket sample from the Canvasly case study CSV.
"""

import csv
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# ─────────────────────────────────────────────
# Load seed data
# ─────────────────────────────────────────────

SEED_CSV = Path(os.environ.get("SEED_DATA_PATH", "/app/data/canvasly_tickets.csv"))

def load_tickets() -> list[dict]:
    if not SEED_CSV.exists():
        return []
    with open(SEED_CSV) as f:
        return list(csv.DictReader(f))

TICKETS = load_tickets()
TICKET_STATES: dict[str, dict] = {
    ticket["ticket_id"]: {"status": "open", "replies": []}
    for ticket in TICKETS
}

# ─────────────────────────────────────────────
# Account data derived from ticket seed
# Maps ticket accounts to Salesforce-style records
# ─────────────────────────────────────────────

ACCOUNTS = {
    "meridian-health": {
        "account_id": "meridian-health",
        "account_name": "Meridian Health",
        "arr": 125000,
        "tier": "enterprise",
        "seat_count": 450,
        "renewal_date": "2026-09-01",
        "account_owner": "Sarah Chen",
        "health_score": 62,
    },
    "dataforge": {
        "account_id": "dataforge",
        "account_name": "DataForge",
        "arr": 87500,
        "tier": "enterprise",
        "seat_count": 175,
        "renewal_date": "2026-07-15",
        "account_owner": "Mike Torres",
        "health_score": 28,
    },
    "luma-analytics": {
        "account_id": "luma-analytics",
        "account_name": "Luma Analytics",
        "arr": 100000,
        "tier": "enterprise",
        "seat_count": 200,
        "renewal_date": "2026-11-01",
        "account_owner": "Sarah Chen",
        "health_score": 45,
    },
    "corevista": {
        "account_id": "corevista",
        "account_name": "CoreVista",
        "arr": 150000,
        "tier": "enterprise",
        "seat_count": 300,
        "renewal_date": "2026-08-01",
        "account_owner": "James Park",
        "health_score": 55,
    },
    "ridgeline-corp": {
        "account_id": "ridgeline-corp",
        "account_name": "Ridgeline Corp",
        "arr": 125000,
        "tier": "enterprise",
        "seat_count": 250,
        "renewal_date": "2026-10-15",
        "account_owner": "Mike Torres",
        "health_score": 40,
    },
    "novatech": {
        "account_id": "novatech",
        "account_name": "NovaTech",
        "arr": 250000,
        "tier": "enterprise",
        "seat_count": 500,
        "renewal_date": "2027-01-01",
        "account_owner": "Sarah Chen",
        "health_score": 70,
    },
    "general": {
        "account_id": "general",
        "account_name": "General Account",
        "arr": 0,
        "tier": "trial",
        "seat_count": 5,
        "renewal_date": None,
        "account_owner": None,
        "health_score": 80,
    },
}

# Map ticket_ids to account_ids based on notes
TICKET_ACCOUNT_MAP = {
    "T-4804": "meridian-health",
    "T-4824": "meridian-health",
    "T-4810": "luma-analytics",
    "T-4815": "dataforge",
    "T-4828": "dataforge",
    "T-4813": "corevista",
    "T-4823": "novatech",
    "T-4829": "ridgeline-corp",
    "T-4805": "novatech",
    # Demo scenario tickets — mapped so enterprise/churn logic triggers correctly
    "DEMO-001": "general",           # Scenario 1: standard account, magic import retry
    "DEMO-002": "dataforge",         # Scenario 2: enterprise (DataForge, 175 seats, high priority)
    "DEMO-003": "general",           # Scenario 3: standard billing question
    "DEMO-004": "novatech",          # Scenario 4: CSAT anomaly on enterprise account
    "DEMO-005A": "dataforge",        # Scenario 5: churn risk cluster
    "DEMO-005B": "dataforge",
    "DEMO-005C": "dataforge",
}

def get_account_for_ticket(ticket_id: str) -> dict:
    account_id = TICKET_ACCOUNT_MAP.get(ticket_id, "general")
    return ACCOUNTS.get(account_id, ACCOUNTS["general"])


# ─────────────────────────────────────────────
# Notification log (in-memory for mock Slack)
# ─────────────────────────────────────────────
notification_log: list[dict] = []


# ─────────────────────────────────────────────
# Mock Zendesk API — port 8001
# ─────────────────────────────────────────────
zendesk_app = FastAPI(title="Mock Zendesk API")

class ZendeskTicketCreate(BaseModel):
    ticket_id: str
    created_at: str
    channel: str
    category: str
    priority_assigned: str
    agent_name: str | None = None
    csat_score: int | None = None
    agent_internal_notes: str | None = None

@zendesk_app.get("/health")
def zendesk_health():
    return {"status": "ok", "service": "mock-zendesk"}

@zendesk_app.get("/api/v2/tickets")
def list_tickets():
    return {"tickets": TICKETS, "count": len(TICKETS)}

@zendesk_app.get("/api/v2/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    ticket = next((t for t in TICKETS if t["ticket_id"] == ticket_id), None)
    state = TICKET_STATES.get(ticket_id)
    if not ticket and not state:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return {"ticket": ticket, "state": state or {"status": "open", "replies": []}}

@zendesk_app.post("/api/v2/tickets/{ticket_id}/reply")
def reply_to_ticket(ticket_id: str, body: dict):
    """Simulates sending a reply on a ticket (used by auto-resolve flow)."""
    state = TICKET_STATES.setdefault(ticket_id, {"status": "open", "replies": []})
    state["replies"].append({
        "body": body.get("body", ""),
        "public": bool(body.get("public", True)),
        "timestamp": datetime.utcnow().isoformat(),
    })
    return {
        "status": "sent",
        "ticket_id": ticket_id,
        "message": body.get("body", ""),
        "timestamp": datetime.utcnow().isoformat(),
    }


@zendesk_app.put("/api/v2/tickets/{ticket_id}")
def update_ticket(ticket_id: str, body: dict):
    """Simulates the Zendesk status update used to solve a ticket."""
    state = TICKET_STATES.setdefault(ticket_id, {"status": "open", "replies": []})
    if body.get("status"):
        state["status"] = body["status"]
    return {"status": "updated", "ticket_id": ticket_id, "state": state}

@zendesk_app.post("/api/v2/tickets/fire")
def fire_ticket(ticket: ZendeskTicketCreate):
    """Fires a ticket event — used by the seed script and load tester."""
    return {"status": "accepted", "ticket_id": ticket.ticket_id}


# ─────────────────────────────────────────────
# Mock Salesforce API — port 8002
# ─────────────────────────────────────────────
salesforce_app = FastAPI(title="Mock Salesforce API")

@salesforce_app.get("/health")
def sf_health():
    return {"status": "ok", "service": "mock-salesforce"}

@salesforce_app.get("/services/data/v58.0/query")
def sf_query(q: str = ""):
    """Handles SOQL-style queries — returns matching account records."""
    # Simplified: return all accounts
    return {
        "totalSize": len(ACCOUNTS),
        "done": True,
        "records": list(ACCOUNTS.values()),
    }

@salesforce_app.get("/services/data/v58.0/sobjects/Account/{account_id}")
def get_account(account_id: str):
    account = ACCOUNTS.get(account_id)
    if not account:
        # Return general account for unknown IDs
        return ACCOUNTS["general"]
    return account

@salesforce_app.get("/services/data/v58.0/sobjects/Account/byTicket/{ticket_id}")
def get_account_by_ticket(ticket_id: str):
    return get_account_for_ticket(ticket_id)


# ─────────────────────────────────────────────
# Mock Canvasly Admin Portal — port 8003
# ─────────────────────────────────────────────
admin_app = FastAPI(title="Mock Canvasly Admin Portal")

FEATURE_FLAGS = {
    "meridian-health": {"magic_import_enabled": True, "design_curation": True, "sso": True},
    "dataforge": {"magic_import_enabled": True, "design_curation": False, "sso": True},
    "luma-analytics": {"magic_import_enabled": True, "design_curation": True, "sso": False},
    "corevista": {"magic_import_enabled": False, "design_curation": False, "sso": True},
    "ridgeline-corp": {"magic_import_enabled": True, "design_curation": True, "sso": True},
    "novatech": {"magic_import_enabled": True, "design_curation": True, "sso": True},
}

USAGE_METRICS = {
    "meridian-health": {"monthly_active_users": 312, "canvas_count": 847, "last_login_days_ago": 0},
    "dataforge": {"monthly_active_users": 89, "canvas_count": 234, "last_login_days_ago": 2},
    "luma-analytics": {"monthly_active_users": 145, "canvas_count": 391, "last_login_days_ago": 1},
    "corevista": {"monthly_active_users": 0, "canvas_count": 156, "last_login_days_ago": 1},  # locked out
    "ridgeline-corp": {"monthly_active_users": 0, "canvas_count": 412, "last_login_days_ago": 0},  # locked out
    "novatech": {"monthly_active_users": 423, "canvas_count": 1203, "last_login_days_ago": 0},
}

@admin_app.get("/health")
def admin_health():
    return {"status": "ok", "service": "mock-canvasly-admin"}

@admin_app.get("/api/accounts/{account_id}")
def get_admin_account(account_id: str):
    return {
        "account_id": account_id,
        "feature_flags": FEATURE_FLAGS.get(account_id, {}),
        "usage": USAGE_METRICS.get(account_id, {}),
    }

@admin_app.get("/api/accounts/{account_id}/tickets/recent")
def get_recent_tickets(account_id: str, limit: int = 5):
    account_tickets = [
        t for t in TICKETS
        if TICKET_ACCOUNT_MAP.get(t["ticket_id"]) == account_id
    ]
    return {"recent_tickets": account_tickets[-limit:], "count": len(account_tickets)}


# ─────────────────────────────────────────────
# Mock Slack Notifications — port 8004
# ─────────────────────────────────────────────
slack_app = FastAPI(title="Mock Slack Notifications")

class SlackMessage(BaseModel):
    channel: str
    text: str
    attachments: list[dict] | None = None
    metadata: dict | None = None

@slack_app.get("/health")
def slack_health():
    return {"status": "ok", "service": "mock-slack"}

@slack_app.post("/api/chat.postMessage")
def post_message(msg: SlackMessage):
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "channel": msg.channel,
        "text": msg.text,
        "attachments": msg.attachments,
        "metadata": msg.metadata,
    }
    notification_log.append(entry)
    print(f"[MOCK SLACK] #{msg.channel}: {msg.text}")
    return {"ok": True, "ts": entry["ts"]}

@slack_app.get("/api/notifications", response_class=HTMLResponse)
def get_notifications(limit: int = 50):
    entries = notification_log[-limit:]
    total = len(notification_log)

    rows = []
    for e in reversed(entries):
        ts = e.get("ts", "")
        channel = e.get("channel", "")
        text = e.get("text", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        meta = e.get("metadata") or {}
        meta_html = ""
        if meta:
            meta_html = " &nbsp;·&nbsp; ".join(
                f"<span class='meta-key'>{k}</span> <span class='meta-val'>{v}</span>"
                for k, v in meta.items() if v is not None
            )
        channel_class = "ch-oncall" if "oncall" in channel else ("ch-health" if "health" in channel else ("ch-ops" if "ops" in channel else "ch-default"))
        rows.append(f"""
        <tr>
          <td class="ts">{ts[:19].replace("T", " ")}</td>
          <td><span class="channel {channel_class}">{channel}</span></td>
          <td class="msg">{text}{('<div class="meta">' + meta_html + '</div>') if meta_html else ''}</td>
        </tr>""")

    rows_html = "\n".join(rows) if rows else "<tr><td colspan='3' class='empty'>No notifications yet.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="5">
<title>Mock Slack — Notification Log</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Segoe UI", system-ui, sans-serif; font-size: 14px;
         line-height: 1.5; background: #f7f8fa; color: #1f2328; padding: 24px 16px; }}
  .page {{ max-width: 900px; margin: 0 auto; }}
  header {{ display: flex; align-items: baseline; gap: 12px; margin-bottom: 20px; }}
  h1 {{ font-size: 18px; font-weight: 700; }}
  .badge {{ background: #e5e7eb; border-radius: 20px; padding: 2px 10px;
            font-size: 12px; font-weight: 600; color: #57606a; }}
  .hint {{ font-size: 12px; color: #57606a; margin-left: auto; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border: 1px solid #e5e7eb; border-radius: 6px; overflow: hidden; }}
  th {{ background: #f7f8fa; text-align: left; padding: 9px 14px; font-size: 12px;
        font-weight: 600; color: #57606a; border-bottom: 1px solid #e5e7eb; }}
  td {{ padding: 10px 14px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  .ts {{ font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px;
         color: #57606a; white-space: nowrap; }}
  .channel {{ display: inline-block; font-family: "SFMono-Regular", Consolas, monospace;
              font-size: 12px; font-weight: 600; border-radius: 3px; padding: 1px 7px; }}
  .ch-oncall  {{ background: #ffebe9; color: #cf222e; }}
  .ch-health  {{ background: #fff8c5; color: #9a6700; }}
  .ch-ops     {{ background: #dafbe1; color: #1a7f37; }}
  .ch-default {{ background: #ddf4ff; color: #0969da; }}
  .msg {{ max-width: 600px; word-break: break-word; }}
  .meta {{ margin-top: 6px; font-size: 11px; color: #57606a; }}
  .meta-key {{ font-weight: 600; }}
  .meta-val {{ color: #1f2328; }}
  .empty {{ text-align: center; padding: 32px; color: #57606a; font-style: italic; }}
  footer {{ margin-top: 20px; text-align: center; font-size: 11px; color: #57606a;
            border-top: 1px solid #e5e7eb; padding-top: 12px; }}
</style>
</head>
<body>
<div class="page">
  <header>
    <h1>Mock Slack — Notification Log</h1>
    <span class="badge">{total} total</span>
    <span class="hint">auto-refreshes every 5s</span>
  </header>
  <table>
    <thead><tr><th>Time</th><th>Channel</th><th>Message</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <footer>Made with IBM Bob</footer>
</div>
</body>
</html>"""

@slack_app.get("/api/notifications/json")
def get_notifications_json(limit: int = 50):
    return {"notifications": notification_log[-limit:], "total": len(notification_log)}

@slack_app.delete("/api/notifications")
def clear_notifications():
    notification_log.clear()
    return {"cleared": True}


# ─────────────────────────────────────────────
# Run all four services on separate ports
# ─────────────────────────────────────────────

def run_app(app: FastAPI, port: int):
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

if __name__ == "__main__":
    threads = [
        threading.Thread(target=run_app, args=(zendesk_app, 8001), daemon=True),
        threading.Thread(target=run_app, args=(salesforce_app, 8002), daemon=True),
        threading.Thread(target=run_app, args=(admin_app, 8003), daemon=True),
        threading.Thread(target=run_app, args=(slack_app, 8004), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
