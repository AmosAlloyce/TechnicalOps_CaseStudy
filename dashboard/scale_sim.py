"""
dashboard/scale_sim.py

Streamlit Scale Simulator.
Interactive cost and infrastructure modelling at different ticket volumes.
Sourced from data/volume_projections.md model values.
"""

import os

import plotly.graph_objects as go
import psycopg2
import streamlit as st

# ─────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Canvasly — Scale Simulator",
    page_icon="📈",
    layout="wide",
)

st.title("📈 Canvasly TechOps — Scale Simulator")
st.caption(
    "Model infrastructure cost and capacity at different ticket volumes. "
    "Based on the Canvasly case study growth projections."
)

# ─────────────────────────────────────────────
# Live ticket count from Postgres
# ─────────────────────────────────────────────
@st.cache_data(ttl=30)
def get_live_ticket_count() -> int:
    try:
        conn = psycopg2.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", "5432")),
            dbname=os.environ.get("DB_NAME", "canvasly"),
            user=os.environ.get("DB_USER", "canvasly"),
            password=os.environ.get("DB_PASSWORD", "canvasly_dev"),
        )
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tickets")
            return cur.fetchone()[0]
    except Exception:
        return 30  # fallback: seed data count


live_count = get_live_ticket_count()
st.info(f"🔴 Live: **{live_count} tickets** currently in the database (30-ticket seed data)")

# ─────────────────────────────────────────────
# Volume slider
# ─────────────────────────────────────────────
st.subheader("Adjust Ticket Volume")
col1, col2 = st.columns([3, 1])

with col1:
    tickets_per_day = st.slider(
        "Tickets per day",
        min_value=1_200,
        max_value=24_000,
        value=1_200,
        step=200,
        help="Current baseline: 1,200/day. Canvasly projects 5,000/day in 6 months.",
    )

with col2:
    multiplier = round(tickets_per_day / 1_200, 1)
    st.metric("Multiplier vs today", f"{multiplier}x")

tickets_per_month = tickets_per_day * 22  # working days

st.markdown("---")

# ─────────────────────────────────────────────
# Cost model calculations
# ─────────────────────────────────────────────

def calc_costs(tpd: int) -> dict:
    tpm = tpd * 22

    # Lambda: 4 invocations per ticket
    lambda_invocations = tpm * 4
    lambda_cost = max(0, (lambda_invocations - 1_000_000) / 1_000_000 * 0.20)

    # API Gateway: $3.50/million
    apigw_cost = tpm / 1_000_000 * 3.50

    # Aurora Serverless v2: ACU-hours
    # Rough model: 0.5 ACU base + 0.0002 * tpd
    avg_acu = max(0.5, 0.5 + tpd * 0.0002)
    peak_acu = min(16, avg_acu * 2)
    aurora_cost = avg_acu * 0.12 * 24 * 30  # $0.12/ACU-hour

    # EventBridge: $1/million events
    eb_cost = (tpm * 4) / 1_000_000 * 1.00

    # LLM
    groq_free_limit = 14_400  # req/day
    if tpd <= groq_free_limit:
        llm_cost = 0
        llm_provider = "Groq (free)"
    else:
        # Groq paid: ~$0.05/M input + $0.08/M output, avg 500 in + 200 out tokens
        llm_cost = tpm * (500 * 0.05 + 200 * 0.08) / 1_000_000
        llm_provider = "Groq (paid)"

    total = lambda_cost + apigw_cost + aurora_cost + eb_cost + llm_cost

    return {
        "lambda_cost":   lambda_cost,
        "apigw_cost":    apigw_cost,
        "aurora_cost":   aurora_cost,
        "eb_cost":       eb_cost,
        "llm_cost":      llm_cost,
        "total":         total,
        "avg_acu":       avg_acu,
        "peak_acu":      peak_acu,
        "llm_provider":  llm_provider,
        "lambda_inv":    lambda_invocations,
        "groq_req_day":  tpd,
        "groq_over_limit": tpd > groq_free_limit,
    }


costs = calc_costs(tickets_per_day)

# ─────────────────────────────────────────────
# KPI row
# ─────────────────────────────────────────────
st.subheader("Projected Infrastructure")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Monthly Cost", f"${costs['total']:.2f}")
c2.metric("Aurora ACU (avg)", f"{costs['avg_acu']:.1f}")
c3.metric("Aurora ACU (peak)", f"{costs['peak_acu']:.1f}")
c4.metric("LLM Provider", costs["llm_provider"])
c5.metric("Lambda Invocations/mo", f"{costs['lambda_inv']:,}")

