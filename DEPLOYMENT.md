# Canvasly TechOps — Deployment Guide

## Local Development (Docker Compose)

### Prerequisites
- Docker + Docker Compose
- A free [Groq API key](https://console.groq.com)

### Setup

```bash
# 1. Copy environment file and add your Groq key
cp .env.example .env
# Edit .env: set GROQ_API_KEY=gsk_...

# 2. Start the full stack
docker compose up --build

# 3. Verify services
curl http://localhost:8001/health   # mock Zendesk
curl http://localhost:3000/api/health  # Grafana
```

### Offline Mode (no internet/Groq key)

```bash
# Downloads phi3-mini (~2.3GB) and uses it as the LLM
docker compose --profile offline up --build

# In .env: LLM_PROVIDER=ollama
```

### Service URLs

| Service | URL | Credentials |
|---|---|---|
| Grafana | http://localhost:3000 | admin / canvasly_dev |
| n8n | http://localhost:5678 | admin / canvasly_dev |
| Scale Simulator | http://localhost:8502 | — |
| Mock Slack log | http://localhost:8004/api/notifications | — |

---

## AWS Deployment (sam deploy)

### Prerequisites

```bash
pip install aws-sam-cli
aws configure  # set your AWS credentials
```

### SSM Parameters (set before deploying)

```bash
aws ssm put-parameter --name /canvasly/db/user         --value canvasly       --type SecureString
aws ssm put-parameter --name /canvasly/db/password      --value YOUR_DB_PASS   --type SecureString
aws ssm put-parameter --name /canvasly/llm/provider     --value groq           --type String
aws ssm put-parameter --name /canvasly/llm/groq_api_key --value gsk_...        --type SecureString
aws ssm put-parameter --name /canvasly/salesforce/api_url  --value https://... --type String
aws ssm put-parameter --name /canvasly/admin/api_url       --value https://... --type String
aws ssm put-parameter --name /canvasly/zendesk/api_url     --value https://... --type String
aws ssm put-parameter --name /canvasly/slack/webhook_url   --value https://... --type String
aws ssm put-parameter --name /canvasly/vpc/private_subnet_ids --value subnet-xxx,subnet-yyy --type String
```

### Build and Deploy

```bash
sam build

# First deploy (interactive setup)
sam deploy --guided --parameter-overrides Environment=prod

# Subsequent deploys
sam deploy
```

### What Gets Deployed

- 4 Lambda functions (webhook_receiver, enrichment, triage_agent, escalation)
- API Gateway (POST /webhook/ticket)
- EventBridge custom bus + rules + DLQ
- Aurora Serverless v2 cluster (0.5–16 ACU)
- RDS Proxy (connection pooling for Lambda)

### Outputs

After deploy, `sam deploy` prints:
```
WebhookEndpoint: https://abc123.execute-api.us-east-1.amazonaws.com/prod/webhook/ticket
```

Configure this URL as the Zendesk webhook endpoint.

---

## Cost Estimates

| Volume | AWS infra | LLM | Total/month |
|---|---|---|---|
| 1,200 tickets/day (today) | ~$5 | $0 (Groq free) | **~$5** |
| 5,000 tickets/day (6 months) | ~$20 | ~$3 (Groq paid) | **~$23** |
| 12,000 tickets/day (12 months) | ~$37 | ~$7 (Bedrock) | **~$44** |

For reference: Zendesk license alone is $4,005/month (45 agents × $89).

---

## LLM Upgrade Path

At ~4,500 tickets/day the Groq free tier (14,400 req/day) is exceeded.

### Option 1: Groq paid (cheapest)

```bash
# No code change needed — same API
# .env or SSM: LLM_PROVIDER stays as groq
# Groq paid tier activates automatically when free quota is exceeded
```

### Option 2: AWS Bedrock (recommended for enterprise data residency)

```bash
# 1. Add Bedrock permission to triage_agent Lambda role in infra/template.yaml:
#    - bedrock:InvokeModel on arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku*

# 2. Update SSM parameter:
aws ssm put-parameter --name /canvasly/llm/provider --value bedrock --overwrite

# 3. Redeploy:
sam deploy
```

**That's it. No code changes required.** The `BaseLLMClient` abstraction in `agents/llm_client.py` handles the swap.

### Option 3: OpenAI

```bash
aws ssm put-parameter --name /canvasly/llm/provider     --value openai  --overwrite
aws ssm put-parameter --name /canvasly/llm/openai_key   --value sk-...  --type SecureString
sam deploy
```

---

## Scaling Notes

- **Lambda concurrency:** Default account limit 1,000 concurrent. At projected Canvasly volumes (peak ~83/min at T3) no limit increases are needed.
- **Aurora Serverless v2:** Scales 0.5–16 ACU in seconds. Costs ~$0 when idle (pauses after 5 min). Increase max ACU if needed via console — no redeployment required.
- **EventBridge:** 10,000 events/sec default — not the bottleneck at any projected volume.
- **triage_agent reserved concurrency:** Set to 10 in dev to protect Groq free tier. Remove the `ReservedConcurrentExecutions` limit in prod when using a paid LLM.

---

## Teardown

```bash
sam delete --stack-name canvasly-techops
```

Note: Aurora deletion protection is enabled in prod. Disable it first:
```bash
aws rds modify-db-cluster --db-cluster-identifier <id> --no-deletion-protection
```
