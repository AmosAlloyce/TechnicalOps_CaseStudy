"""
agents/triage_agent.py

LangGraph AI triage agent for Canvasly support tickets.
Receives an enriched ticket and produces:
  - A routing decision: auto_resolve | enterprise_queue | standard_queue | escalate
  - A draft response for the agent (or auto-sent for auto_resolve)
  - An escalation flag for the escalation Lambda

LLM: BaseLLMClient (Groq by default, Ollama offline fallback).
Fallback: rule-based routing if LLM output fails JSON validation.

State machine nodes:
  classify_issue → assess_enterprise_risk → decide_routing → draft_response → done
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Literal, TypedDict

import psycopg2
import psycopg2.pool
from langgraph.graph import END, StateGraph

from agents.llm_client import BaseLLMClient, get_llm_client

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────
# DB pool
# ─────────────────────────────────────────────
DB_HOST     = os.environ.get("DB_HOST", "localhost")
DB_PORT     = int(os.environ.get("DB_PORT", "5432"))
DB_NAME     = os.environ.get("DB_NAME", "canvasly")
DB_USER     = os.environ.get("DB_USER", "canvasly")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "canvasly_dev")

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
# Agent state schema
# ─────────────────────────────────────────────

RoutingDecision = Literal["auto_resolve", "enterprise_queue", "standard_queue", "escalate"]

class TicketState(TypedDict):
    # Input (enriched ticket)
    ticket_id:          str
    created_at:         str
    channel:            str
    category:           str
    priority:           str
    agent_name:         str
    internal_notes:     str
    csat_score:         int | None
    escalated:          bool
    is_enterprise:      bool
    account_id:         str | None
    account_name:       str | None
    account_arr:        float | None
    account_tier:       str | None
    account_seat_count: int | None
    renewal_date:       str | None
    account_owner:      str | None

    # Agent decisions (accumulated across nodes)
    issue_class:        str | None        # classified issue type
    severity:           str | None        # low | medium | high | critical
    is_retry_pattern:   bool | None       # Magic Import transient error
    routing:            RoutingDecision | None
    routing_reason:     str | None
    draft_response:     str | None
    llm_provider_used:  str | None
    fallback_used:      bool


# ─────────────────────────────────────────────
# Rule-based routing (fallback when LLM fails)
# ─────────────────────────────────────────────

MAGIC_IMPORT_RETRY_KEYWORDS = [
    "transient", "retry", "processing failed", "timeout",
    "failed again", "retry resolved", "try again", "reload",
]

MAGIC_IMPORT_HARD_FAILURE_KEYWORDS = [
    "file size", "50mb", "size limit", "too large", "oversized",
]

RETRY_RESPONSE_TEMPLATE = (
    "Hi there,\n\n"
    "Thanks for reaching out about the import issue. "
    "We've identified this as a transient processing error that typically resolves on retry.\n\n"
    "Please try importing your file again. "
    "If you're uploading an image or PDF, ensure the file is under 50MB.\n\n"
    "If the issue persists after retrying, please reply to this message and "
    "we'll escalate to our engineering team.\n\n"
    "Apologies for the inconvenience.\n\nThe Canvasly Support Team"
)

FILE_SIZE_RESPONSE_TEMPLATE = (
    "Hi there,\n\n"
    "The import failed because the file is larger than Canvasly's 50MB limit. "
    "Please reduce the file size and try again. If you need help preparing the "
    "file, reply here and our team will help.\n\n"
    "The Canvasly Support Team"
)


def classify_from_rules(state: TicketState) -> tuple[str, str, bool]:
    """Return a conservative issue class, severity, and retry signal."""
    category = (state.get("category") or "").lower()
    priority = (state.get("priority") or "").lower()
    notes = (state.get("internal_notes") or "").lower()

    if "magic import" in category and "quality" in category:
        issue_class = "magic_import_quality"
    elif "magic import" in category and "error" in category:
        issue_class = "magic_import_error"
    elif "billing" in category:
        issue_class = "billing"
    elif "access" in category or "onboarding" in category:
        issue_class = "account_access"
    elif "performance" in category:
        issue_class = "canvas_performance"
    elif "feature" in category:
        issue_class = "feature_request"
    else:
        issue_class = "other"

    is_hard_failure = any(keyword in notes for keyword in MAGIC_IMPORT_HARD_FAILURE_KEYWORDS)
    is_retry = (
        issue_class == "magic_import_error"
        and not is_hard_failure
        and any(keyword in notes for keyword in MAGIC_IMPORT_RETRY_KEYWORDS)
    )

    if priority == "high" or "cancellation" in category or "downgrade" in category:
        severity = "high"
    elif priority == "medium":
        severity = "medium"
    else:
        severity = "low"
    return issue_class, severity, is_retry


def draft_from_rules(state: TicketState, routing: str, is_retry: bool) -> str | None:
    """Produce safe, deterministic demo drafts without inventing policy."""
    category = (state.get("category") or "").lower()
    account_name = state.get("account_name") or "your team"
    if is_retry:
        return RETRY_RESPONSE_TEMPLATE
    if "magic import" in category and "error" in category:
        return FILE_SIZE_RESPONSE_TEMPLATE
    if routing == "escalate":
        return (
            f"Hi,\n\nWe've received this urgent issue for {account_name} and escalated it "
            "to our on-call team. We will provide an update within 30 minutes.\n\n"
            "The Canvasly Support Team"
        )
    if "billing" in category:
        return (
            "Hi,\n\nThanks for reaching out. We've received your billing question and "
            "are checking the account details now. We'll follow up with a clear answer.\n\n"
            "The Canvasly Support Team"
        )
    if "access" in category or "onboarding" in category:
        return (
            "Hi,\n\nWe've received your access request and are reviewing the workspace "
            "configuration. We'll update you as soon as we have the next step.\n\n"
            "The Canvasly Support Team"
        )
    if routing == "enterprise_queue":
        return (
            f"Hi,\n\nWe've prioritised this request for {account_name} and assigned it to "
            "our enterprise support queue. We'll keep you updated.\n\n"
            "The Canvasly Support Team"
        )
    return None


def rule_based_routing(state: TicketState, *, fallback_used: bool = True) -> TicketState:
    """
    Deterministic fallback routing. Used when LLM output fails validation.
    Always logs that rules were used so it's visible in Grafana.
    """
    priority = (state.get("priority") or "").lower()
    is_ent = bool(state.get("is_enterprise", False))
    issue_class, severity, is_retry = classify_from_rules(state)

    # Do not autonomously close enterprise tickets, even when the likely fix is a retry.
    if is_retry and not is_ent:
        routing = "auto_resolve"
        reason  = "Magic Import transient error — retry pattern detected by rules"
    elif is_retry and is_ent:
        routing = "enterprise_queue"
        reason  = "Enterprise Magic Import retry pattern — draft response, human review required"
    elif is_ent and priority == "high":
        routing = "escalate"
        reason  = "Enterprise account + high priority — escalate"
    elif is_ent:
        routing = "enterprise_queue"
        reason  = "Enterprise account — route to enterprise priority queue"
    else:
        routing = "standard_queue"
        reason  = "Standard ticket — route to general queue"

    if is_retry and not is_ent:
        draft = RETRY_RESPONSE_TEMPLATE
    elif issue_class == "magic_import_error" and not is_retry:
        draft = FILE_SIZE_RESPONSE_TEMPLATE
    else:
        draft = draft_from_rules(state, routing, is_retry)

    return {
        **state,
        "issue_class":     issue_class,
        "severity":        severity,
        "is_retry_pattern": is_retry,
        "routing":         routing,
        "routing_reason":  reason,
        "draft_response":  draft,
        "llm_provider_used": "rules",
        "fallback_used":   fallback_used,
    }


# ─────────────────────────────────────────────
# Prompt templates (compact for Groq/small models)
# ─────────────────────────────────────────────

CLASSIFY_PROMPT = """\
You are a support ticket classifier for Canvasly, a B2B SaaS collaborative design tool.

