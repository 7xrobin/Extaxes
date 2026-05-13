"""
LangGraph node functions.
"""
import re
from django.conf import settings
from openai import OpenAI
from .state import AgentState, TaxState
from .tax_engine import (
    tax_on_exit, vorabpauschale, sparerpauschbetrag_limit,
    exit_tax_applies, effective_rate,
)
from .price_service import get_prices

client = OpenAI(api_key=settings.OPENAI_API_KEY)


def _msg_role(m) -> str:
    """Return 'user' or 'assistant' for both dicts and LangChain message objects."""
    if isinstance(m, dict):
        return m.get("role", "")
    msg_type = getattr(m, "type", "human")
    return "assistant" if msg_type in ("ai", "tool") else "user"


def _msg_content(m) -> str:
    """Return content string for both dicts and LangChain message objects."""
    if isinstance(m, dict):
        return m.get("content", "")
    return getattr(m, "content", str(m))

# ── INTAKE NODE ──────────────────────────────────────────────────────────────

INTAKE_QUESTIONS = [
    ("savings_total",
     "Let's start. How much do you have saved in total across all accounts? "
     "(Give me a rough number in euros — it doesn't have to be exact.)"),

    ("emergency_fund_floor",
     "How much of that do you want to keep as an emergency fund? "
     "Most people in Germany aim for 3–6 months of expenses."),

    ("monthly_investment_budget",
     "How much can you invest each month going forward?"),

    ("goals",
     "What are you investing for? You can have multiple goals — for example: "
     "'financial independence', 'buy a house by 2030', 'just grow my savings'. "
     "List them all, or say 'not sure yet' if you haven't decided."),

    ("risk_profile",
     "Last question for onboarding: how would you feel if your portfolio dropped 20% "
     "in a bad year?\n"
     "  A) I'd be very uncomfortable — I prefer safety\n"
     "  B) Uncomfortable, but I'd hold and wait\n"
     "  C) Fine — I'm in this for the long term"),

    ("tax_bracket",
     "Optional but useful: what's your approximate gross annual salary in Germany? "
     "This helps me estimate your tax bracket. You can skip this by saying 'skip'."),
]


def intake_node(state: AgentState) -> AgentState:
    """
    Conversational onboarding — one question at a time.
    Graph interrupts BEFORE this node, so user message is in state when it runs.
    """
    step = state.get("intake_step", 0)
    messages = list(state.get("messages", []))

    # Parse previous answer if we have one
    if step > 0 and messages:
        last_user_msg = next(
            (_msg_content(m) for m in reversed(messages) if _msg_role(m) == "user"),
            None,
        )
        if last_user_msg:
            state = _parse_intake_answer(dict(state), step - 1, last_user_msg)

    # Ask next question
    if step < len(INTAKE_QUESTIONS):
        _, question = INTAKE_QUESTIONS[step]
        return {
            **state,
            "intake_step": step + 1,
            "messages": messages + [{"role": "assistant", "content": question}],
            "current_node": "intake",
        }

    # All questions answered — move to upload
    return {
        **state,
        "messages": messages + [{
            "role": "assistant",
            "content": (
                "Thanks! I have everything I need. "
                "Now let's look at your portfolio. You can add holdings manually "
                "in the Portfolio tab, or upload a Trade Republic CSV. "
                "Once you've added your positions, come back here and I'll "
                "analyse them and build your strategy."
            ),
        }],
        "current_node": "upload",
    }


def _parse_intake_answer(state: dict, step: int, answer: str) -> dict:
    """Parse user answer and store in correct state field."""
    field, _ = INTAKE_QUESTIONS[step]
    answer = answer.strip()

    if field == "savings_total":
        state["savings_total"] = _extract_number(answer)
    elif field == "emergency_fund_floor":
        state["emergency_fund_floor"] = _extract_number(answer)
        state["investable_surplus"] = max(
            0, state.get("savings_total", 0) - state["emergency_fund_floor"]
        )
    elif field == "monthly_investment_budget":
        state["monthly_investment_budget"] = _extract_number(answer)
    elif field == "goals":
        state["goals"] = _parse_goals(answer)
    elif field == "risk_profile":
        mapping = {"a": "conservative", "b": "balanced", "c": "growth"}
        state["risk_profile"] = mapping.get(answer.lower()[0], "balanced")
    elif field == "tax_bracket":
        state["tax_bracket"] = _estimate_tax_bracket(answer)

    return state


