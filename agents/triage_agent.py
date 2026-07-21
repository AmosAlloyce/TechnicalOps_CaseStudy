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


def rule_based_routing(state: TicketState) -> TicketState:
    """
    Deterministic fallback routing. Used when LLM output fails validation.
    Always logs that rules were used so it's visible in Grafana.
    """
    category  = (state.get("category") or "").lower()
    priority  = (state.get("priority") or "").lower()
    notes     = (state.get("internal_notes") or "").lower()
    is_ent    = bool(state.get("is_enterprise", False))

    # Detect Magic Import retry pattern
    is_retry = (
        "magic import - error" in category
        and any(kw in notes for kw in MAGIC_IMPORT_RETRY_KEYWORDS)
    )

    if is_retry:
        routing = "auto_resolve"
        reason  = "Magic Import transient error — retry pattern detected by rules"
        draft   = RETRY_RESPONSE_TEMPLATE
    elif is_ent and priority == "high":
        routing = "escalate"
        reason  = "Enterprise account + high priority — escalate"
        draft   = f"This is a high-priority issue for {state.get('account_name', 'an enterprise account')}. Escalating immediately."
    elif is_ent:
        routing = "enterprise_queue"
        reason  = "Enterprise account — route to enterprise priority queue"
        draft   = None
    else:
        routing = "standard_queue"
        reason  = "Standard ticket — route to general queue"
        draft   = None

    return {
        **state,
        "is_retry_pattern":  is_retry,
        "routing":           routing,
        "routing_reason":    reason,
        "draft_response":    draft,
        "llm_provider_used": "rules",
        "fallback_used":     True,
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
            return {
                **state,
                "issue_class":       result.get("issue_class", "other"),
                "severity":          result.get("severity", "low"),
                "is_retry_pattern":  bool(result.get("is_retry_pattern", False)),
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
    graph.add_node("draft_response",  make_draft_node(llm))

    graph.set_entry_point("classify_issue")
    graph.add_edge("classify_issue", "decide_routing")
    graph.add_edge("decide_routing", "draft_response")
    graph.add_edge("draft_response", END)

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
                    state.get("routing") == "auto_resolve",
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


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def triage(ticket: dict, llm: BaseLLMClient | None = None) -> dict:
    """
    Main entry point. Runs the LangGraph triage pipeline on an enriched ticket.
    Returns the final state dict with routing decision and draft response.
    """
    if llm is None:
        llm = get_llm_client()

    graph = build_graph(llm)

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

    final_state = graph.invoke(initial_state)
    persist_decision(final_state)
    return final_state
