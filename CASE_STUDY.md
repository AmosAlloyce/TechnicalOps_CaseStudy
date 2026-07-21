# Canvasly Support Operations: Diagnosis & Solution

**Role:** Head of Technical Operations  
**Prepared by:** Portfolio submission — Canvasly TechOps Case Study  
**Accompanying repo:** See `README.md` for architecture, setup, and live demo instructions

---

## Executive Summary

Canvasly is losing enterprise accounts right now — not because the product is broken, but because the operational layer around it has never been built. Three distinct failure modes are compounding into a churn signal: enterprise tickets are invisible to the system, agents waste a third of every handle time switching between tools, and a class of highly automatable errors is being resolved manually hundreds of times per month. The pilot experiment already proved that separating enterprise tickets reduces churn risk — it just couldn't be sustained manually. This document describes what to build, in what order, and why, to make that separation permanent and extend it across every failure mode the data exposes.

---

## The Diagnosis

### How I Read the Data

Before categorising problems, I read every agent note in the 30-ticket sample looking for patterns the aggregate metrics obscure. Three things jumped out immediately.

**First:** The same accounts appear multiple times. Meridian Health is in T-4804 and T-4824 — two Magic Import Quality complaints in 48 hours, the second explicitly threatening to switch to Miro. DataForge is in T-4815 (overnight email, 412-minute first response) and T-4828 (cancellation request, citing "poor support responsiveness and Magic Import quality regression"). These aren't isolated incidents. They are accounts in the process of churning, visible in the ticket data days before the cancellation request arrives — and the current system has no mechanism to surface them.

**Second:** There are two entirely distinct Magic Import problems that the category label "Magic Import" flattens into one. The first is transient processing failures that resolve on retry — T-4801, T-4806, T-4808, T-4814, T-4818, T-4821, T-4826, T-4830. These are operationally trivial and fully automatable. The second is genuine AI output quality failures on complex inputs — T-4811, T-4816, T-4824. These route to a Design Curation queue with a 67–70+ hour actual wait time while agents are promising 2–3 hours. That promise-to-reality gap is an active trust destruction mechanism, not just a support problem.

**Third:** The after-hours failure mode is not a staffing problem in disguise — it is a systems problem. T-4813 and T-4829 are SSO emergencies on enterprise accounts (300 seats and 250 seats respectively) that arrived after 6pm EST. Both sat unresolved overnight or until morning. Devon's note on T-4829 — *"this is the second after-hours SSO emergency this week, we are going to lose enterprise customers over this"* — is a field observation from someone watching the churn happen in real time. Despite the improved daytime metrics from the pilot, two enterprise accounts still churned during the period. The pilot did not cover after hours. That is why.

---

### The Three Highest-Leverage Problems

Ranked by enterprise churn impact, not operational volume:

#### Problem 1: Enterprise tickets have no visibility, no priority, and no after-hours coverage

This is the primary churn driver. The current system routes a 450-seat enterprise SSO emergency identically to a free trial user asking about dark mode. T-4805, T-4813, T-4815, T-4823, T-4829 all document enterprise-tier issues being handled with general-queue response times — or not handled at all until the next business day.

The pilot data makes the counterfactual explicit: when 8 agents were manually dedicated to enterprise tickets, enterprise FRT dropped from a median of 11 minutes to 3 minutes and enterprise CSAT rose from 68% to 88%. The intervention worked. The only reason it stopped was that it required 2.5 hours/day of manual triage — an unsustainable human cost that this system eliminates entirely.

Critically, the pilot did not address after-hours. T-4813 (CoreVista, 300 seats, lost an evening of work) and T-4829 (Ridgeline Corp, 250 seats, locked out during a client presentation) both occurred after 6pm. No routing logic, however good, helps an enterprise customer who cannot access their workspace during a client-facing moment if no one is available to respond.

**Churn exposure:** At 4.2% quarterly enterprise churn on 300 accounts at $50K+ ARR, the current run rate is roughly $630K ARR at risk per quarter. The 38% of cancellations citing support responsiveness means approximately $239K of that is directly attributable to this failure mode.

#### Problem 2: Agent context is fragmented across four systems

