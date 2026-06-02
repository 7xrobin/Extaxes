"""
LangGraph node functions.
"""
import json
import re
from django.conf import settings
from django.utils import timezone
from openai import OpenAI
from portfolio.models import UserProfile
from .state import AgentState, TaxState
from .tax_engine import (
    tax_on_exit, vorabpauschale, sparerpauschbetrag_limit,
    exit_tax_applies, effective_rate,
)
from .price_service import get_prices
from .simulation import (
    DEFAULT_ANNUAL_RETURN_PCT, project_growth,
    recommend_investment, required_monthly_for_target,
)

client = OpenAI(api_key=settings.OPENAI_API_KEY)


# ── RAG + DETERMINISTIC TOOLS ──────────────────────────────────────────────────

def _tax_rag_context(query: str) -> str:
    """
    Retrieve trusted tax-source passages for `query`. Import is guarded so the
    graph keeps working when the rag app has no indexed sources (or none match).
    Returns a prompt-ready, source-cited block, or "" when nothing relevant.
    """
    try:
        from rag.retriever import build_context
        ctx = build_context(query)
    except Exception:
        return ""
    if not ctx:
        return ""
    return (
        "\n\nTRUSTED TAX SOURCES — treat these as the PRIMARY source of truth for any "
        "tax question. Prefer them over your own assumptions, and cite the source URL "
        "in your answer when you use them:\n" + ctx
    )


_PROJECTION_KEYWORDS = (
    "how much", "simulate", "simulation", "project", "projection", "grow to",
    "worth in", "in 10 years", "in 5 years", "in 20 years", "future value",
    "compound", "to reach", "to hit", "per month to", "each month to",
)