Ticket details:
- ID: {ticket_id}
- Category: {category}
- Priority: {priority}
- Channel: {channel}
- Notes: {notes}

Classify this ticket. Return ONLY valid JSON:
{{
  "issue_class": "magic_import_error|magic_import_quality|billing|account_access|canvas_performance|feature_request|other",
  "severity": "low|medium|high|critical",
  "is_retry_pattern": true|false
}}

is_retry_pattern = true only if: category contains "Magic Import - Error" AND notes mention transient failure, retry, timeout, or processing failed.
"""

ROUTING_PROMPT = """\
You are a support routing agent for Canvasly.

Ticket:
- ID: {ticket_id}
- Issue class: {issue_class}
- Severity: {severity}
- Is retry pattern: {is_retry_pattern}
- Enterprise account: {is_enterprise}
- Account: {account_name} ({seat_count} seats, ${arr} ARR)
- Priority: {priority}
- Created at: {created_at}

Return ONLY valid JSON:
{{
  "routing": "auto_resolve|enterprise_queue|standard_queue|escalate",
  "reason": "one sentence"
}}

Rules:
- auto_resolve: is_retry_pattern=true only
- escalate: enterprise=true AND (severity=high OR severity=critical)
- enterprise_queue: enterprise=true AND NOT escalate
- standard_queue: all others
"""

DRAFT_PROMPT = """\
You are a Canvasly support agent writing a ticket response.

