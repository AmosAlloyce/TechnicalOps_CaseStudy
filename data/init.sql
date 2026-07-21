-- Canvasly TechOps — Database initialisation
-- Runs once on first postgres container start

CREATE SCHEMA IF NOT EXISTS n8n;

-- ─────────────────────────────────────────────
-- tickets table
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tickets (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id           VARCHAR(20) UNIQUE NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    channel             VARCHAR(20),
    category            VARCHAR(100),
    priority_assigned   VARCHAR(20),
    first_response_min  INTEGER,
    resolution_min      INTEGER,
    agent_name          VARCHAR(100),
    escalated           BOOLEAN DEFAULT FALSE,
    csat_score          INTEGER,
    agent_internal_notes TEXT,

    -- Enriched fields (populated by enrichment Lambda)
    account_id          VARCHAR(100),
    account_name        VARCHAR(200),
    is_enterprise       BOOLEAN DEFAULT FALSE,
    account_arr         NUMERIC(12, 2),
    account_tier        VARCHAR(20),      -- enterprise | mid-market | trial
    account_seat_count  INTEGER,
    renewal_date        DATE,
    account_owner       VARCHAR(100),

    -- Triage agent fields (populated by triage_agent Lambda)
    triage_routing      VARCHAR(30),      -- auto_resolve | enterprise_queue | standard_queue | escalate
    triage_reasoning    TEXT,
    llm_provider_used   VARCHAR(20),      -- groq | ollama | rules
    draft_response      TEXT,
    auto_resolved       BOOLEAN DEFAULT FALSE,

    -- Escalation fields (populated by escalation Lambda)
    escalation_paged    BOOLEAN DEFAULT FALSE,
    escalation_paged_at TIMESTAMPTZ,
    escalation_acked_at TIMESTAMPTZ,

    ingested_at         TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

-- Indexes for dashboard query performance
CREATE INDEX IF NOT EXISTS idx_tickets_account_id    ON tickets (account_id);
CREATE INDEX IF NOT EXISTS idx_tickets_is_enterprise ON tickets (is_enterprise);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at    ON tickets (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_category      ON tickets (category);
CREATE INDEX IF NOT EXISTS idx_tickets_triage_routing ON tickets (triage_routing);

-- ─────────────────────────────────────────────
-- escalation_log table
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escalation_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       VARCHAR(20) REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    paged_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    on_call_agent   VARCHAR(100),
    acked_at        TIMESTAMPTZ,
    channel         VARCHAR(20) DEFAULT 'slack',   -- slack | pagerduty
    payload         JSONB
);

CREATE INDEX IF NOT EXISTS idx_escalation_ticket_id ON escalation_log (ticket_id);
CREATE INDEX IF NOT EXISTS idx_escalation_paged_at  ON escalation_log (paged_at DESC);

-- ─────────────────────────────────────────────
-- agent_decisions table (audit log for LLM decisions)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       VARCHAR(20) REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    node_name       VARCHAR(50),          -- classify_issue | decide_routing | draft_response | etc.
    llm_provider    VARCHAR(20),
    prompt_tokens   INTEGER,
    response_raw    TEXT,
    parsed_output   JSONB,
    fallback_used   BOOLEAN DEFAULT FALSE,
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_ticket_id  ON agent_decisions (ticket_id);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_decided_at ON agent_decisions (decided_at DESC);