def _extract_number(text: str) -> float:
    """Extract first number from text. Handles €8,000 / 8000 / 8k."""
    text = text.lower().replace(",", "").replace("€", "").replace("eur", "")
    match = re.search(r"(\d+\.?\d*)\s*(k)?", text)
    if not match:
        return 0.0
    val = float(match.group(1))
    if match.group(2) == "k":
        val *= 1000
    return val


def _parse_goals(text: str) -> list[dict]:
    """Parse free-text goals into Goal list."""
    if any(w in text.lower() for w in ["not sure", "skip", "unsure", "don't know"]):
        return [{"name": "grow savings", "target_amount": 0,
                 "target_date": "open", "monthly_allocation": 0, "priority": 1}]
    goals = []
    for i, part in enumerate(text.split(",")):
        part = part.strip()
        if part:
            goals.append({
                "name": part,
                "target_amount": 0,
                "target_date": "open",
                "monthly_allocation": 0,
                "priority": i + 1,
            })
    return goals or [{"name": "grow savings", "target_amount": 0,
                      "target_date": "open", "monthly_allocation": 0, "priority": 1}]


def _estimate_tax_bracket(text: str) -> float:
    """Estimate marginal tax rate from gross salary."""
    salary = _extract_number(text)
    if salary == 0 or "skip" in text.lower():
        return 0.42  # default assumption for demo
    if salary < 20_000:
        return 0.14
    elif salary < 40_000:
        return 0.30
    elif salary < 68_000:
        return 0.37
    else:
        return 0.42


def route_after_intake(state: AgentState) -> str:
    if state.get("current_node") == "upload":
        return "upload"
    return "continue_intake"


# ── UPLOAD NODE ───────────────────────────────────────────────────────────────

def upload_node(state: AgentState) -> AgentState:
    """
    Triggered after intake is complete.
    Holdings are loaded from the Django DB (set by portfolio views).
    This node converts DB holdings to AgentState format.
    """
    from portfolio.models import Holding as DBHolding, UserProfile

    user_id = state.get("user_id", "demo")
    try:
        profile = UserProfile.objects.get(user_id=user_id)
        db_holdings = list(profile.holdings.all())
    except UserProfile.DoesNotExist:
        db_holdings = []

    holdings = [
        {
            "ticker":              h.ticker,
            "isin":                h.isin,
            "asset_type":          h.asset_type,
            "units":               h.units,
            "avg_purchase_price":  h.avg_purchase_price,
            "purchase_date":       str(h.purchase_date) if h.purchase_date else "",
            "current_price":       0.0,
            "current_value":       0.0,
            "unrealised_gain":     0.0,
            "unrealised_gain_pct": 0.0,
        }
        for h in db_holdings
    ]

    return {
        **state,
        "holdings": holdings,
        "current_node": "analysis",
        "messages": list(state.get("messages", [])) + [{
            "role": "assistant",
            "content": (
                f"I can see {len(holdings)} position(s) in your portfolio. "
                "Let me fetch live prices and analyse your holdings..."
            ),
        }],
    }


# ── ANALYSIS NODE ─────────────────────────────────────────────────────────────

