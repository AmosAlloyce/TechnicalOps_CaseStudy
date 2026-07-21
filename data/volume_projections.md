# Canvasly TechOps — Volume Projections & Cost Model

## Ticket Volume Assumptions

| Tier | Scenario | Tickets/Day | Tickets/Month |
|---|---|---|---|
| T1 | Today (300 enterprise accounts) | 1,200 | 26,400 |
| T2 | 6 months (800 enterprise accounts) | 5,000 | 110,000 |
| T3 | 12 months (1,500+ enterprise accounts) | 12,000 | 264,000 |

Growth driver: Canvasly plans to onboard 500 new enterprise accounts in the next 6 months at 100–500 seats each. Current ratio of ~4 tickets/day per account is held constant.

---

## AWS Cost Model

### Lambda

AWS Lambda free tier: 1M requests/month + 400,000 GB-seconds/month.  
Assumption: 4 Lambdas fire per ticket (receiver, enrichment, triage, occasional escalation).

| Tier | Lambda invocations/month | Est. cost |
|---|---|---|
| T1 | ~105,600 | **$0** (within free tier) |
| T2 | ~440,000 | **$0** (within free tier) |
| T3 | ~1,056,000 | **~$0.21** |

Lambda is effectively free at all projected Canvasly volumes.

### API Gateway

$3.50 per million API calls.

| Tier | Calls/month | Est. cost |
|---|---|---|
| T1 | ~26,400 | **~$0.09** |
| T2 | ~110,000 | **~$0.39** |
| T3 | ~264,000 | **~$0.92** |

### Aurora Serverless v2

$0.12/ACU-hour. Pauses after 5 min idle ($0 when paused).

| Tier | Avg ACU | Peak ACU | Est. cost/month |
|---|---|---|---|
| T1 | 0.5 | 1.0 | **~$4** |
| T2 | 1.5 | 3.0 | **~$18** |
| T3 | 3.0 | 6.0 | **~$35** |

### EventBridge

$1.00 per million events.

| Tier | Events/month | Est. cost |
|---|---|---|
| T1 | ~52,800 | **~$0.05** |
| T2 | ~220,000 | **~$0.22** |
| T3 | ~528,000 | **~$0.53** |

### S3 (Lambda artifacts)

~$0.50/month at all tiers (negligible).

---

## LLM Cost Model

### Groq (free tier)

| Limit | Value |
|---|---|
| Requests/minute | 30 |
| Requests/day | 14,400 |
| Cost | $0 |

| Tier | Req/day needed | Within free tier? |
|---|---|---|
| T1 | 1,200 | ✅ Yes |
| T2 | 5,000 | ❌ No — **upgrade trigger** |
| T3 | 12,000 | ❌ No |

### Groq paid tier (post-upgrade)

~$0.05 per 1M input tokens, ~$0.08 per 1M output tokens.  
Avg prompt: ~500 tokens. Avg response: ~200 tokens.

| Tier | LLM cost/month |
|---|---|
| T2 | **~$2.75** |
| T3 | **~$6.60** |

### AWS Bedrock — Claude Haiku (production alternative)

$0.00025 per 1K input tokens, $0.00125 per 1K output tokens.

| Tier | LLM cost/month |
|---|---|
| T2 | **~$3.85** |
| T3 | **~$9.24** |

**LLM upgrade trigger:** At ~4,500 tickets/day, switch `LLM_PROVIDER` from `groq` to `bedrock` or `groq` paid. Single env var change, no code changes.

---

## Total Monthly Cost Summary

| Tier | AWS infra | LLM | **Total** |
|---|---|---|---|
| T1 (1,200/day) | ~$5 | $0 (Groq free) | **~$5/mo** |
| T2 (5,000/day) | ~$20 | ~$3 (Groq paid) | **~$23/mo** |
| T3 (12,000/day) | ~$37 | ~$7 (Bedrock) | **~$44/mo** |

For context: the current Zendesk license cost alone is **$4,005/month** (45 agents × $89). This entire stack at T3 volume costs ~1% of the Zendesk license.

---

## Concurrency Analysis

### Peak burst scenario

Assumption: 10x average rate for 5-minute burst (e.g. large enterprise onboarding event).

| Tier | Avg tickets/min | Peak burst (10x, 5 min) | Peak Lambda concurrency |
|---|---|---|---|
| T1 | 0.83 | 8.3/min | ~8 concurrent |
| T2 | 3.47 | 34.7/min | ~35 concurrent |
| T3 | 8.33 | 83.3/min | ~83 concurrent |

AWS Lambda default account limit: 1,000 concurrent. **No limit increases required at any projected Canvasly volume.**

### Groq rate limit at peak burst

T1 peak: 8.3 req/min → within 30 req/min limit ✅  
T2 peak: 34.7 req/min → **exceeds 30 req/min limit** — upgrade trigger confirmed at T2

---

## LLM Breakeven Analysis

At T2+ volume, Groq paid ($2.75/mo) and Bedrock ($3.85/mo) are both negligible.  
The decision between them at scale is latency (Groq ~200ms, Bedrock ~400ms) and data residency requirements (Bedrock keeps data in AWS VPC; Groq sends to external API).

**Recommendation:** Switch to Bedrock at T2+ if data residency is a customer requirement (likely for enterprise healthcare/finance accounts). Otherwise Groq paid is cheaper and faster.