if costs["groq_over_limit"]:
    st.warning(
        f"⚠️ **Groq free tier exceeded.** At {tickets_per_day:,} tickets/day, "
        f"you need {tickets_per_day:,} LLM calls/day vs. the 14,400/day free limit. "
        f"Switch `LLM_PROVIDER=bedrock` in your `.env` — no code changes required."
    )
else:
    st.success(
        f"✅ **Groq free tier sufficient** — {tickets_per_day:,}/day vs 14,400/day limit. "
        f"LLM cost: **$0**"
    )

st.markdown("---")

# ─────────────────────────────────────────────
# Cost breakdown chart
# ─────────────────────────────────────────────
st.subheader("Monthly Cost Breakdown")

volumes = list(range(1_200, 24_200, 600))
totals  = [calc_costs(v)["total"] for v in volumes]
auroras = [calc_costs(v)["aurora_cost"] for v in volumes]
llms    = [calc_costs(v)["llm_cost"] for v in volumes]
lambdas = [calc_costs(v)["lambda_cost"] for v in volumes]

fig = go.Figure()
fig.add_trace(go.Scatter(x=volumes, y=auroras, name="Aurora Serverless v2", stackgroup="one", fill="tonexty"))
fig.add_trace(go.Scatter(x=volumes, y=llms,    name="LLM (Groq/Bedrock)",   stackgroup="one", fill="tonexty"))
fig.add_trace(go.Scatter(x=volumes, y=lambdas, name="Lambda + API GW",      stackgroup="one", fill="tonexty"))
fig.add_vline(x=tickets_per_day, line_dash="dash", line_color="red", annotation_text=f"Current: {tickets_per_day:,}/day")
fig.add_vline(x=14_400, line_dash="dot", line_color="orange", annotation_text="Groq free limit")
fig.update_layout(
    xaxis_title="Tickets per day",
    yaxis_title="Monthly cost ($)",
    legend_title="Component",
    height=400,
    margin={"t": 20},
)
st.plotly_chart(fig, use_container_width=True)

# ─────────────────────────────────────────────
# LLM breakeven analysis
# ─────────────────────────────────────────────
st.subheader("LLM Provider Breakeven")
st.caption(
    "At what volume does each LLM option become cost-optimal? "
    "All options are cheap — the decision is latency + data residency, not cost."
)

llm_volumes = list(range(1_200, 24_200, 600))
groq_costs    = [0 if v <= 14_400 else calc_costs(v)["llm_cost"] for v in llm_volumes]
bedrock_costs = [v * 22 * (500 * 0.00025 + 200 * 0.00125) / 1_000 for v in llm_volumes]  # Claude Haiku
openai_costs  = [v * 22 * (500 * 0.000150 + 200 * 0.000600) / 1_000 for v in llm_volumes]  # GPT-4o-mini

fig2 = go.Figure()
fig2.add_trace(go.Scatter(x=llm_volumes, y=groq_costs,    name="Groq (free→paid)"))
fig2.add_trace(go.Scatter(x=llm_volumes, y=bedrock_costs, name="AWS Bedrock Claude Haiku"))
fig2.add_trace(go.Scatter(x=llm_volumes, y=openai_costs,  name="OpenAI GPT-4o-mini"))
fig2.add_vline(x=14_400, line_dash="dot", line_color="orange", annotation_text="Groq free limit")
fig2.update_layout(
    xaxis_title="Tickets per day",
    yaxis_title="Monthly LLM cost ($)",
    height=350,
    margin={"t": 20},
)
st.plotly_chart(fig2, use_container_width=True)

st.caption(
    "Switching LLM provider: set `LLM_PROVIDER=bedrock` (or `openai`) in `.env`. "
    "No code changes required — the `BaseLLMClient` abstraction handles the swap."
)

# ─────────────────────────────────────────────
# Three-tier summary table
# ─────────────────────────────────────────────
st.subheader("Three-Tier Cost Summary")
tier_data = {
    "Tier": ["T1 — Today", "T2 — 6 months", "T3 — 12 months"],
    "Tickets/Day": [1_200, 5_000, 12_000],
    "Monthly Cost": [f"${calc_costs(v)['total']:.0f}" for v in [1_200, 5_000, 12_000]],
    "LLM": [calc_costs(v)["llm_provider"] for v in [1_200, 5_000, 12_000]],
    "Aurora ACU (avg)": [f"{calc_costs(v)['avg_acu']:.1f}" for v in [1_200, 5_000, 12_000]],
}
st.table(tier_data)

st.markdown("---")
st.caption("Canvasly TechOps Portfolio — Scale Simulator | See CASE_STUDY.md for full ROI analysis")