def analysis_node(state: AgentState) -> AgentState:
    """Pure calculation — no LLM. Fetches live prices, computes gains and tax."""
    holdings = state.get("holdings", [])
    is_married = state.get("is_married", False)

    tickers = [h["ticker"] for h in holdings]
    prices = get_prices(tickers)

    updated = []
    total_invested = 0.0
    total_value = 0.0

    for h in holdings:
        price = prices.get(h["ticker"], 0.0)
        cost  = h["units"] * h["avg_purchase_price"]
        value = h["units"] * price
        gain  = value - cost
        gain_pct = (gain / cost * 100) if cost > 0 else 0.0

        updated.append({**h,
            "current_price":       price,
            "current_value":       value,
            "unrealised_gain":     gain,
            "unrealised_gain_pct": gain_pct,
        })
        total_invested += cost
        total_value    += value

    allowance = sparerpauschbetrag_limit(is_married)
    tax_positions = []

    for h in updated:
        vp       = vorabpauschale(h["current_value"], h["asset_type"])
        tax_now  = tax_on_exit(h["unrealised_gain"], h["asset_type"])
        rate     = effective_rate(h["asset_type"]) * 100

        tax_positions.append({
            "ticker":                h["ticker"],
            "asset_type":            h["asset_type"],
            "unrealised_gain":       h["unrealised_gain"],
            "tax_if_sold_now":       tax_now,
            "effective_rate_pct":    rate,
            "vorabpauschale_annual": vp,
            "note":                  _tax_note(h["asset_type"]),
        })

    tax_state: TaxState = {
        "sparerpauschbetrag_used":       0.0,
        "sparerpauschbetrag_remaining":  allowance,
        "vorabpauschale_total_estimate": sum(p["vorabpauschale_annual"] for p in tax_positions),
        "exit_tax_warning":              exit_tax_applies(total_invested),
        "positions":                     tax_positions,
    }

    return {
        **state,
        "holdings":              updated,
        "total_invested":        total_invested,
        "total_current_value":   total_value,
        "total_unrealised_gain": total_value - total_invested,
        "tax":                   tax_state,
        "current_node":          "plan",
    }


def _tax_note(asset_type: str) -> str:
    notes = {
        "etf_acc":  "Accumulating ETF — 30% tax exemption applies (Teilfreistellung §20 InvStG). Annual Vorabpauschale deducted in January.",
        "etf_dist": "Distributing ETF — 30% tax exemption applies. Dividends taxed when received.",
        "stock":    "Individual stock — no tax exemption. Full 26.375% on gains.",
        "savings":  "Savings product — interest taxed at 26.375% when credited.",
    }
    return notes.get(asset_type, "")


# ── PLAN NODE ─────────────────────────────────────────────────────────────────

PLAN_SYSTEM_PROMPT = """You are Kyron, a financial clarity assistant for expats in Germany.
Your job is to help users understand their investment situation and think through a strategy.

IMPORTANT RULES:
- Never recommend specific products to buy or sell
- Always frame suggestions as "options to consider" or "one approach could be"
- Always include: "This is educational — not personal investment advice."
- Keep language simple and direct — avoid jargon
- When mentioning taxes, use plain English first, then the German term in brackets
- Be concise — no more than 300 words in your plan proposal
"""