Ticket:
- Category: {category}
- Issue class: {issue_class}
- Routing: {routing}
- Account: {account_name}
- Notes: {notes}

Write a professional, empathetic response. Be concise (under 100 words). 
Do NOT make up features or policies. If you do not know the resolution, say you are investigating.
Return ONLY the response text, no JSON wrapper.
"""


# ─────────────────────────────────────────────
# LangGraph nodes
# ─────────────────────────────────────────────

def make_classify_node(llm: BaseLLMClient):
    def classify_issue(state: TicketState) -> TicketState:
        prompt = CLASSIFY_PROMPT.format(
            ticket_id=state["ticket_id"],
            category=state.get("category", ""),
            priority=state.get("priority", ""),
            channel=state.get("channel", ""),
            notes=(state.get("internal_notes") or "")[:400],
        )
        try:
            result = llm.complete_json(prompt)
            issue_class = result.get("issue_class", "other")
            is_retry = bool(result.get("is_retry_pattern", False))
            notes = (state.get("internal_notes") or "").lower()
            if any(keyword in notes for keyword in MAGIC_IMPORT_HARD_FAILURE_KEYWORDS):
                is_retry = False
            return {
                **state,
                "issue_class":       issue_class,
                "severity":          result.get("severity", "low"),
                "is_retry_pattern":  is_retry,
                "llm_provider_used": type(llm).__name__.lower().replace("client", ""),
                "fallback_used":     False,
            }
        except (ValueError, Exception) as exc:
            logger.warning("classify_issue LLM failed for %s, using rules: %s", state["ticket_id"], exc)
            return rule_based_routing(state)
    return classify_issue


def make_routing_node(llm: BaseLLMClient):
    def decide_routing(state: TicketState) -> TicketState:
        # If fallback already kicked in, skip
        if state.get("fallback_used"):
            return state

        prompt = ROUTING_PROMPT.format(
            ticket_id=state["ticket_id"],
            issue_class=state.get("issue_class", "other"),
            severity=state.get("severity", "low"),
            is_retry_pattern=state.get("is_retry_pattern", False),
            is_enterprise=state.get("is_enterprise", False),
            account_name=state.get("account_name", "Unknown"),
            seat_count=state.get("account_seat_count", 0),
            arr=state.get("account_arr", 0),
            priority=state.get("priority", ""),
            created_at=state.get("created_at", ""),
        )
        try:
            result = llm.complete_json(prompt)
            routing = result.get("routing", "standard_queue")
            if routing not in ("auto_resolve", "enterprise_queue", "standard_queue", "escalate"):
                raise ValueError(f"Invalid routing value: {routing}")
            if state.get("is_enterprise") and routing == "auto_resolve":
                routing = "enterprise_queue"
            return {
                **state,
                "routing":        routing,
                "routing_reason": result.get("reason", ""),
            }
        except (ValueError, Exception) as exc:
            logger.warning("decide_routing LLM failed for %s, using rules: %s", state["ticket_id"], exc)
            return rule_based_routing(state)
    return decide_routing


def make_draft_node(llm: BaseLLMClient):
    def draft_response(state: TicketState) -> TicketState:
        # Auto-resolve uses template, not LLM draft
        if state.get("routing") == "auto_resolve":
            return {**state, "draft_response": RETRY_RESPONSE_TEMPLATE}

        # Escalations get a brief holding note
        if state.get("routing") == "escalate":
            return {
                **state,
                "draft_response": (
                    f"Hi,\n\nWe've received your urgent report and have escalated this "
                    f"to our on-call team immediately. You will hear back within 30 minutes.\n\n"
                    f"The Canvasly Support Team"
                ),
            }

        if state.get("fallback_used"):
            return state

        prompt = DRAFT_PROMPT.format(
            category=state.get("category", ""),
            issue_class=state.get("issue_class", ""),
            routing=state.get("routing", ""),
            account_name=state.get("account_name", ""),
            notes=(state.get("internal_notes") or "")[:300],
        )
        try:
            draft = llm.complete(prompt)
            return {**state, "draft_response": draft.strip()}
        except Exception as exc:
            logger.warning("draft_response LLM failed for %s: %s", state["ticket_id"], exc)
            return {**state, "draft_response": None}
    return draft_response


# ─────────────────────────────────────────────
# Graph builder
# ─────────────────────────────────────────────

def build_graph(llm: BaseLLMClient) -> Any:
    graph = StateGraph(TicketState)

    graph.add_node("classify_issue",  make_classify_node(llm))
    graph.add_node("decide_routing",  make_routing_node(llm))
    graph.add_node("write_draft",     make_draft_node(llm))

    graph.set_entry_point("classify_issue")
    graph.add_edge("classify_issue", "decide_routing")
    graph.add_edge("decide_routing", "write_draft")
    graph.add_edge("write_draft", END)

    return graph.compile()


# ─────────────────────────────────────────────
# DB persistence
# ─────────────────────────────────────────────

def persist_decision(state: TicketState) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tickets SET
                    triage_routing     = %s,
                    triage_reasoning   = %s,
                    llm_provider_used  = %s,
                    draft_response     = %s,
                    auto_resolved      = %s,
                    updated_at         = now()
                WHERE ticket_id = %s
                """,
                (
                    state.get("routing"),
                    state.get("routing_reason"),
                    state.get("llm_provider_used"),
                    state.get("draft_response"),
                    False,
                    state["ticket_id"],
                ),
            )
            cur.execute(
                """
                INSERT INTO agent_decisions
                    (ticket_id, node_name, llm_provider, fallback_used, parsed_output)
                VALUES (%s, 'triage_complete', %s, %s, %s)
                """,
                (
                    state["ticket_id"],
                    state.get("llm_provider_used"),
                    state.get("fallback_used", False),
                    json.dumps({
                        "routing":       state.get("routing"),
                        "severity":      state.get("severity"),
                        "issue_class":   state.get("issue_class"),
                        "is_retry":      state.get("is_retry_pattern"),
                    }),
                ),
            )
    logger.info(
        "Persisted triage for %s: routing=%s llm=%s fallback=%s",
        state["ticket_id"], state.get("routing"),
        state.get("llm_provider_used"), state.get("fallback_used"),
    )


