# Canvasly TechOps — System Architecture

## Request Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│  INGEST LAYER                                                       │
│                                                                     │
│  Zendesk ──► API Gateway ──► webhook_receiver Lambda                │
│                                   │                                 │
│                                   ▼                                 │
│                           EventBridge Bus                           │
│                          (TicketCreated /                           │
│                           TicketUpdated)                            │
│                      ┌────────────┴────────────┐                   │
│                      ▼                         ▼                   │
│              SQS Dead-Letter               EventBridge             │
│              Queue (failed                  Rules                  │
│              deliveries)                    │                      │
└─────────────────────────────────────────────┼────────────────────-─┘
                                              │
┌─────────────────────────────────────────────▼────────────────────-─┐
│  PROCESSING LAYER                                                   │
│                                                                     │
│  enrichment Lambda ◄── EventBridge rule: TicketCreated             │
│       │  (fetch Salesforce + admin portal, flag is_enterprise)      │
│       ▼                                                             │
│  Aurora Serverless v2 (Postgres-compatible)                         │
│       │                                                             │
│  triage_agent Lambda ◄── EventBridge rule: TicketEnriched          │
│       │  (LangGraph agent → classify, route, draft, escalate)       │
│       │  LLM: GroqClient (default) / OllamaClient (fallback)        │
│       ▼                                                             │
│  ┌─────────────────┬────────────────┬──────────────────┐           │
│  │  AUTO-RESOLVE   │  DRAFT+QUEUE   │  ESCALATE        │           │
│  │  (Magic Import  │  (standard     │  (enterprise +   │           │
│  │   retry)        │   tickets)     │   high + OOH)    │           │
│  └─────────────────┴────────────────┴──────────────────┘           │
│                                              │                      │
│  escalation Lambda ◄─────────────────────────┘                     │
│       │  (check hours, check dedup, fire alert)                     │
│       ▼                                                             │
│  n8n Workflow Layer (Slack alerts, digests, CSAT anomaly)           │
└─────────────────────────────────────────────────────────────────────┘
                                              │
┌─────────────────────────────────────────────▼────────────────────-─┐
│  OBSERVABILITY LAYER                                                │
│                                                                     │
│  Grafana ◄── Postgres (direct datasource)                           │
│  • Queue Health dashboard                                           │
│  • Churn Risk Radar dashboard                                       │
│  • AI Agent Activity dashboard                                      │
│  • After-Hours Alert Log dashboard                                  │
│                                                                     │
│  Streamlit Scale Simulator ◄── volume_projections.md + Postgres     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Scaling Boundaries

### Lambda

| Function | Concurrency Config | Rationale |
|---|---|---|
| `webhook_receiver` | Unreserved | Absorbs any burst; stateless, fast, publishes to EventBridge only |
| `enrichment` | Unreserved | External API calls are the bottleneck, not Lambda concurrency |
| `triage_agent` | Reserved = 10 (dev), unreserved (prod) | Dev: protects Groq free tier (30 req/min); prod: uncapped with paid LLM |
| `escalation` | Unreserved | Low volume by design; only fires on enterprise + high + out-of-hours |

AWS Lambda default account limit: **1,000 concurrent executions** (soft limit, raisable via support ticket).  
At 12,000 tickets/day (~8.3/min avg, ~50/min peak burst): peak Lambda concurrency ~50 — well within defaults.

### EventBridge

- Default throughput: **10,000 events/sec** per bus
- At 12,000 tickets/day = **0.14 events/sec avg**, **~2 events/sec at peak burst**
- EventBridge is **not the bottleneck at any projected Canvasly volume**
- Soft limit increase path: AWS Support → Service Quotas → EventBridge → `PutEvents` transactions/sec

### Aurora Serverless v2

| Volume Tier | Avg ACU | Peak ACU | Est. Monthly DB Cost |
|---|---|---|---|
| 1,200 tickets/day (today) | 0.5 ACU | 1.0 ACU | ~$4/mo |
| 6,000 tickets/day (6 months) | 1.5 ACU | 3.0 ACU | ~$18/mo |
| 12,000 tickets/day (12 months) | 3.0 ACU | 6.0 ACU | ~$35/mo |

- Minimum: **0.5 ACU** (~$0.06/hr when active, $0 when paused after 5 min idle)
- Maximum: **16 ACU** (handles ~10,000 concurrent connections — far beyond any Canvasly scenario)
- ACU increment: 0.5 — scales up in seconds, scales down over minutes
- RDS Proxy sits in front of Aurora in production — pools Lambda connections, prevents exhaustion under concurrency burst

### Groq Free Tier

| Limit | Value | Canvasly headroom |
|---|---|---|
| Requests/minute | 30 | At 1,200/day = 0.83/min avg — fine |
| Requests/day | 14,400 | At 1,200/day = 1,200/day — fine |
| Requests/day (6 months) | 14,400 | At 5,000/day — **exceeds free tier** |

