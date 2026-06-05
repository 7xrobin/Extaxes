"""
LangGraph node functions.

Agent capabilities (live prices, tax-source RAG, growth projections) live in
agent/tools.py as traceable tools. The nodes below are thin wrappers that invoke
those tools and write their results into graph state, so each tool is both a
LangSmith span and an explicit node on the graph.
"""
import json
import re
import logging
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
from .observability import instrument_openai
from .tools import fetch_prices, retrieve_tax_context, compute_projection
from .validators import validate_plan_alignment
from . import catalog

logger = logging.getLogger("agent.nodes")

# OpenAI client, instrumented for LangSmith when tracing is enabled (no-op otherwise).
client = instrument_openai(OpenAI(api_key=settings.OPENAI_API_KEY))


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
                "Hi! I'm InvestBuddy, your financial clarity assistant for expats in Germany. "
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


# ── FETCH-PRICES TOOL NODE ────────────────────────────────────────────────────

def fetch_prices_node(state: AgentState) -> AgentState:
    """
    Tool node: pulls live prices for the current holdings and stores them in
    state so the (pure-calculation) analysis node can stay LLM- and IO-free.
    """
    holdings = state.get("holdings", [])
    tickers = [h["ticker"] for h in holdings]
    logger.info("fetch_prices_node: fetching %d ticker(s)", len(tickers))
    prices = fetch_prices(tickers)
    return {**state, "prices": prices, "current_node": "analysis"}


# ── ANALYSIS NODE ─────────────────────────────────────────────────────────────

def analysis_node(state: AgentState) -> AgentState:
    """Pure calculation — no LLM. Consumes prices from the fetch_prices node."""
    holdings = state.get("holdings", [])
    is_married = state.get("is_married", False)

    tickers = [h["ticker"] for h in holdings]
    # Prefer prices populated by fetch_prices_node; fall back to a direct fetch so
    # analysis_node remains correct when called on its own (e.g. in unit tests).
    prices = state["prices"] if "prices" in state else get_prices(tickers)

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


# Number of automatic re-prompts allowed when the proposed allocation doesn't match
# the user's risk profile. One retry keeps the self-correcting loop bounded and fast.
MAX_PLAN_ALIGNMENT_RETRIES = 1


PLAN_SYSTEM_PROMPT = """You are InvestBuddy, a financial clarity assistant for expats in Germany.
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
    """LLM call — generates/revises diversification plan with exit rules."""
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
2. How to split the €{state.get('monthly_investment_budget', 0):,.0f}/month budget across the suggested categories
3. Simple exit rules: when it makes sense to review or take profits
4. One plain-English tax insight relevant to their situation

Choose category names ONLY from this fixed list (so the strategy, suggestions, and holdings stay consistent):
{", ".join(catalog.category_names())}

After the prose, append a fenced JSON block (```json ... ```) with the structured allocation in this exact shape, using the same categories you proposed (names must match the list above exactly):
{{"categories": [{{"name": "Core World ETF", "allocation_pct": 60}}, ...], "total_target_amount": <number in euros>}}
The allocation_pct values must sum to 100. The total_target_amount is the user's intended invested capital target (e.g. investable_surplus, or annual budget × years for a long horizon — pick a sensible number).
"""

    # Review gate: generate the plan, then verify the equity weight matches the
    # user's risk profile. If it doesn't, re-prompt with a correction (bounded by
    # MAX_PLAN_ALIGNMENT_RETRIES) so a "conservative" user can't silently get an
    # aggressive allocation. This runs inside the node — rather than as a graph
    # loop back into `plan` — so the confirmation messages appended below are only
    # ever produced once per turn.
    risk_profile = state.get("risk_profile", "balanced")
    correction = ""
    plan_text, strategy_data, align_warnings = "", None, []

    for attempt in range(MAX_PLAN_ALIGNMENT_RETRIES + 1):
        response = client.chat.completions.create(
            model=settings.AGENT_MODEL,
            messages=[
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user",   "content": context + correction},
            ],
            max_tokens=700,
        )
        raw = response.choices[0].message.content or ""
        plan_text, strategy_data = _split_plan_and_json(raw)

        align_warnings = validate_plan_alignment(strategy_data, risk_profile)
        if not align_warnings:
            break
        correction = (
            "\n\nIMPORTANT — your previous proposal did NOT match the user's stated "
            f"risk profile ('{risk_profile}'). Issues: " + " ".join(align_warnings)
            + " Re-balance the allocation so the equity weight fits this risk "
            "profile, then return the corrected prose and the JSON block."
        )
        logger.info("plan_node: re-planning for risk alignment (attempt %d)", attempt + 1)

    # If still misaligned after the retry, be transparent rather than silently
    # presenting a mismatched plan.
    if align_warnings:
        plan_text += (
            "\n\n_Note: this allocation may not fully match your "
            f"{risk_profile} risk profile ({' '.join(align_warnings)}) — "
            "let me know and I can rebalance it._"
        )

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