Agents cannot begin responding to a ticket until they have assembled context from Zendesk, the Canvasly admin portal, Salesforce, and a Google Sheet. T-4802 documents this explicitly: *"Took me 25 min just to gather the info before I could even start responding."* T-4807, T-4820, and T-4827 each describe the same four-system lookup for billing and policy information.

The case study estimates 5–6 minutes of avoidable AHT per ticket from tool-switching. At 1,200 tickets/day and 45 agents at $3,400/month fully loaded (~$20/hr), the productivity cost is:

**5 min × 1,200 tickets/day × 22 working days × ($20/60 min) = ~$44,000/month in recoverable agent time.**

Even at 50% recovery efficiency, this is the largest single operational cost in the system — larger than the Zendesk license cost for the entire team.

#### Problem 3: Magic Import transient errors are resolved manually at scale

Devon counted 23 retry-resolved import errors in a single week (T-4821). Maria tracked four consecutive mornings of the same pattern (T-4818). The fix in every case is identical: send a message asking the user to retry; the error resolves. There is no diagnostic value in a human performing this action. It is a machine task being done by a person.

At 23 touches/week, 14 minutes AHT, $20/hr:

**23 × 14 min × ($20/60 min) × 4.3 weeks/month = ~$460/month** for a problem that should cost zero agent time.

This number is small relative to Problem 2, but the automation is also trivial — it is the highest-ROI-per-engineering-hour item in the backlog, and shipping it in week 1 creates immediate visible signal that the new system is working.

The Design Curation queue problem (T-4811, T-4816, T-4824) is a different issue entirely and is **not solvable with operational tooling**. The 67–70 hour queue wait time is a product resourcing decision. What this system can do is stop agents from promising 2–3 hour turnarounds when the actual queue is 70 hours — that specific lie is an operational failure, and it can be fixed with accurate queue-status messaging.

---

## The ROI Model

| Initiative | Calculation | Monthly Value |
|---|---|---|
| Enterprise queue separation | Pilot: CSAT 68→88%, FRT 11→3 min. If 4.2% churn → 2.5% churn on enterprise: ~5 accounts retained/quarter × $50K ARR = **$250K ARR/quarter** | Retention, not cost |
| After-hours escalation | Two documented after-hours enterprise emergencies in 48 hours. At $50K+ ARR per account, preventing one churn event pays for the system for years | Retention, not cost |
| Enrichment layer (AHT reduction) | 5 min × 1,200 tickets/day × 22 days × $0.33/min = ~$44K/mo; at 50% recovery | **~$22,000/month** |
| Magic Import auto-retry | 23 touches/week × 14 min × $0.33/min × 4.3 weeks | **~$460/month** |
| Reduced agent attrition | 11% monthly attrition × 45 agents × $3,400 = $16,830/mo replacement cost. Better tooling is a documented retention factor | Partial offset |

The enrichment layer is the largest direct cost saving. The enterprise routing and after-hours escalation are the largest churn-prevention levers. Both need to ship.

---

## The Solution

### Week 1: Stop the Bleeding

The case study is explicit — leadership wants to know what ships this week. Three things can ship in days, not weeks, and together they address the most acute churn signals:

**1. Enterprise ticket separation via Zendesk triggers (hours, not days)**

Before any custom code is deployed, Zendesk native triggers can route tickets from enterprise accounts (identified by email domain or organisation tag) to a dedicated enterprise view. This is exactly what the pilot team lead did manually — it can be automated in Zendesk configuration in under an hour. This is not the permanent solution, but it stops the bleeding immediately while the enrichment and routing Lambda is being built.

**2. After-hours enterprise alert via a simple webhook (day 1–2)**

A single Lambda behind API Gateway, receiving Zendesk ticket create events, can check: is this a High priority ticket? Is the account enterprise? Is it outside 08:00–18:00 EST? If all three: page the on-call rotation. This requires no enrichment pipeline, no AI, and no database — just the Zendesk webhook payload and a Slack message. It can be deployed in a day and would have prevented the CoreVista and Ridgeline Corp incidents.

**3. Magic Import auto-retry macro + trigger (hours)**

A Zendesk trigger that matches on `category = Magic Import - Error` + `first response pending` can fire a macro sending the retry message automatically. This covers the transient error class immediately, without any Lambda or AI infrastructure. The agent-built LangGraph version replaces this later with richer classification, but the Zendesk-native version ships in hours.

