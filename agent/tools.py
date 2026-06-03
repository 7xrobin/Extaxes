"""
Agent tools.

Each function here is a discrete, side-effect-light capability the agent can call:
live prices, trusted tax-source retrieval (RAG), and deterministic growth
projections. They are decorated with ``@traceable`` so every invocation shows up
as its own span in LangSmith, and they are wrapped by thin graph nodes
(agent/nodes.py) so they also appear as explicit nodes on the LangGraph.

Keeping the tool logic here — separate from the node wiring — means the same tool
can be reused outside the graph (e.g. the digest pipeline) without dragging graph
state along.
"""
import re
import logging

from . import catalog
from .observability import traceable
from .price_service import get_prices
from .simulation import (
    DEFAULT_ANNUAL_RETURN_PCT, project_growth,
    recommend_investment, required_monthly_for_target,
)

logger = logging.getLogger("agent.tools")


# ── PRICE TOOL ────────────────────────────────────────────────────────────────

@traceable(run_type="tool", name="fetch_prices")
def fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch live prices for a list of tickers. Returns {ticker: price}, 0.0 on miss."""
    if not tickers:
        return {}
    prices = get_prices(tickers)
    missing = [t for t in tickers if not prices.get(t)]
    if missing:
        logger.warning("fetch_prices: no price for %s", ", ".join(missing))
    logger.info("fetch_prices: resolved %d/%d tickers", len(tickers) - len(missing), len(tickers))
    return prices


# ── TYPE CATALOG TOOL ─────────────────────────────────────────────────────────

@traceable(run_type="tool", name="get_available_types")
def get_available_types() -> dict:
    """
    Return the available instrument types and allocation categories the rest of the
    app supports ("API availability"). The strategy, Discover suggestions, and
    Holdings are all constrained to this shared vocabulary so a category means the
    same thing everywhere. Sourced from the canonical catalog in agent/catalog.py.
    """
    result = {
        "asset_types": catalog.ASSET_TYPES,
        "categories": catalog.CATEGORIES,
    }
    logger.info(
        "get_available_types: %d asset types, %d categories",
        len(result["asset_types"]), len(result["categories"]),
    )
    return result


# ── TAX RAG TOOL ──────────────────────────────────────────────────────────────

@traceable(run_type="retriever", name="retrieve_tax_context")
def retrieve_tax_context(query: str) -> str:
    """
    Retrieve trusted tax-source passages for `query`. Import is guarded so the
    graph keeps working when the rag app has no indexed sources (or none match).
    Returns a prompt-ready, source-cited block, or "" when nothing relevant.
    """
    try:
        from rag.retriever import build_context
        ctx = build_context(query)
    except Exception:
        logger.warning("retrieve_tax_context: retrieval failed for %r", query, exc_info=True)
        return ""
    if not ctx:
        logger.debug("retrieve_tax_context: no relevant sources for %r", query)
        return ""
    logger.info("retrieve_tax_context: %d chars of grounded context", len(ctx))
    return (
        "\n\nTRUSTED TAX SOURCES — treat these as the PRIMARY source of truth for any "
        "tax question. Prefer them over your own assumptions, and cite the source URL "
        "in your answer when you use them:\n" + ctx
    )


# ── PROJECTION / SIMULATION TOOL ──────────────────────────────────────────────

_PROJECTION_KEYWORDS = (
    "how much", "simulate", "simulation", "project", "projection", "grow to",
    "worth in", "in 10 years", "in 5 years", "in 20 years", "future value",
    "compound", "to reach", "to hit", "per month to", "each month to",
)


@traceable(run_type="tool", name="compute_projection")
def compute_projection(query: str, state: dict) -> str:
    """
    If the user is asking a how-much / projection question, compute the figures
    deterministically (the LLM must not do the math) and return a prompt block
    with exact numbers. Returns "" when the question isn't projection-related.
    """
    # Lazy import avoids a circular dependency (nodes imports tools at module load).
    from .nodes import _extract_number

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

    logger.info("compute_projection: %d-year projection computed", years)
    return "\n".join(lines)
