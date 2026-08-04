# Technical Operations Support Demo

A working Docker Compose demonstration of an automated support-operations
pipeline built from a case-study ticket dataset.

The local system ingests support tickets, enriches them with account context,
routes them using deterministic rules, drafts customer replies, safely
auto-resolves retryable import failures, escalates qualifying enterprise
incidents, and exposes operational dashboards and workflow automations.

Related documentation:

- [`CASE_STUDY.md`](CASE_STUDY.md): diagnosis, proposed operating model, and ROI analysis
- [`ARCHITECTURE.md`](ARCHITECTURE.md): production-oriented system design
- [`DEPLOYMENT.md`](DEPLOYMENT.md): AWS deployment reference and cost model

## Current State

The repository currently provides a complete local demo with these behaviors:

| Capability | Current implementation |
|---|---|
| Ticket ingest | FastAPI webhook receiver on port `8010` |
| Event routing | Local EventBridge-style fan-out service on port `8020` |
| Account enrichment | Mock Salesforce and admin APIs |
| Triage | Credential-free deterministic rules by default |
| Draft responses | Safe category-specific templates stored in Postgres |
| Auto-resolution | Retryable import errors receive a public reply and are marked solved in the mock ticket system |
| Enterprise handling | Enterprise retry cases remain open for human review |
| Escalation | High-priority enterprise tickets are paged only when their arrival timestamp is outside configured business hours |
| Workflow automation | Four n8n workflows are imported and published automatically |
| Dashboards | Four provisioned Grafana dashboards plus a Streamlit scale simulator |
| Seed data | A one-shot service replays all 30 CSV tickets through the pipeline |
| Tests | Seven focused routing and business-hours tests |

No API key is required for the default demo. External LLM providers are
optional.

## Local Architecture

```text
CSV seed or demo script
          |
          v
Webhook receiver :8010
          |
          v
Local event bus :8020
          |
          v
Account enrichment :8011
          |
          +--------------------> Postgres :5432
          |
          v
Rules/LLM triage :8012
          |
          +---- standard or enterprise queue -> stored draft
          |
          +---- retryable import error -> reply + solved status
          |
          +---- urgent enterprise issue -> escalation :8013 -> mock Slack

n8n :5678 provides separate webhook and scheduled workflow demonstrations.
Grafana :3000 reads operational data from Postgres.
Streamlit :8502 provides the volume and cost simulator.
```

The main ticket pipeline uses the local services directly. The n8n CSAT
scenario calls an n8n production webhook, while the other imported workflows
can also be triggered independently for demonstrations.

## Quick Start

### Prerequisites

- Docker Engine
- Docker Compose v2
- Ports `3000`, `5432`, `5678`, `8001-8004`, `8010-8013`, `8020`, and `8502` available

### Configure

From the repository directory:

```bash
cp .env.example .env
```

The default configuration uses:

```text
LLM_PROVIDER=rules
```

This mode is deterministic and requires neither an API key nor an internet
connection after the Docker images are available.

### Start

Run in the background:

```bash
docker compose up -d --build
```

Follow startup activity when needed:

```bash
docker compose logs -f
```

On a fresh build, n8n imports and publishes four workflows sequentially. This
can take around one to two minutes. The seed container then replays 30 tickets
and exits successfully.

Check the final service state:

```bash
docker compose ps
```

The long-running services should be `running` or `healthy`. The `seed` and
`n8n-owner` services are one-shot setup jobs, so an exit code of `0` is
expected.

## Interfaces

| Service | URL | Access |
|---|---|---|
| Grafana | http://localhost:3000 | Anonymous viewer access is enabled |
| n8n | http://localhost:5678 | Owner credentials are printed by the `n8n-owner` logs |
| Scale simulator | http://localhost:8502 | No login |
| Mock Slack log | http://localhost:8004/api/notifications | No login |
| Mock ticket API | http://localhost:8001/api/v2/tickets | No login |
| Event log | http://localhost:8020/events | No login |

To display the n8n owner setup result:

```bash
docker compose logs n8n-owner
```

## Demo Scenarios

Run all five scenarios interactively:

```bash
./simulate_scenarios.sh
```