These three interventions require no engineering sprint, no AWS deployment, and no external dependencies. They can be in production on day one.

### Week 2–4: Build the Permanent Layer

With the immediate bleeding stopped, the priority shifts to building the infrastructure that makes the week-1 interventions permanent, scalable, and observable:

1. **Ticket enrichment Lambda** — pulls Salesforce + admin portal data on every ticket create event, flags enterprise accounts, eliminates the four-system lookup. This is the foundation everything else builds on.
2. **AI triage agent** — LangGraph state machine that classifies tickets, routes them intelligently, generates draft responses, and handles the Magic Import retry pattern with full context rather than a blunt trigger.
3. **Grafana ops dashboard** — makes queue health, churn signals, and agent activity visible in real time. The current system has no queue monitoring at all.
4. **n8n workflow layer** — replaces the broken Zapier automations and adds the churn risk digest that would have surfaced Meridian Health and DataForge before their cancellation requests.

### What I Explicitly Left Out

**Rebuilding the Google Sheet policy layer.** T-4807 and T-4827 show agents unsure if the policy sheet is current. The right fix is moving policy into a Zendesk knowledge base article with a defined owner and update cadence. This is a process and ownership problem, not a tooling problem. Adding another system does not help.

**Fixing Magic Import quality.** The AI output failures on complex inputs (T-4811, T-4816, T-4824) and the 70-hour Design Curation queue are product problems. They require engineering resourcing and executive sponsorship — the 6–8 week lead time the constraints describe. What this system does is stop agents from lying to customers about queue times, and flag affected enterprise accounts for proactive outreach.

**Building a custom analytics layer.** The 200K-row Google Sheet used for ad-hoc reporting is a real problem, but Grafana connecting directly to the Postgres database solves this for the ops use case without building anything custom.

---

## Trade-off Log

**Groq (cloud API) over a local LLM model**
The initial design used a local Ollama model. Small local models (tinyllama, phi3-mini) produce unreliable structured JSON output — the triage agent's routing decisions would have been wrong often enough to require the rule-based fallback to carry most of the actual work, making the "AI agent" effectively theatre. Groq's free tier provides Llama 3 8B, a model that reliably follows structured output instructions. The trade-off is a cloud dependency; the mitigation is the `BaseLLMClient` abstraction and Ollama as an offline fallback. For a portfolio demo that needs to actually work, correctness beats purity.

**EventBridge over SQS-first**
EventBridge as the event bus means the ingest Lambda publishes once and multiple downstream consumers (enrichment, triage, escalation) react independently via rules — without the ingest Lambda knowing anything about them. SQS would require the ingest Lambda to know which queues to write to, coupling it to the downstream topology. At Canvasly's scale EventBridge is not the bottleneck; the decoupling benefit is immediate.

**Grafana over a custom Streamlit dashboard**
A Streamlit dashboard would have been faster to build and more flexible. It would also look like a prototype to anyone who has used real ops tooling. Grafana is what a TechOps team actually uses to monitor queue health. Pre-provisioned dashboards that load from Postgres on first start means the demo is always meaningful. The trade-off is that Grafana is less interactive than Streamlit for cost modelling — which is why Streamlit is kept for the Scale Simulator specifically.

**AWS SAM over Terraform**
SAM is Python Lambda native, requires no additional toolchain, and `sam local invoke` gives instant local testing without any AWS account. Terraform would be more flexible for a multi-service production deployment but is significantly heavier for a portfolio project where the goal is demonstrability, not comprehensive infrastructure management.

---

## What I Would Do Differently

The enrichment Lambda fetches Salesforce and admin portal data synchronously on every ticket create event. At 1,200 tickets/day this is fine — latency is acceptable and the mock APIs are fast. At 10,000+ tickets/day with real external APIs that have their own rate limits and p99 latencies, this becomes a reliability risk: a slow Salesforce API call holds up the enrichment Lambda, which delays triage, which delays routing. The right design caches account data (ARR, tier, seat count) in a short-TTL DynamoDB table updated on a schedule, and the enrichment Lambda reads from cache rather than making synchronous external calls on the hot path. I chose simplicity for the portfolio build; I would choose resilience for production.

---

*This case study accompanies a working code repository. See `README.md` for architecture details, setup instructions, and demo walkthrough.*