_QA_APPROVED_PROMPT = """You are InvestBuddy, a financial clarity assistant for expats in Germany.
The user has approved their investment strategy. Answer follow-up questions helpfully and concisely (under 200 words).

Rules:
- Reference specific parts of the strategy only when directly relevant — do NOT repeat the full strategy text
- Explain German tax terms in plain English first, then add the term in brackets
- Never say "you should buy/sell X" — say "it might be worth reviewing" or "one option is"
- This is educational — not personal investment advice."""

_QA_PENDING_PROMPT = """You are InvestBuddy, a financial clarity assistant for expats in Germany.
You have just proposed an investment strategy to the user. They may ask questions before deciding.
Answer their question concisely (under 150 words), then ask whether the plan looks good to them.

Rules:
- Reference specific parts of the strategy only when directly relevant — do NOT repeat the full strategy text
- Explain German tax terms in plain English first, then add the term in brackets
- Never say "you should buy/sell X"
- End every response with: "Does the plan look good to you, or would you like to change anything?"
- This is educational — not personal investment advice."""


def _last_user_message(messages) -> str:
    """Most recent user-authored message content, or "" if none."""
    return next(
        (_msg_content(m) for m in reversed(messages) if _msg_role(m) == "user"), ""
    )


# Explicit edit verbs/phrases. Used as a high-recall signal that the user wants
# the plan CHANGED (not just explained). Kept deliberately broad: silently
# dropping an adjustment — and then saving the stale, pre-adjustment plan when the
# user says "looks good" — is the worst failure mode of this flow.
_EDIT_KEYWORDS = (
    "adjust", "change", "revise", "rebalance", "rework", "redo", "update",
    "more conservative", "more aggressive", "less risk", "more risk",
    "increase", "decrease", "reduce", "lower", "raise", "bump", "shift",
    "more bonds", "less bonds", "more equity", "less equity", "more stock",
    "less stock", "swap", "replace", "remove", "drop the", "instead of",
    "make it", "set the", "different",
)


def _looks_like_edit(text: str) -> bool:
    """
    True when a message reads as an explicit request to CHANGE the plan.

    Question-shaped messages (ending in '?') are excluded so hypothetical
    "what if I had more bonds?" asks still route to Q&A rather than re-planning.
    Used to confirm/upgrade an adjustment — never to downgrade one.
    """
    t = text.lower().strip()
    if t.endswith("?"):
        return False
    return any(k in t for k in _EDIT_KEYWORDS)