**LLM upgrade trigger:** At ~4,000–5,000 tickets/day, switch `LLM_PROVIDER` from `groq` to `bedrock` or `openai`. This is a single env var change — no code changes required. See [DEPLOYMENT.md](DEPLOYMENT.md) for the upgrade path.

---

## LLM Abstraction Layer

All LLM calls in the system go through `agents/llm_client.py`. The interface is minimal by design:

```python
class BaseLLMClient:
    def complete(self, prompt: str, system: str = "") -> str:
        raise NotImplementedError
```

### Provider Selection

Controlled by the `LLM_PROVIDER` environment variable:

| Value | Provider | Use case |
|---|---|---|
| `groq` (default) | Groq API — Llama 3 8B | Dev, demo, low-volume production |
| `ollama` | Local Ollama — phi3-mini | Offline development, no API key |
| `bedrock` | AWS Bedrock — Claude Haiku | Production at scale (cost-efficient) |
| `openai` | OpenAI — GPT-4o-mini | Production alternative |

### Fallback Behaviour

If `LLM_PROVIDER=groq` but `GROQ_API_KEY` is missing or invalid:
→ System logs a warning and falls back to `OllamaClient` automatically  
→ If Ollama is also unavailable: falls back to rule-based routing  
→ The system **never fails silently** — every fallback is logged and visible in Grafana (AI Agent Activity dashboard)

---

## Database Schema

### `tickets` table

| Column | Type | Index | Notes |
|---|---|---|---|
| `id` | UUID PK | — | Internal ID |
| `ticket_id` | VARCHAR | UNIQUE | e.g. T-4801 |
| `created_at` | TIMESTAMPTZ | ✓ | Partition key for time-range queries |
| `channel` | VARCHAR | — | chat / email / voice |
| `category` | VARCHAR | — | Raw from Zendesk |
| `priority_assigned` | VARCHAR | — | Raw from Zendesk |
| `first_response_min` | INTEGER | — | |
| `resolution_min` | INTEGER | — | |
| `agent_name` | VARCHAR | — | |
| `escalated` | BOOLEAN | — | |
| `csat_score` | INTEGER | — | |
| `agent_internal_notes` | TEXT | — | |
| `account_id` | VARCHAR | ✓ | FK to accounts; used for churn radar queries |
| `is_enterprise` | BOOLEAN | ✓ | Core routing flag |
| `account_arr` | NUMERIC | — | From Salesforce enrichment |
| `account_tier` | VARCHAR | — | enterprise / mid-market / trial |
| `account_seat_count` | INTEGER | — | From admin portal |
| `renewal_date` | DATE | — | From Salesforce |
| `account_owner` | VARCHAR | — | From Salesforce |
| `triage_routing` | VARCHAR | — | Agent decision: auto_resolve / enterprise_queue / standard_queue / escalate |
| `triage_reasoning` | TEXT | — | LLM reasoning logged for auditability |
| `llm_provider_used` | VARCHAR | — | groq / ollama / rules (fallback) |
| `draft_response` | TEXT | — | LLM-generated draft for agent review |
| `escalation_paged` | BOOLEAN | — | |
| `escalation_acked_at` | TIMESTAMPTZ | — | |

### `escalation_log` table

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `ticket_id` | VARCHAR | FK to tickets |
| `paged_at` | TIMESTAMPTZ | |
| `on_call_agent` | VARCHAR | |
| `acked_at` | TIMESTAMPTZ | NULL until acknowledged |
| `channel` | VARCHAR | slack / pagerduty |

---

## Failure Modes & Mitigations

| Failure | Impact | Mitigation |
|---|---|---|
| Groq API down / rate limited | Triage agent falls back to Ollama or rule-based routing | `BaseLLMClient` fallback chain; logged to Grafana |
| EventBridge delivery failure | Ticket not processed | SQS DLQ catches failed deliveries; alerts on DLQ depth |
| enrichment Lambda times out | Ticket stored un-enriched | Enrichment retry on next EventBridge event; `is_enterprise` defaults to False (conservative) |
| Aurora connection exhaustion | DB writes fail | RDS Proxy pools connections; max 5 conns per Lambda instance via psycopg2 pool |
| n8n workflow failure | Notification not sent | n8n built-in retry + error workflow; alerts on failure |
| Ollama not running (offline fallback unavailable) | Full LLM fallback to rules | Rule-based routing covers all ticket types; system continues functioning |

---

## Volume Projections & Cost Model

See [`data/volume_projections.md`](data/volume_projections.md) for full tier-by-tier breakdown.

| Tier | Volume | AWS Cost/mo | LLM | DB |
|---|---|---|---|---|
| Today | 1,200/day | ~$8 | Groq free | 0.5 ACU |
| 6 months | 5,000/day | ~$35 | Groq paid / Bedrock | 1.5 ACU |
| 12 months | 12,000/day | ~$80 | Bedrock / OpenAI | 3.0 ACU |

---

## Key Design Decisions

See [`CASE_STUDY.md`](CASE_STUDY.md) Trade-off Log section for full reasoning behind each decision.
