# Canvasly TechOps

> **Head of Technical Operations portfolio project.**
> A serverless AI-powered support operations platform built in response to the Canvasly case study — automating triage, enterprise routing, churn detection, and after-hours escalation.

**→ Start with [`CASE_STUDY.md`](CASE_STUDY.md)** — the written diagnosis, solution design, and ROI analysis.  
**→ See [`ARCHITECTURE.md`](ARCHITECTURE.md)** — system design, scaling model, and component decisions.  
**→ See [`DEPLOYMENT.md`](DEPLOYMENT.md)** — AWS deploy guide and cost model.

---

## What This Solves

Three problems identified directly from the ticket data cause Canvasly's enterprise churn:

| Problem | Evidence | Solution |
|---|---|---|
| Enterprise tickets invisible — no priority, no after-hours coverage | T-4813, T-4815, T-4829: enterprise accounts waiting hours overnight | Enrichment Lambda flags enterprise tickets; after-hours escalation Lambda pages on-call |
| Agents check 4 systems before responding | T-4802: *"25 min just to gather info before I could start responding"* | Enrichment Lambda pre-fetches Salesforce + admin portal data on every ticket |
| Magic Import retry errors resolved manually 23+ times/week | T-4821: *"23rd retry-resolved import error this week"* | AI triage agent detects pattern, auto-replies, auto-closes — zero agent touch |

---

## Architecture

