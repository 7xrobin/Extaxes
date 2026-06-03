"""
Canonical type taxonomy for InvestBuddy — the single source of truth for the
*kinds* of things a user can hold, plan, or discover.

Three parts of the app used to invent their own vocabulary:
  - the Holding model's ``ASSET_TYPES`` choices,
  - the strategy (plan) node's free-form allocation categories,
  - the Discover suggestions' ``plan_category`` strings.

They now all read from here, so a "Core World ETF" bucket means the same thing
whether it came from the approved strategy, an AI suggestion, or a holding the
user added by hand. ``agent/tools.py`` exposes this catalog to the agent via the
``get_available_types`` tool ("API availability"), and the discover/plan prompts
are constrained to the names below.

Pure data + helpers, no Django or network imports — safe to use from models,
views, nodes, and validators alike.
"""

# ── ASSET TYPES (instrument kinds) ─────────────────────────────────────────────
# `value` MUST match the existing Holding.asset_type values in the DB — these are
# choices only, so changing the list here never requires a migration.
# `teilfreistellung` is the §20 InvStG partial-exemption fraction for that kind.
ASSET_TYPES = [
    {
        "value": "etf_acc",
        "label": "Accumulating ETF",
        "teilfreistellung": 0.30,
        "description": "Reinvests dividends internally (thesaurierend). 30% of "
                       "gains are tax-exempt (Teilfreistellung) — the most "
                       "tax-efficient wrapper for German residents.",
    },
    {
        "value": "etf_dist",
        "label": "Distributing ETF",
        "teilfreistellung": 0.30,
        "description": "Pays dividends out to you. Still gets the 30% equity "
                       "Teilfreistellung, but distributions are taxed in the year "
                       "they are paid.",
    },
    {
        "value": "stock",
        "label": "Individual Stock",
        "teilfreistellung": 0.00,
        "description": "A single company share. No Teilfreistellung — gains are "
                       "taxed at the full ~26.375% Abgeltungsteuer rate.",
    },
    {
        "value": "savings",
        "label": "Savings / Cash",
        "teilfreistellung": 0.00,
        "description": "Cash, savings accounts, money-market parking. Interest is "
                       "taxed as capital income; no fund exemption applies.",
    },
]


# ── CATEGORIES (allocation / plan buckets) ─────────────────────────────────────
# `equity` drives the risk-alignment check in agent/validators.py.
# `default_asset_type` is the instrument kind a suggestion in this bucket usually
# takes, so the UI and prompts can default sensibly.
CATEGORIES = [
    {
        "name": "Core World ETF", "slug": "core-world", "equity": True,
        "default_asset_type": "etf_acc",
        "description": "Broad global developed+emerging equity (e.g. FTSE All-World, "
                       "MSCI ACWI) — the diversified core of most portfolios.",
    },
    {
        "name": "US / S&P 500", "slug": "us-sp500", "equity": True,
        "default_asset_type": "etf_acc",
        "description": "US large-cap exposure via UCITS S&P 500 / total-US-market ETFs.",
    },
    {
        "name": "Emerging Markets", "slug": "emerging-markets", "equity": True,
        "default_asset_type": "etf_acc",
        "description": "Higher-growth, higher-volatility developing-economy equity.",
    },
    {
        "name": "Developed ex-US", "slug": "developed-ex-us", "equity": True,
        "default_asset_type": "etf_acc",
        "description": "Europe, Japan and other developed markets outside the US.",
    },
    {
        "name": "Bonds / Stability", "slug": "bonds-stability", "equity": False,
        "default_asset_type": "etf_dist",
        "description": "Government/corporate bond ETFs to dampen volatility. No "
                       "Teilfreistellung (taxed at the full rate).",
    },
    {
        "name": "Dividend / Income", "slug": "dividend-income", "equity": True,
        "default_asset_type": "etf_dist",
        "description": "Dividend-focused equity ETFs for a regular income stream.",
    },
    {
        "name": "Thematic / Satellite", "slug": "thematic-satellite", "equity": True,
        "default_asset_type": "etf_acc",
        "description": "Targeted bets (tech, clean energy, sustainability, AI) layered "
                       "as a small satellite around the core.",
    },
    {
        "name": "Individual Stocks", "slug": "individual-stocks", "equity": True,
        "default_asset_type": "stock",
        "description": "Single companies. No fund exemption; full tax rate on gains.",
    },
    {
        "name": "Cash / Savings", "slug": "cash-savings", "equity": False,
        "default_asset_type": "savings",
        "description": "Emergency-fund and short-horizon money kept out of the market.",
    },
]


# ── HELPERS ────────────────────────────────────────────────────────────────────

def asset_type_choices() -> list[tuple[str, str]]:
    """Django model `choices` form: [(value, label), ...]."""
    return [(t["value"], t["label"]) for t in ASSET_TYPES]


def asset_type_labels() -> dict[str, str]:
    """{value: label} for templates/prompts."""
    return {t["value"]: t["label"] for t in ASSET_TYPES}


def category_names() -> list[str]:
    """Ordered list of canonical category names (for prompt constraints + UI)."""
    return [c["name"] for c in CATEGORIES]


_EQUITY_BY_NAME = {c["name"].lower(): c["equity"] for c in CATEGORIES}


def is_equity_category(name: str) -> bool | None:
    """
    True/False if `name` is a known catalog category, else None so callers can
    fall back to a heuristic for off-catalog (LLM-invented) names.
    """
    return _EQUITY_BY_NAME.get((name or "").strip().lower())


def teilfreistellung_pct(asset_type: str) -> float:
    """§20 InvStG partial-exemption fraction for an asset type (0.0 if unknown)."""
    for t in ASSET_TYPES:
        if t["value"] == asset_type:
            return t["teilfreistellung"]
    return 0.0