def plan_node(state: AgentState) -> AgentState:
    """GPT-4o call — generates diversification plan with exit rules."""
    context = f"""
User profile:
- Total savings: €{state.get('savings_total', 0):,.0f}
- Emergency fund: €{state.get('emergency_fund_floor', 0):,.0f}
- Available to invest: €{state.get('investable_surplus', 0):,.0f}
- Monthly budget: €{state.get('monthly_investment_budget', 0):,.0f}/month
- Risk profile: {state.get('risk_profile', 'balanced')}
- Tax bracket: {state.get('tax_bracket', 0.42) * 100:.0f}%
- Goals: {[g['name'] for g in state.get('goals', [])]}

Current portfolio: {len(state.get('holdings', []))} positions
Total invested: €{state.get('total_invested', 0):,.0f}
Current value: €{state.get('total_current_value', 0):,.0f}
Unrealised gain: €{state.get('total_unrealised_gain', 0):,.0f}

Tax status:
- Annual tax-free allowance remaining: €{state.get('tax', {}).get('sparerpauschbetrag_remaining', 1000):,.0f}
- Estimated annual ETF advance tax (Vorabpauschale): €{state.get('tax', {}).get('vorabpauschale_total_estimate', 0):,.2f}

Propose:
1. A suggested allocation across 2-4 ETF/asset categories (not specific products)
2. How to split the €{state.get('monthly_investment_budget', 0):,.0f}/month budget across goals
3. Simple exit rules: when it makes sense to review or take profits
4. One plain-English tax insight relevant to their situation
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user",   "content": context},
        ],
        max_tokens=600,
    )
    plan_text = response.choices[0].message.content
    messages = list(state.get("messages", []))

    return {
        **state,
        "approved_strategy": {"plan_text": plan_text},
        "current_node": "approval",
        "messages": messages + [
            {"role": "assistant", "content": plan_text},
            {"role": "assistant", "content":
                "Does this look right to you? You can say:\n"
                "• **'Looks good'** — I'll save this as your strategy\n"
                "• **'Adjust [something]'** — I'll revise the plan\n"
                "• **'More conservative'** or **'More aggressive'** — I'll shift the approach"
            },
        ],
    }


# ── APPROVAL NODE ─────────────────────────────────────────────────────────────

def approval_node(state: AgentState) -> AgentState:
    """
    Reads user's approval or adjustment request.
    Graph interrupts BEFORE this node — user message is already in state.
    """
    messages = list(state.get("messages", []))
    last_user = next(
        (_msg_content(m) for m in reversed(messages) if _msg_role(m) == "user"), ""
    ).lower()

    approved = any(w in last_user for w in [
        "good", "looks good", "yes", "approve", "save", "confirm", "ok", "perfect", "great",
    ])

    if approved:
        return {
            **state,
            "current_node": "done",
            "messages": messages + [{"role": "assistant", "content":
                "Strategy saved. I'll track your portfolio against this plan.\n\n"
                "You can view your portfolio and tax status in the Portfolio tab, "
                "or generate your weekly digest anytime from the Digest tab."
            }],
        }

    return {
        **state,
        "current_node": "adjust",
        "messages": messages + [{"role": "assistant", "content":
            "Got it — let me revise the plan with your feedback..."
        }],
    }


def route_after_approval(state: AgentState) -> str:
    return "done" if state.get("current_node") == "done" else "adjust"


# ── DIGEST NODE ───────────────────────────────────────────────────────────────

DIGEST_SYSTEM_PROMPT = """You are Kyron generating a weekly investment digest.

Write in plain English — imagine explaining to a smart friend who doesn't know
finance terms. Structure:
1. Portfolio snapshot (total value, gain/loss, best/worst performer)
2. One action to pay attention to (exit rule triggered? tax allowance running low?)
3. Tax status in simple terms
4. Monthly plan check (on track?)

Rules:
- Explain German tax terms in plain English FIRST, then add the term in brackets
- Keep each section under 80 words
- Never say "you should buy/sell X" — say "it might be worth reviewing" or "one option is"
- End with: "This is educational — not personal investment advice."
"""


def digest_node(state: dict) -> dict:
    """GPT-4o call — generates weekly plain-English digest."""
    holdings = state.get("holdings", [])
    holdings_summary = "\n".join([
        f"  {h['ticker']}: €{h.get('current_value', 0):,.0f} | "
        f"gain: €{h.get('unrealised_gain', 0):,.0f} ({h.get('unrealised_gain_pct', 0):+.1f}%) | "
        f"type: {h['asset_type']}"
        for h in holdings
    ]) or "  No holdings recorded yet."

    tax = state.get("tax", {})
    context = f"""
Portfolio as of today:
{holdings_summary}

Total invested:    €{state.get('total_invested', 0):,.0f}
Current value:     €{state.get('total_current_value', 0):,.0f}
Total gain/loss:   €{state.get('total_unrealised_gain', 0):,.0f}

Tax status:
  Annual tax-free allowance used: €{tax.get('sparerpauschbetrag_used', 0):,.0f}
  Remaining: €{tax.get('sparerpauschbetrag_remaining', 1000):,.0f}
  Estimated annual ETF advance tax: €{tax.get('vorabpauschale_total_estimate', 0):,.2f}
  Exit tax warning: {tax.get('exit_tax_warning', False)}

Monthly plan:
  Budget: €{state.get('monthly_investment_budget', 0):,.0f}/month
  Goals: {[g['name'] for g in state.get('goals', [])]}
  Approved strategy: {state.get('approved_strategy', {}).get('plan_text', 'Not yet defined')[:200]}
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
            {"role": "user",   "content": context},
        ],
        max_tokens=700,
    )

    digest_text = response.choices[0].message.content
    return {
        **state,
        "messages": list(state.get("messages", [])) + [
            {"role": "assistant", "content": digest_text}
        ],
    }
