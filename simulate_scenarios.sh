#!/bin/bash
# =============================================================================
# simulate_scenarios.sh
# Canvasly TechOps — Demo Scenario Simulator
#
# Fires individual test tickets to demonstrate each pipeline scenario.
# Run AFTER the full stack is up: docker compose up --build
#
# Usage:
#   chmod +x simulate_scenarios.sh
#   ./simulate_scenarios.sh            # runs all scenarios with pauses
#   ./simulate_scenarios.sh 1          # run only scenario 1
#
# Scenarios:
#   1 — Magic Import auto-resolve  (AI detects retry pattern, closes ticket, no agent)
#   2 — Enterprise escalation      (high priority, enterprise account, pages on-call)
#   3 — Standard queue routing     (normal ticket, draft queued for agent)
#   4 — CSAT anomaly alert         (low CSAT on enterprise ticket)
#   5 — Churn risk account         (multiple tickets from at-risk enterprise account)
# =============================================================================

# Note: intentionally NOT using set -e or -u — the JSON payloads contain $ signs
# that would be misread as shell variables under -u (e.g. $87,500 ARR).
set -o pipefail

WEBHOOK_URL="${WEBHOOK_URL:-http://localhost:8010/webhook/ticket}"
SLACK_LOG="${SLACK_LOG:-http://localhost:8004/api/notifications}"
EVENT_LOG="${EVENT_LOG:-http://localhost:8020/events}"

# Colours
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

pause() {
    echo ""
    echo -e "${YELLOW}── Press ENTER to continue to next scenario ──${NC}"
    read -r
}

header() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  SCENARIO $1: $2${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

fire() {
    local ticket_id="$1"
    local payload="$2"
    echo -e "${GREEN}→ Firing ticket ${ticket_id}...${NC}"
    response=$(curl -s -X POST "${WEBHOOK_URL}" \
        -H "Content-Type: application/json" \
        -d "${payload}")
    echo "  Response: ${response}"
    echo ""
}

check_slack() {
    echo -e "${YELLOW}📬 Slack notification log:${NC}"
    echo "  → Open in browser: ${SLACK_LOG}"
    result=$(curl -sf "${SLACK_LOG}/json?limit=3" 2>/dev/null) || true
    if [ -n "$result" ]; then
        echo "$result" | grep -o '"channel":"[^"]*"\|"ts":"[^"]*"' | sed 's/"//g; s/:/: /' | head -20
    else
        echo "  (no notifications yet or service not reachable)"
    fi
    echo ""
}

check_events() {
    echo -e "${YELLOW}📡 Recent event bus activity:${NC}"
    curl -s "${EVENT_LOG}?limit=5" | python3 -m json.tool 2>/dev/null | \
        grep -E '"detail_type"|"ticket_id"|"routing"' | head -20 || true
    echo ""
}

wait_for_pipeline() {
    echo -e "${YELLOW}⏳ Waiting 4s for pipeline to process...${NC}"
    sleep 4
}

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1: Magic Import Auto-Resolve
# Trigger: category contains "Magic Import - Error" + transient notes
# Expected: AI routes to auto_resolve → reply sent to Zendesk → no agent needed
# ─────────────────────────────────────────────────────────────────────────────
scenario_1() {
    header "1" "Magic Import Auto-Resolve"
    echo "Ticket: Magic Import processing failed error"
    echo "Expected: AI detects retry pattern → auto-resolves → sends reply to customer → Slack #support-ops logs it"
    echo ""

    fire "DEMO-001" '{
        "ticket": {
            "id": "DEMO-001",
            "created_at": "2026-01-15T02:14:00Z",
            "channel": "email",
            "category": "Magic Import - Error",
            "priority": "low",
            "agent": {"name": "Maria S."},
            "internal_notes": "Customer tried to upload a PNG whiteboard. Got processing failed error. Classic transient timeout — had them retry.",
            "csat_score": null,
            "escalated": false
        }
    }'

    wait_for_pipeline

    echo -e "${GREEN}✓ Check results:${NC}"
    echo "  → Grafana: http://localhost:3000  (AI Agent Activity dashboard)"
    echo "  → Mock Slack: http://localhost:8004/api/notifications"
    echo "  → Event log: http://localhost:8020/events"
    check_slack
}

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2: Enterprise After-Hours Escalation
# Trigger: enterprise account + high priority + FORCE_AFTER_HOURS=true
# Expected: on-call paged via Slack #enterprise-oncall
# ─────────────────────────────────────────────────────────────────────────────
scenario_2() {
    header "2" "Enterprise After-Hours Escalation"
    echo "Ticket: Enterprise account (DataForge, 175 seats) — SSO failure, high priority"
    echo "Expected: Enriched as enterprise → triage routes to escalate → on-call paged in Slack"
    echo ""
    echo -e "${YELLOW}NOTE: Make sure FORCE_AFTER_HOURS=true is set in your .env${NC}"
    echo "      (or run this scenario outside business hours Mon-Fri 8am-6pm EST)"
    echo ""

    fire "DEMO-002" '{
        "ticket": {
            "id": "DEMO-002",
            "created_at": "2026-01-15T02:30:00Z",
            "channel": "email",
            "category": "Account - Access",
            "priority": "high",
            "agent": {"name": "James K."},
            "internal_notes": "80 users at DataForge cannot access workspace. SSO config broke after their IT team updated SAML settings. Enterprise account. No access since 11pm.",
            "csat_score": null,
            "escalated": false
        }
    }'

    wait_for_pipeline

    echo -e "${GREEN}✓ Check results:${NC}"
    echo "  → Slack #enterprise-oncall: http://localhost:8004/api/notifications"
    echo "  → Grafana After-Hours Alerts: http://localhost:3000"
    check_slack
}

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3: Standard Queue Routing with Draft Response
# Trigger: non-enterprise, medium priority billing question
# Expected: AI classifies → routes to standard_queue → drafts response for agent
# ─────────────────────────────────────────────────────────────────────────────
scenario_3() {
    header "3" "Standard Queue — AI Draft Response"
    echo "Ticket: Billing question from a standard account"
    echo "Expected: AI classifies as billing → routes to standard_queue → drafts response"
    echo ""

    fire "DEMO-003" '{
        "ticket": {
            "id": "DEMO-003",
            "created_at": "2026-01-15T10:00:00Z",
            "channel": "chat",
            "category": "Billing - Invoice Question",
            "priority": "medium",
            "agent": {"name": "Devon R."},
            "internal_notes": "Customer wants to know why their invoice is higher than expected this month. Says they did not add any seats.",
            "csat_score": null,
            "escalated": false
        }
    }'

    wait_for_pipeline

    echo -e "${GREEN}✓ Check results:${NC}"
    echo "  → Grafana Queue Health: http://localhost:3000"
    echo "  → Check DB: psql canvasly -c \"SELECT ticket_id, triage_routing, draft_response FROM tickets WHERE ticket_id='DEMO-003'\""
    check_events
}

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 4: CSAT Anomaly — Enterprise low score
# Trigger: enterprise account, CSAT score 1
# Expected: n8n CSAT Anomaly workflow fires, alerts account owner
# ─────────────────────────────────────────────────────────────────────────────
scenario_4() {
    header "4" "CSAT Anomaly Alert"
    echo "Ticket: Resolved ticket from NovaTech with CSAT score 1/5"
    echo "Expected: n8n CSAT Anomaly workflow detects low score on enterprise account → Slack #customer-health"
    echo ""
    echo -e "${YELLOW}NOTE: This fires directly to the n8n webhook, simulating a CSAT response.${NC}"
    echo ""

    N8N_CSAT_URL="${N8N_CSAT_URL:-http://localhost:5678/webhook/csat-response}"
    echo -e "${GREEN}→ Firing CSAT anomaly to n8n webhook...${NC}"
    response=$(curl -s -X POST "${N8N_CSAT_URL}" \
        -H "Content-Type: application/json" \
        -d '{
            "ticket_id": "DEMO-004",
            "account_name": "NovaTech",
            "account_seat_count": 500,
            "account_owner": "Sarah Chen",
            "is_enterprise": true,
            "csat_score": 1
        }')
    echo "  Response: ${response}"
    echo ""

    wait_for_pipeline

    echo -e "${GREEN}✓ Check results:${NC}"
    echo "  → Slack #customer-health: http://localhost:8004/api/notifications"
    echo "  → n8n execution log: http://localhost:5678"
    check_slack
}

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 5: Churn Risk — Multiple tickets from at-risk account
# Fires 3 tickets from DataForge (health_score=28, renewal in 6 months)
# Expected: Grafana churn radar shows DataForge; n8n daily digest would flag it
# ─────────────────────────────────────────────────────────────────────────────
scenario_5() {
    header "5" "Churn Risk — DataForge Account Cluster"
    echo 'Firing 3 tickets from DataForge (health score 28, $87,500 ARR, renewal July 2026)'
    echo "Expected: Grafana Churn Risk dashboard lights up for DataForge"
    echo ""

    fire "DEMO-005A" '{
        "ticket": {
            "id": "DEMO-005A",
            "created_at": "2026-01-15T09:00:00Z",
            "channel": "email",
            "category": "Canvas - Performance",
            "priority": "medium",
            "agent": {"name": "Priya M."},
            "internal_notes": "DataForge team reports canvas is extremely slow with large datasets. Third complaint this week from this account.",
            "csat_score": 2,
            "escalated": false
        }
    }'

    sleep 1

    fire "DEMO-005B" '{
        "ticket": {
            "id": "DEMO-005B",
            "created_at": "2026-01-15T11:30:00Z",
            "channel": "chat",
            "category": "Magic Import - Error",
            "priority": "medium",
            "agent": {"name": "James K."},
            "internal_notes": "DataForge user getting import errors repeatedly. Very frustrated. Said they are evaluating other tools.",
            "csat_score": 1,
            "escalated": false
        }
    }'

    sleep 1

    fire "DEMO-005C" '{
        "ticket": {
            "id": "DEMO-005C",
            "created_at": "2026-01-15T14:00:00Z",
            "channel": "email",
            "category": "Account - Access",
            "priority": "high",
            "agent": {"name": "Devon R."},
            "internal_notes": "DataForge account admin locked out. SSO issue. Second access issue this month.",
            "csat_score": null,
            "escalated": true
        }
    }'

    wait_for_pipeline

    echo -e "${GREEN}✓ Check results:${NC}"
    echo "  → Grafana Churn Risk dashboard: http://localhost:3000"
    echo "  → DataForge should appear in the high-risk accounts panel"
    check_events
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

