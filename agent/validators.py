"""
Suitability review gates for Kyron.

Two validators sit between raw LLM output and the user, enforcing the promises the
product makes about German tax efficiency and personalisation:

- ``validate_etf_suggestions`` — flags tickers that don't look UCITS / EU-domiciled.
  Non-UCITS funds (e.g. US-listed SPY, VTI) lose the 30% Teilfreistellung exemption
  and trigger §18 InvStG reporting for German residents, so recommending one would
  undermine the very tax optimisation the agent exists to provide. It *annotates*
  each suggestion with a ``warning`` instead of dropping it, so the user keeps full
  visibility but is told to verify.

- ``validate_plan_alignment`` — checks the LLM's proposed equity weight against the
  user's stated risk profile, so a "conservative" user can't silently receive an
  aggressive allocation just because the model ignored the intake context.

Both are ``@traceable`` so they appear as spans in LangSmith, consistent with the
tool functions in ``agent/tools.py``. They are pure and side-effect-light — the
ETF gate mutates the dicts it is handed (adding ``warning``) and returns them.
"""
import logging

from .observability import traceable

logger = logging.getLogger("agent.validators")


# ── ETF / UCITS DOMICILIATION GATE ────────────────────────────────────────────

# Exchanges whose listings are (near-always) EU/UCITS funds. Matched, upper-cased,
# against the `exchange` field the discover LLM returns for each suggestion.
VALID_EXCHANGES = {
    "XETRA", "GETTEX", "FWB", "FRANKFURT", "STUTTGART", "GER", "TRADEGATE",
    "EURONEXT", "EURONEXT AMSTERDAM", "EURONEXT PARIS", "AMS", "PAR",
    "LSE", "LONDON", "BORSA ITALIANA", "MIL", "SIX", "SWISS",
}

# Ticker suffixes for European venues (yfinance convention). A ticker ending in one
# of these is treated as an EU listing without needing a network lookup.
EU_SUFFIXES = (
    ".DE", ".AS", ".L", ".PA", ".MI", ".SW", ".VI", ".BR",
    ".LS", ".MC", ".F", ".STU", ".MU", ".DU", ".HM", ".BE", ".XC",
)

# US-listed funds with no UCITS wrapper — not available/suitable for German retail
# investors. Flagged hard if the LLM slips one through despite the system prompt.
KNOWN_NON_UCITS = {
    "SPY", "VOO", "VTI", "IVV", "QQQ", "VEA", "VWO", "VT",
    "SCHD", "VIG", "BND", "ARKK", "DIA", "IWM", "VUG", "VYM",
}

_NON_UCITS_WARNING = (
    "US-listed fund — not UCITS. German investors lose the 30% Teilfreistellung "
    "exemption and face additional §18 InvStG reporting. Look for the UCITS version "
    "(e.g. an iShares/Vanguard/Xtrackers ETF listed on XETRA) instead."
)
_UNVERIFIED_WARNING = (
    "Could not confirm a European/UCITS listing — verify the fund is UCITS and "
    "EU-domiciled before investing. German tax efficiency (Teilfreistellung) "
    "depends on it."
)


def _looks_european(ticker: str, exchange: str) -> bool:
    """True when the ticker suffix or LLM-provided exchange points to an EU venue."""
    if any((ticker or "").upper().endswith(suf) for suf in EU_SUFFIXES):
        return True
    return (exchange or "").upper() in VALID_EXCHANGES


@traceable(run_type="tool", name="validate_etf_suggestions")
def validate_etf_suggestions(suggestions: list[dict]) -> list[dict]:
    """
    Annotate each suggestion with a ``warning`` when it doesn't look UCITS / EU-listed.

    Never drops a suggestion — the user keeps full visibility, with a flag to verify.
    Suggestions are matched first against a hard blocklist of known US-listed funds,
    then against EU exchange/suffix heuristics. Mutates and returns the input list.
    """
    for s in suggestions:
        ticker = s.get("ticker", "")
        base = ticker.upper().split(".")[0]
        if base in KNOWN_NON_UCITS:
            s["warning"] = _NON_UCITS_WARNING
        elif not _looks_european(ticker, s.get("exchange", "")):
            s["warning"] = _UNVERIFIED_WARNING

    flagged = sum(1 for s in suggestions if s.get("warning"))
    if flagged:
        logger.info(
            "validate_etf_suggestions: flagged %d/%d suggestion(s) for UCITS review",
            flagged, len(suggestions),
        )
    return suggestions


# ── PLAN / RISK-PROFILE ALIGNMENT GATE ────────────────────────────────────────

# Acceptable equity weight band per risk profile: (min_equity_pct, max_equity_pct).
_RISK_EQUITY_BANDS = {
    "conservative": (0, 50),
    "balanced":     (30, 85),
    "growth":       (60, 100),
}

# Substrings in a category name that mark it as equity exposure.
_EQUITY_HINTS = (
    "equity", "world", "stock", "share", "s&p", "sp500", "msci", "nasdaq",
    "emerging", "developed", "all-world", "all world", "dividend", "growth",
)


def _equity_allocation_pct(plan_data: dict) -> float:
    """Sum of allocation_pct across categories whose name looks like equity exposure."""
    cats = (plan_data or {}).get("categories") or []
    return sum(
        c.get("allocation_pct", 0) or 0
        for c in cats
        if any(h in (c.get("name", "") or "").lower() for h in _EQUITY_HINTS)
    )


@traceable(run_type="tool", name="validate_plan_alignment")
def validate_plan_alignment(plan_data: dict, risk_profile: str) -> list[str]:
    """
    Return human-readable warnings when the proposed equity weight doesn't match the
    user's stated risk profile. An empty list means the plan is aligned.
    """
    if not plan_data:
        return []

    risk = (risk_profile or "balanced").lower()
    lo, hi = _RISK_EQUITY_BANDS.get(risk, _RISK_EQUITY_BANDS["balanced"])
    equity_pct = _equity_allocation_pct(plan_data)

    warnings: list[str] = []
    if equity_pct > hi:
        warnings.append(
            f"Equity allocation ({equity_pct:.0f}%) is high for a '{risk}' risk "
            f"profile (expected at most {hi}%)."
        )
    elif equity_pct < lo:
        warnings.append(
            f"Equity allocation ({equity_pct:.0f}%) is low for a '{risk}' risk "
            f"profile (expected at least {lo}%)."
        )

    if warnings:
        logger.info("validate_plan_alignment: %s", " ".join(warnings))
    return warnings
