"""
Investment growth simulation — pure deterministic math, no LLM.

All projections compound monthly. Net-of-tax figures reuse the German tax engine
(agent/tax_engine.py) so the take-home estimate matches the rest of the app.
These are educational estimates, not financial advice.
"""
from .tax_engine import effective_rate, tax_on_exit

# Historical long-run average for a broad world equity ETF (nominal, pre-tax).
DEFAULT_ANNUAL_RETURN_PCT = 7.0


def _monthly_rate(annual_return_pct: float) -> float:
    """Convert an annual return % into an equivalent monthly compounding rate."""
    return (1.0 + annual_return_pct / 100.0) ** (1.0 / 12.0) - 1.0


def project_growth(
    start_amount: float,
    monthly_contribution: float,
    annual_return_pct: float = DEFAULT_ANNUAL_RETURN_PCT,
    years: int = 10,
    asset_type: str = "etf_acc",
) -> dict:
    """
    Project portfolio value with monthly contributions and monthly compounding.

    Returns:
        {
          "series": [{"year", "value", "contributed", "gain"}, ...]  # incl. year 0,
          "final_gross", "total_contributed", "total_gain",
          "tax_on_gain", "final_net",
          "inputs": {...},
        }
    Contributions are added at the start of each month, then the balance grows.
    Net figures apply the effective capital-gains rate (after Teilfreistellung)
    to the total gain, as if the position were fully sold at the horizon.
    """
    start_amount = max(0.0, float(start_amount))
    monthly_contribution = max(0.0, float(monthly_contribution))
    years = max(0, int(years))
    r = _monthly_rate(annual_return_pct)

    value = start_amount
    contributed = start_amount
    series = [{
        "year": 0,
        "value": round(value, 2),
        "contributed": round(contributed, 2),
        "gain": 0.0,
    }]

    for year in range(1, years + 1):
        for _ in range(12):
            value += monthly_contribution
            contributed += monthly_contribution
            value *= (1.0 + r)
        series.append({
            "year": year,
            "value": round(value, 2),
            "contributed": round(contributed, 2),
            "gain": round(value - contributed, 2),
        })

    total_gain = value - contributed
    tax = tax_on_exit(total_gain, asset_type)
    return {
        "series": series,
        "final_gross": round(value, 2),
        "total_contributed": round(contributed, 2),
        "total_gain": round(total_gain, 2),
        "tax_on_gain": round(tax, 2),
        "final_net": round(value - tax, 2),
        "effective_rate_pct": round(effective_rate(asset_type) * 100, 2),
        "inputs": {
            "start_amount": round(start_amount, 2),
            "monthly_contribution": round(monthly_contribution, 2),
            "annual_return_pct": annual_return_pct,
            "years": years,
            "asset_type": asset_type,
        },
    }


def required_monthly_for_target(
    target_amount: float,
    years: int,
    annual_return_pct: float = DEFAULT_ANNUAL_RETURN_PCT,
    start_amount: float = 0.0,
) -> float:
    """
    Closed-form: monthly contribution needed to reach target_amount in `years`,
    starting from `start_amount`, with monthly compounding.

    Mirrors project_growth, which contributes at the START of each month
    (annuity-due), so:
        FV = start*(1+r)^n + C * [((1+r)^n - 1) / r] * (1+r)
    Solve for C. Returns 0 if the target is already met by growth alone.
    """
    target_amount = float(target_amount)
    years = max(0, int(years))
    n = years * 12
    r = _monthly_rate(annual_return_pct)

    if n == 0:
        return 0.0

    grown_start = start_amount * ((1.0 + r) ** n)
    if r == 0:
        needed = (target_amount - grown_start) / n
    else:
        annuity_factor = ((1.0 + r) ** n - 1.0) / r * (1.0 + r)  # annuity-due
        needed = (target_amount - grown_start) / annuity_factor
    return round(max(0.0, needed), 2)


def recommend_investment(investable_surplus: float, monthly_budget: float) -> dict:
    """
    Plain 'how much to invest' summary derived from the user's own numbers.
    Suggests deploying the investable surplus (savings above the emergency fund)
    plus the recurring monthly budget — the levers Kyron already tracks.
    """
    investable_surplus = max(0.0, float(investable_surplus))
    monthly_budget = max(0.0, float(monthly_budget))
    return {
        "lump_sum_available": round(investable_surplus, 2),
        "monthly_contribution": round(monthly_budget, 2),
        "first_year_total": round(investable_surplus + monthly_budget * 12, 2),
    }