SPECIFIC="${1:-}"

# Clear old Slack notifications for a clean demo run
if [ -z "$SPECIFIC" ]; then
    echo -e "${YELLOW}Clearing previous Slack notification log...${NC}"
    curl -s -X DELETE "${SLACK_LOG}" > /dev/null || true
    echo ""
fi

if [ -z "$SPECIFIC" ] || [ "$SPECIFIC" = "1" ]; then
    scenario_1
    [ -z "$SPECIFIC" ] && pause
fi

if [ -z "$SPECIFIC" ] || [ "$SPECIFIC" = "2" ]; then
    scenario_2
    [ -z "$SPECIFIC" ] && pause
fi

if [ -z "$SPECIFIC" ] || [ "$SPECIFIC" = "3" ]; then
    scenario_3
    [ -z "$SPECIFIC" ] && pause
fi

if [ -z "$SPECIFIC" ] || [ "$SPECIFIC" = "4" ]; then
    scenario_4
    [ -z "$SPECIFIC" ] && pause
fi

if [ -z "$SPECIFIC" ] || [ "$SPECIFIC" = "5" ]; then
    scenario_5
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  All scenarios complete.${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Grafana dashboards:    http://localhost:3000  (admin / canvasly_dev)"
echo "  n8n workflows:         http://localhost:5678  (admin / canvasly_dev)"
echo "  Scale Simulator:       http://localhost:8502"
echo "  Slack notification log: http://localhost:8004/api/notifications"
echo "  Event bus log:         http://localhost:8020/events"
echo ""