def _classify_intent(last_user: str) -> str:
    """
    Classify a pending-approval message as approve / adjust / question via the LLM.

    The LLM verdict is primary. An explicit edit request (see `_looks_like_edit`)
    can only UPGRADE the result to "adjust" — we never override a confident
    "adjust" back into a question. That asymmetry is intentional: re-planning when
    the user merely asked a question is recoverable (they see a fresh plan and can
    say so), but dropping a real edit silently saves the wrong strategy.
    """
    intent_resp = client.chat.completions.create(
            model=settings.AGENT_MODEL,
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

    if intent not in ("approve", "adjust", "question"):
        intent = "question"
    # Honour explicit edits even when the LLM under-classifies them as a question.
    if intent == "question" and _looks_like_edit(last_user):
        intent = "adjust"
    return intent


def qa_node(state: AgentState) -> AgentState:
    """
    Router at the post-plan interrupt point. Classifies the latest user message and
    decides the path via `current_node`: "save" (approve), "adjust" (revise), or
    "answer" (everything else). Answer generation happens downstream in answer_node,
    after the retrieve/simulate tool nodes run — so this node never calls the answer
    LLM itself. Graph interrupts BEFORE this node each turn.
    """
    messages = list(state.get("messages", []))
    strategy_saved = state.get("strategy_saved", False)
    last_user = _last_user_message(messages)

    if not strategy_saved:
        intent = _classify_intent(last_user)
        logger.info("qa_node: pending-approval intent=%s", intent)

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
        return {**state, "current_node": "answer"}

    # Approved state: re-classify with the same LLM-backed logic as the
    # pending state so a genuine edit re-opens planning (and is re-saved on the
    # next approval), while plain questions/acknowledgements stay in Q&A.
    # Keyword matching alone was too brittle here — natural phrasings like
    # "I'd prefer mostly bonds now" slipped through to Q&A and the previously
    # saved strategy was never updated.
    intent = _classify_intent(last_user)
    logger.info("qa_node: approved-state intent=%s", intent)
    if intent == "adjust":
        return {
            **state,
            "current_node": "adjust",
            "messages": messages + [{"role": "assistant", "content":
                "Got it — let me revise the plan with your feedback..."
            }],
        }

    return {**state, "current_node": "answer"}


def route_after_qa(state: AgentState) -> str:
    """Map the router's decision (current_node) onto the next graph node."""
    node = state.get("current_node", "qa")
    if node == "save":
        return "approval"
    if node == "adjust":
        return "plan"
    return "answer"  # → retrieve → simulate → answer pipeline


# ── TOOL NODES (RAG + PROJECTION) ─────────────────────────────────────────────

def retrieve_node(state: AgentState) -> AgentState:
    """Tool node: pull trusted tax-source passages for the latest user message."""
    last_user = _last_user_message(list(state.get("messages", [])))
    ctx = retrieve_tax_context(last_user)
    return {**state, "retrieved_context": ctx}


def simulate_node(state: AgentState) -> AgentState:
    """Tool node: deterministic growth projection for how-much / projection asks."""
    last_user = _last_user_message(list(state.get("messages", [])))
    proj = compute_projection(last_user, dict(state))
    return {**state, "projection_context": proj}


# ── ANSWER NODE ───────────────────────────────────────────────────────────────

def answer_node(state: AgentState) -> AgentState:
    """
    LLM answer node. Generates the Q&A reply using the tax-source and projection
    context gathered by the retrieve/simulate tool nodes, in either pending-approval
    or approved mode. Loops back to the qa interrupt afterwards.
    """
    messages = list(state.get("messages", []))
    strategy_saved = state.get("strategy_saved", False)
    strategy = state.get("approved_strategy", {}).get("plan_text", "No strategy on file.")
    profile_ctx = (
        f"Risk profile: {state.get('risk_profile', 'unknown')} | "
        f"Monthly budget: €{state.get('monthly_investment_budget', 0):.0f} | "
        f"Tax bracket: {state.get('tax_bracket', 0.42) * 100:.0f}%"
    )

    # Context produced by the tool nodes keeps numbers exact and tax answers grounded.
    extra_context = (
        (state.get("retrieved_context") or "") + (state.get("projection_context") or "")
    )

    if not strategy_saved:
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

    logger.info("answer_node: generating reply (strategy_saved=%s)", strategy_saved)
    response = client.chat.completions.create(
        model=settings.AGENT_MODEL,
        messages=conversation,
        max_tokens=400,
    )
    answer = response.choices[0].message.content
    return {
        **state,
        "current_node": "qa",
        # Clear per-turn tool context so it never leaks into a later answer.
        "retrieved_context": "",
        "projection_context": "",
        "messages": messages + [{"role": "assistant", "content": answer}],
    }


# ── DIGEST NODE ───────────────────────────────────────────────────────────────

DIGEST_SYSTEM_PROMPT = """You are InvestBuddy generating a weekly investment digest.

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
    """LLM call — generates weekly plain-English digest."""
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
  Approved strategy: {state.get('approved_strategy', {}).get('plan_text', 'Not yet defined')[:200]}
"""

    # Ground the tax section in the curated sources when any are indexed.
    context += retrieve_tax_context(
        "German ETF capital gains tax, Vorabpauschale, Sparerpauschbetrag allowance"
    )

    response = client.chat.completions.create(
        model=settings.AGENT_MODEL,
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