def _projection_context(query: str, state: dict) -> str:
    """
    If the user is asking a how-much / projection question, compute the figures
    deterministically (the LLM must not do the math) and return a prompt block
    with exact numbers. Returns "" when the question isn't projection-related.
    """
    q = (query or "").lower()
    if not any(kw in q for kw in _PROJECTION_KEYWORDS):
        return ""

    start_amount = float(state.get("total_current_value", 0) or 0)
    monthly = float(state.get("monthly_investment_budget", 0) or 0)

    years_match = re.search(r"(\d{1,2})\s*(?:year|yr)", q)
    years = int(years_match.group(1)) if years_match else 10

    proj = project_growth(
        start_amount=start_amount,
        monthly_contribution=monthly,
        annual_return_pct=DEFAULT_ANNUAL_RETURN_PCT,
        years=years,
    )
    rec = recommend_investment(
        state.get("investable_surplus", 0), monthly
    )

    lines = [
        "\n\nCOMPUTED PROJECTION (use these EXACT figures — do not recalculate):",
        f"- Assumptions: start €{proj['inputs']['start_amount']:,.0f}, "
        f"€{proj['inputs']['monthly_contribution']:,.0f}/month, "
        f"{proj['inputs']['annual_return_pct']:.0f}%/yr avg return, "
        f"{years} years, accumulating world ETF.",
        f"- Total contributed over the period: €{proj['total_contributed']:,.0f}",
        f"- Projected value (gross): €{proj['final_gross']:,.0f}",
        f"- Projected gain: €{proj['total_gain']:,.0f}",
        f"- Estimated German tax if fully sold at the end "
        f"(~{proj['effective_rate_pct']:.1f}% effective): €{proj['tax_on_gain']:,.0f}",
        f"- Projected value after tax (net): €{proj['final_net']:,.0f}",
        f"- 'How much to invest' from their own numbers: lump sum available "
        f"€{rec['lump_sum_available']:,.0f}, plus €{rec['monthly_contribution']:,.0f}/month.",
    ]

    # If they named a target amount, also solve for the required monthly contribution.
    target_match = re.search(r"(?:reach|hit|get to|target of)\s*€?\s*([\d.,]+)\s*(k|m)?", q)
    if target_match:
        target = _extract_number(target_match.group(0))
        if target > 0:
            need = required_monthly_for_target(
                target, years, DEFAULT_ANNUAL_RETURN_PCT, start_amount
            )
            lines.append(
                f"- To reach €{target:,.0f} in {years} years from €{start_amount:,.0f}, "
                f"they'd need about €{need:,.0f}/month."
            )

    return "\n".join(lines)


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
        if step == 0:
            content = (
                "Hi! I'm Kyron, your financial clarity assistant for expats in Germany. "
                "I'll help you understand your investments, estimate your tax exposure, "
                "and build a plain-English strategy — no jargon, no product pitches.\n\n"
                "Let's start with a few quick questions.\n\n"
                + question
            )
        else:
            content = question
        return {
            **state,
            "intake_step": step + 1,
            "messages": messages + [{"role": "assistant", "content": content}],
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

    if holdings:
        upload_msg = (
            f"I can see {len(holdings)} position(s) in your portfolio. "
            "Let me fetch live prices and analyse your holdings..."
        )
    else:
        upload_msg = (
            "Your portfolio is empty right now. "
            "You can add positions manually in the Portfolio tab, or upload a Trade Republic CSV. "
            "Once you've added your holdings, come back here and I'll analyse them and build your strategy."
        )

    return {
        **state,
        "holdings": holdings,
        "current_node": "analysis",
        "messages": list(state.get("messages", [])) + [
            {"role": "assistant", "content": upload_msg},
        ],
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
        "savings":  "Savings / Cash — interest taxed at 26.375% when credited.",
    }
    return notes.get(asset_type, "")


# ── PLAN NODE ─────────────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _split_plan_and_json(raw: str):
    """Extract prose and a trailing ```json``` block. Returns (prose, data_or_None)."""
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return raw.strip(), None
    prose = raw[:match.start()].rstrip()
    try:
        data = json.loads(match.group(1))
        cats = data.get("categories")
        if isinstance(cats, list) and all(
            isinstance(c, dict) and "name" in c and "allocation_pct" in c for c in cats
        ):
            return prose, data
    except json.JSONDecodeError:
        pass
    return prose, None


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
    """GPT-4o call — generates/revises diversification plan with exit rules."""
    messages = list(state.get("messages", []))

    # If the user sent an adjustment request (re-running after approval), include it
    last_user_msg = next(
        (_msg_content(m) for m in reversed(messages) if _msg_role(m) == "user"),
        None,
    )
    adjustment_section = (
        f"\nUser's adjustment request (revise the plan to address this): {last_user_msg}\n"
        if last_user_msg else ""
    )

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
{adjustment_section}
Propose:
1. A suggested allocation across 2-4 ETF/asset categories (not specific products)
2. How to split the €{state.get('monthly_investment_budget', 0):,.0f}/month budget across goals
3. Simple exit rules: when it makes sense to review or take profits
4. One plain-English tax insight relevant to their situation

After the prose, append a fenced JSON block (```json ... ```) with the structured allocation in this exact shape, using the same categories you proposed:
{{"categories": [{{"name": "Core World ETF", "allocation_pct": 60}}, ...], "total_target_amount": <number in euros>}}
The allocation_pct values must sum to 100. The total_target_amount is the user's intended invested capital target (e.g. investable_surplus, or annual budget × years for long-horizon goals — pick a sensible number).
"""

    response = client.chat.completions.create(
        # TODO: Make this callable with a flag for model choice, default to gpt-4o-mini
        model="gpt-4o",
        messages=[
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user",   "content": context},
        ],
        max_tokens=700,
    )
    raw = response.choices[0].message.content or ""
    plan_text, strategy_data = _split_plan_and_json(raw)
    messages = list(state.get("messages", []))

    return {
        **state,
        "approved_strategy": {"plan_text": plan_text, "data": strategy_data},
        "strategy_saved": False,
        "current_node": "qa",
        "messages": messages + [
            {"role": "assistant", "content": plan_text},
            {"role": "assistant", "content":
                "Does this look right to you?\n"
                "• **'Looks good'** — I'll save this as your strategy\n"
                "• **'Adjust [something]'** — I'll revise the plan\n"
                "• **'More conservative'** or **'More aggressive'** — I'll shift the approach\n"
                "• Or ask me anything about the strategy first."
            },
        ],
    }


# ── APPROVAL NODE ─────────────────────────────────────────────────────────────
# Thin save-only node — called by route_after_qa when the user approves.
# No user interaction here; graph does not interrupt before this node.

_APPROVAL_CLASSIFIER_PROMPT = """You are classifying a user message during an investment plan review.

Categories:
- approve  — user is happy and wants to save the plan ("looks good", "save it", "yes", "go ahead", "perfect")
- adjust   — user explicitly asks to CHANGE something (more conservative, change allocation, remove X, add Y)
- question — user asks for explanation or information, without requesting a change

Examples:
"Looks good" → approve
"Yes, save it" → approve
"Go ahead" → approve
"Can you make it more conservative?" → adjust
"Change the bond allocation to 30%" → adjust
"Less risk please" → adjust
"What is a Vorabpauschale?" → question
"Why ETFs over stocks?" → question
"Can you explain the exit rule?" → question
"What does this mean for me?" → question
"What if I had more bonds instead?" → question
"How does the tax work?" → question

Output ONLY one of these three words, lowercase, no punctuation, no extra text:
approve
adjust
question

If unsure, output: question"""


def approval_node(state: AgentState) -> AgentState:
    """Saves the approved strategy to DB. Called without interrupt by route_after_qa."""
    messages = list(state.get("messages", []))
    approved = state.get("approved_strategy") or {}
    user_id = state.get("user_id") or "demo"

    profile, _ = UserProfile.objects.get_or_create(user_id=user_id)
    profile.strategy_approved = True
    profile.approved_strategy_text = approved.get("plan_text", "") or ""
    profile.approved_strategy_data = approved.get("data")
    profile.strategy_approved_at = timezone.now()
    profile.save()

    return {
        **state,
        "strategy_saved": True,
        "current_node": "qa",
        "messages": messages + [{"role": "assistant", "content":
            "Strategy saved. I'll track your portfolio against this plan.\n\n"
            "You can view your portfolio and tax status in the Portfolio tab, "
            "or generate your weekly digest anytime from the Digest tab.\n\n"
            "Feel free to ask me anything about your strategy or portfolio."
        }],
    }


# ── Q&A NODE ──────────────────────────────────────────────────────────────────

_QA_APPROVED_PROMPT = """You are Kyron, a financial clarity assistant for expats in Germany.
The user has approved their investment strategy. Answer follow-up questions helpfully and concisely (under 200 words).

Rules:
- Reference specific parts of the strategy only when directly relevant — do NOT repeat the full strategy text
- Explain German tax terms in plain English first, then add the term in brackets
- Never say "you should buy/sell X" — say "it might be worth reviewing" or "one option is"
- This is educational — not personal investment advice."""

_QA_PENDING_PROMPT = """You are Kyron, a financial clarity assistant for expats in Germany.
You have just proposed an investment strategy to the user. They may ask questions before deciding.
Answer their question concisely (under 150 words), then ask whether the plan looks good to them.

Rules:
- Reference specific parts of the strategy only when directly relevant — do NOT repeat the full strategy text
- Explain German tax terms in plain English first, then add the term in brackets
- Never say "you should buy/sell X"
- End every response with: "Does the plan look good to you, or would you like to change anything?"
- This is educational — not personal investment advice."""


def qa_node(state: AgentState) -> AgentState:
    """
    Universal post-plan Q&A node. Handles both pending-approval and approved states.
    Graph interrupts BEFORE this node each turn.
    """
    messages = list(state.get("messages", []))
    strategy_saved = state.get("strategy_saved", False)
    strategy = state.get("approved_strategy", {}).get("plan_text", "No strategy on file.")
    profile_ctx = (
        f"Risk profile: {state.get('risk_profile', 'unknown')} | "
        f"Monthly budget: €{state.get('monthly_investment_budget', 0):.0f} | "
        f"Tax bracket: {state.get('tax_bracket', 0.42) * 100:.0f}%"
    )

    last_user = next(
        (_msg_content(m) for m in reversed(messages) if _msg_role(m) == "user"), ""
    )

    # While strategy is pending, classify intent before calling the LLM
    if not strategy_saved:
        intent_resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": _APPROVAL_CLASSIFIER_PROMPT},
                {"role": "user",   "content": last_user},
            ],
            max_tokens=3,
            temperature=0,
            seed=42,
        )
        raw_intent = (intent_resp.choices[0].message.content or "").strip().lower()
        first_word = raw_intent.split()[0] if raw_intent.split() else ""
        intent = re.sub(r"[^a-z]", "", first_word)

        # Defense in depth: even if classifier says "adjust", require an
        # explicit change phrase in the user message before re-running plan.
        _ADJUST_PHRASES = (
            "adjust", "change", "revise", "more conservative", "more aggressive",
            "less risk", "more risk", "different", "instead of",
        )
        if intent == "adjust" and not any(p in last_user.lower() for p in _ADJUST_PHRASES):
            intent = "question"

        if intent == "approve":
            return {
                **state,
                "current_node": "save",
                "messages": messages + [{"role": "assistant", "content":
                    "Got it — saving your strategy now..."
                }],
            }

        if intent == "adjust":
            return {
                **state,
                "current_node": "adjust",
                "messages": messages + [{"role": "assistant", "content":
                    "Got it — let me revise the plan with your feedback..."
                }],
            }

    # Trusted tax sources + any deterministic projection — injected into either branch
    # so numbers stay accurate and tax answers are grounded in the curated sources.
    extra_context = _tax_rag_context(last_user) + _projection_context(last_user, dict(state))

    if not strategy_saved:
        # Anything other than approve/adjust (question, empty, hallucinated) → safe Q&A path
        system_content = (
            _QA_PENDING_PROMPT
            + f"\n\nProposed strategy:\n{strategy}"
            + f"\n\nUser profile: {profile_ctx}"
            + extra_context
        )
    else:
        system_content = (
            _QA_APPROVED_PROMPT
            + f"\n\nApproved strategy:\n{strategy}"
            + f"\n\nUser profile: {profile_ctx}"
            + extra_context
        )

    conversation = [{"role": "system", "content": system_content}]
    for m in messages:
        role = _msg_role(m)
        content = _msg_content(m)
        if role in ("user", "assistant"):
            conversation.append({"role": role, "content": content})

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=conversation,
        max_tokens=400,
    )
    answer = response.choices[0].message.content
    return {
        **state,
        "current_node": "qa",
        "messages": messages + [{"role": "assistant", "content": answer}],
    }


def route_after_qa(state: AgentState) -> str:
    node = state.get("current_node", "qa")

    # qa_node signals approval or adjust via current_node
    if node == "save":
        return "approval"
    if node == "adjust":
        return "plan"

    # Approved state: check for explicit plan-revision keywords
    messages = list(state.get("messages", []))
    last_user = next(
        (_msg_content(m) for m in reversed(messages) if _msg_role(m) == "user"), ""
    ).lower()
    revise_keywords = ["revise plan", "change plan", "new plan", "redo plan", "update strategy"]
    if any(kw in last_user for kw in revise_keywords):
        return "plan"

    return "qa"


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

    # Ground the tax section in the curated sources when any are indexed.
    context += _tax_rag_context(
        "German ETF capital gains tax, Vorabpauschale, Sparerpauschbetrag allowance"
    )

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