```
Zendesk ──► API Gateway ──► webhook_receiver Lambda
                                   │
                            EventBridge Bus
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
           enrichment Lambda              (SQS DLQ on failure)
           (Salesforce + admin portal)
                    │
                Aurora Serverless v2
                    │
           triage_agent Lambda
           (LangGraph + Groq/Ollama)
                    │
        ┌───────────┼────────────┐
        ▼           ▼            ▼
   auto-resolve  draft+queue  escalation Lambda
   (Magic Import  (standard    (after-hours
    retries)       tickets)     enterprise)
                                │
                         n8n Workflows
                         (alerts, digests,
                          CSAT anomaly)
                                │
                         Grafana Dashboard
                         (queue health, churn radar,
                          agent activity, alert log)
```

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- A free [Groq API key](https://console.groq.com) (takes 2 minutes)

### 1. Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/canvasly-techops.git
cd canvasly-techops

# Create your .env from the example
cp .env.example .env

# Add your Groq API key to .env
# Edit .env and set: GROQ_API_KEY=gsk_your_key_here

# For demo mode: force escalation to always fire regardless of time of day
# FORCE_AFTER_HOURS=true
```

### 2. Start the stack

```bash
docker compose up --build
```

This starts:
- Postgres (`:5432`)
- Mock APIs — Zendesk (`:8001`), Salesforce (`:8002`), Admin Portal (`:8003`), Slack (`:8004`)
- Lambda services — webhook receiver (`:8010`), enrichment (`:8011`), triage agent (`:8012`), escalation (`:8013`)
- Local EventBridge (`:8020`)
- n8n (`:5678`)
- Grafana (`:3000`)
- Streamlit Scale Simulator (`:8502`)

On first start, the seed service automatically replays all 30 tickets through the full pipeline. The `n8n-import` service then imports all 4 workflows into n8n automatically.

### 3. Open the dashboards

| Service | URL | Credentials |
|---|---|---|
| **Grafana** | http://localhost:3000 | admin / canvasly_dev |
| **n8n** | http://localhost:5678 | admin / canvasly_dev |
| **Scale Simulator** | http://localhost:8502 | — |
| **Mock Slack log** | http://localhost:8004/api/notifications | — |
| **Event log** | http://localhost:8020/events | — |

### 4. Simulate specific scenarios (for demos / recording)

```bash
chmod +x simulate_scenarios.sh

# Run all 5 scenarios interactively (with pauses between each)
./simulate_scenarios.sh

# Run a single scenario
./simulate_scenarios.sh 1   # Magic Import auto-resolve
./simulate_scenarios.sh 2   # Enterprise after-hours escalation
./simulate_scenarios.sh 3   # Standard queue + AI draft response
./simulate_scenarios.sh 4   # CSAT anomaly alert (via n8n)
./simulate_scenarios.sh 5   # Churn risk account cluster (DataForge)
```

> **Scenario 2 tip:** Set `FORCE_AFTER_HOURS=true` in your `.env` so escalation fires regardless of the current time of day.

### 5. Offline mode (no Groq key / no internet)

```bash
# Start with Ollama profile to pull phi3-mini (~2.3GB)
docker compose --profile offline up --build

# Set LLM_PROVIDER in .env
LLM_PROVIDER=ollama
```

---

## Project Structure

```
canvasly-techops/
├── CASE_STUDY.md             # ← Primary submission document
├── ARCHITECTURE.md           # System design & scaling model
├── DEPLOYMENT.md             # AWS deploy guide
├── docker-compose.yml        # Full local stack
├── .env.example              # Environment variable reference
│
├── lambdas/
│   ├── webhook_receiver/     # Zendesk webhook ingest → EventBridge
│   ├── enrichment/           # Salesforce + admin portal enrichment
│   ├── triage_agent/         # LangGraph AI triage agent
│   └── escalation/           # After-hours on-call paging
│
├── agents/
│   ├── llm_client.py         # Swappable LLM abstraction
│   └── triage_agent.py       # LangGraph state machine
│
├── n8n/
│   └── workflows/            # Exported workflow JSON files
│
├── grafana/
│   ├── provisioning/         # Auto-provisioned datasources + dashboards
│   └── dashboards/           # Dashboard JSON
│
├── dashboard/
│   └── scale_sim.py          # Streamlit cost/volume simulator
│
├── mocks/
│   ├── main.py               # Mock Zendesk, Salesforce, admin, Slack APIs
│   └── event_bus/            # Local EventBridge simulator
│
├── data/
│   ├── canvasly_tickets.csv  # 30-ticket sample (seed data)
│   ├── init.sql              # DB schema
│   ├── seed.py               # Seed script
│   ├── oncall_schedule.json  # On-call rotation config
│   └── volume_projections.md # Cost model at 3 volume tiers
│
├── infra/
│   └── template.yaml         # AWS SAM — Lambda + EventBridge + Aurora Serverless v2
│
└── tests/                    # Unit + integration tests
```

---

## Environment Variables

See [`.env.example`](.env.example) for the full reference. Key variables:

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `groq` | LLM backend: `groq` \| `ollama` \| `bedrock` \| `openai` |
| `GROQ_API_KEY` | — | Required for `groq` provider. Get free at console.groq.com |
| `OLLAMA_MODEL` | `phi3-mini` | Model for offline fallback |
| `ENTERPRISE_SEAT_THRESHOLD` | `100` | Seat count above which account is flagged enterprise |
| `ENTERPRISE_ARR_THRESHOLD` | `50000` | ARR ($) above which account is flagged enterprise |
| `BUSINESS_HOURS_START` | `8` | After-hours escalation window start (24h EST) |
| `BUSINESS_HOURS_END` | `18` | After-hours escalation window end (24h EST) |

---

## Scaling

At ~4,500 tickets/day, switch `LLM_PROVIDER` from `groq` to `bedrock`:
```bash
# .env
LLM_PROVIDER=bedrock
```
No code changes. See [`DEPLOYMENT.md`](DEPLOYMENT.md) for full upgrade path and cost model.

---

## Deploy to AWS

```bash
pip install aws-sam-cli
sam build
sam deploy --guided
```

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for prerequisites, cost estimates, and teardown.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Ingest | AWS Lambda (Python 3.12) + API Gateway + EventBridge |
| AI agents | LangGraph + Groq (Llama 3 8B) / Ollama (phi3-mini) |
| Database | Aurora Serverless v2 (prod) / Postgres (dev) |
| Workflows | n8n (self-hosted) |
| Ops dashboard | Grafana |
| Scale modelling | Streamlit |
| IaC | AWS SAM |
| Mocks | FastAPI |