The script pauses between scenarios so each result can be inspected in the
dashboards and mock interfaces.

Run a single scenario:

```bash
./simulate_scenarios.sh 1   # Retryable import error: reply and solve
./simulate_scenarios.sh 2   # Enterprise after-hours escalation
./simulate_scenarios.sh 3   # Standard queue with a generated draft
./simulate_scenarios.sh 4   # Enterprise low-CSAT alert through n8n
./simulate_scenarios.sh 5   # At-risk enterprise account ticket cluster
```

Scenario 2 evaluates the timestamp in its ticket payload. It does not depend
on the operator's current time. `FORCE_AFTER_HOURS=true` is only needed when
testing a live payload that arrives during configured business hours.

## Verified Routing Rules

The default rules provider intentionally favors predictable, reviewable demo
behavior:

- Transient import processing failures can be auto-resolved for standard accounts.
- Enterprise import retries require human review and are not autonomously closed.
- File-size and other hard failures remain open with corrective instructions.
- High-priority enterprise incidents route to escalation.
- Billing and other standard tickets receive category-specific drafts.
- A ticket is marked auto-resolved in Postgres only after the mock ticket API accepts the reply and solved-status update.

## Tests

Run the focused suite inside the triage image:

```bash
docker compose run --rm --no-deps triage-agent \
  python -m unittest discover -s tests -v
```

The current suite contains seven tests covering:

- transient import auto-resolution;
- file-size failure exclusion;
- enterprise retry review;
- enterprise escalation routing;
- CSV timestamp parsing;
- daytime business-hours behavior;
- after-hours behavior.

## Optional LLM Providers

The rules provider is the supported default for a credential-free demo.

To use Groq, update `.env`:

```text
LLM_PROVIDER=groq
GROQ_API_KEY=your_key
```

To use the local Ollama profile:

```bash
docker compose --profile offline up -d --build
```

Then set:

```text
LLM_PROVIDER=ollama
```

The Ollama profile downloads a model on first use and therefore needs network
access and additional disk space.

## Persistence and Resetting

Postgres, n8n, Grafana, and Ollama use named Docker volumes. A normal stop keeps
their data:

```bash
docker compose down
```

The following command permanently removes the local database, workflow state,
and dashboard state:

```bash
docker compose down -v
```

Use the volume-removal command only when a completely clean demo is required.

The mock ticket and Slack state is held in memory and resets when the mock API
container is recreated.

## Expected Startup Messages

These messages do not prevent the demo from working:

- n8n may time out while checking its external registry when internet access is unavailable.
- n8n may report that the internal Python task runner is unavailable; the included workflows use HTTP, JavaScript, schedule, and database nodes.
- Python may emit timezone-related deprecation warnings from the mock services.
- Docker may report a stopped orphan from an older Compose definition.

The reliable success signals are:

- application health checks return `200`;
- the seed reports `Sent: 30  Errors: 0`;
- n8n reports four activated workflows;
- `docker compose ps` shows the long-running services healthy.

## Project Structure

```text
TechnicalOps_CaseStudy/
|-- CASE_STUDY.md
|-- ARCHITECTURE.md
|-- DEPLOYMENT.md
|-- docker-compose.yml
|-- simulate_scenarios.sh
|-- agents/                  # Triage graph, rules, and provider abstraction
|-- lambdas/                 # Local FastAPI wrappers and Lambda handlers
|-- mocks/                   # Ticketing, CRM, admin, Slack, and event-bus mocks
|-- data/                    # CSV seed, database schema, and projections
|-- n8n/workflows/           # Four imported workflow definitions
|-- grafana/                 # Provisioning and dashboard definitions
|-- dashboard/               # Streamlit scale simulator
|-- infra/                   # AWS SAM reference architecture
`-- tests/                   # Focused demo logic tests
```

## Production Reference

The local demo uses Docker, FastAPI, Postgres, mock integrations, and a local
event bus. The files under `infra/`, along with `ARCHITECTURE.md` and
`DEPLOYMENT.md`, describe a possible AWS deployment using Lambda, API Gateway,
EventBridge, and a managed database. That cloud deployment is separate from
the verified local demo and is not required to run it.