def mark_auto_resolved(ticket_id: str) -> None:
    """Mark a ticket solved only after the external reply/status update succeeds."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET auto_resolved = TRUE, updated_at = now() WHERE ticket_id = %s",
                (ticket_id,),
            )


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def triage(ticket: dict, llm: BaseLLMClient | None = None) -> dict:
    """
    Main entry point. Runs the LangGraph triage pipeline on an enriched ticket.
    Returns the final state dict with routing decision and draft response.
    """
    initial_state: TicketState = {
        "ticket_id":          ticket.get("ticket_id", ""),
        "created_at":         ticket.get("created_at", ""),
        "channel":            ticket.get("channel", ""),
        "category":           ticket.get("category", ""),
        "priority":           ticket.get("priority", ""),
        "agent_name":         ticket.get("agent_name", ""),
        "internal_notes":     ticket.get("internal_notes", ""),
        "csat_score":         ticket.get("csat_score"),
        "escalated":          bool(ticket.get("escalated", False)),
        "is_enterprise":      bool(ticket.get("is_enterprise", False)),
        "account_id":         ticket.get("account_id"),
        "account_name":       ticket.get("account_name"),
        "account_arr":        ticket.get("account_arr"),
        "account_tier":       ticket.get("account_tier"),
        "account_seat_count": ticket.get("account_seat_count"),
        "renewal_date":       ticket.get("renewal_date"),
        "account_owner":      ticket.get("account_owner"),
        # Agent fields initialised to None
        "issue_class":        None,
        "severity":           None,
        "is_retry_pattern":   None,
        "routing":            None,
        "routing_reason":     None,
        "draft_response":     None,
        "llm_provider_used":  None,
        "fallback_used":      False,
    }

    if os.environ.get("LLM_PROVIDER", "rules").lower() == "rules":
        final_state = rule_based_routing(initial_state, fallback_used=False)
        persist_decision(final_state)
        return final_state

    if llm is None:
        llm = get_llm_client()

    graph = build_graph(llm)

    final_state = graph.invoke(initial_state)
    persist_decision(final_state)
    return final_state
