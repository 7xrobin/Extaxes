from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.template.loader import render_to_string
import pandas as pd
import io
import json
from datetime import date
import yfinance as yf
from openai import OpenAI
from .models import UserProfile, Holding, ExitRule
from agent.tax_engine import (
    tax_on_exit, vorabpauschale, effective_rate, sparerpauschbetrag_limit,
    teilfreistellung_pct, teilfreistellung_note,
)
from agent.price_service import (
    get_price, get_prices, get_period_start_prices, get_period_returns, PERIOD_MAP,
)
from agent.simulation import project_growth, DEFAULT_ANNUAL_RETURN_PCT
from agent.validators import validate_etf_suggestions
from agent import catalog
from django.template.defaultfilters import slugify


PERIOD_LABELS = {"1M": "1 month", "3M": "3 months", "6M": "6 months", "1Y": "1 year", "YTD": "YTD"}

# Order periods are offered in the Discover dropdown (shortest → longest, YTD last).
DISCOVER_PERIODS = ["1M", "3M", "6M", "YTD", "1Y"]


_CATEGORY_COLORS = ['#4f8ef7', '#3ecf8e', '#f87171', '#f7c948', '#a78bfa', '#fb923c']


def _compute_category_coverage(strategy_data, holdings):
    if not strategy_data:
        return []
    cats = strategy_data.get('categories') or []
    target_total = strategy_data.get('total_target_amount') or 0

    holdings_by_cat = {}
    for h in holdings:
        if h.plan_category:
            holdings_by_cat.setdefault(h.plan_category, []).append(h)

    result = []
    for i, cat in enumerate(cats):
        cat_holdings = holdings_by_cat.get(cat['name'], [])
        cat_value = sum(h.current_value for h in cat_holdings)
        cat_target = cat['allocation_pct'] / 100 * target_total if target_total else 0
        fill_pct = min(100, cat_value / cat_target * 100) if cat_target > 0 else 0
        result.append({
            'name': cat['name'],
            'allocation_pct': cat['allocation_pct'],
            'fill_pct': round(fill_pct, 1),
            'cat_value': cat_value,
            'cat_target': cat_target,
            'holdings_count': len(cat_holdings),
            'color': _CATEGORY_COLORS[i % len(_CATEGORY_COLORS)],
        })
    return result


def _get_or_create_profile(user_id: str):
    profile, _ = UserProfile.objects.get_or_create(user_id=user_id)
    return profile


def _refresh_prices(holdings):
    """Fetch live prices and recompute all-time gain on each holding (persisted)."""
    tickers = [h.ticker for h in holdings]
    prices = get_prices(tickers) if tickers else {}
    for h in holdings:
        price = prices.get(h.ticker, h.current_price)
        cost = h.units * h.avg_purchase_price
        h.current_price = price
        h.current_value = h.units * price
        h.unrealised_gain = h.current_value - cost
        h.unrealised_gain_pct = (h.unrealised_gain / cost * 100) if cost > 0 else 0
        h.save(update_fields=[
            "current_price", "current_value", "unrealised_gain", "unrealised_gain_pct",
        ])


def _humanize_since(d):
    """A compact 'how long ago' string for a date, e.g. '2y 3m', '5m', '12d'."""
    if not d:
        return None
    days = max(0, (date.today() - d).days)
    years, rem = divmod(days, 365)
    months = rem // 30
    if years:
        return f"{years}y {months}m" if months else f"{years}y"
    if months:
        return f"{months}m"
    return f"{days}d"


def _earliest_purchase(holdings):
    """Oldest purchase_date across holdings (the portfolio's holding age), or None."""
    dates = [h.purchase_date for h in holdings if h.purchase_date]
    return min(dates) if dates else None


def _weighted_annual_return(holdings):
    """
    Value-weighted trailing 12-month return (%) across the holdings — the blended
    'average annual gain' of the ETFs/stocks actually owned. Falls back to the
    default assumption when no holding has usable 1Y price history.
    """
    if not holdings:
        return DEFAULT_ANNUAL_RETURN_PCT
    start_prices = get_period_start_prices([h.ticker for h in holdings], "1Y")
    weighted = base = 0.0
    for h in holdings:
        sp = start_prices.get(h.ticker, 0.0)
        if sp > 0 and h.current_price and h.current_value > 0:
            ret = (h.current_price - sp) / sp * 100.0
            weighted += h.current_value * ret
            base += h.current_value
    return round(weighted / base, 2) if base > 0 else DEFAULT_ANNUAL_RETURN_PCT


def _totals_dict(holdings, rng="ALL", start_prices=None):
    """
    Build the header stats for portfolio/totals.html for the given time range.
    'ALL' compares against cost basis (all-time); a period compares against the
    price at the start of that window (using start_prices, fetched if not given).
    """
    rng = (rng or "ALL").upper()
    total_value = sum(h.current_value for h in holdings)
    total_invested = sum(h.units * h.avg_purchase_price for h in holdings)
    holding_since = _earliest_purchase(holdings)
    holding_for = _humanize_since(holding_since)

    if rng == "ALL" or rng not in PERIOD_MAP:
        total_gain = total_value - total_invested
        return {
            "total_value": total_value,
            "secondary_label": "Invested",
            "secondary_value": total_invested,
            "gain_label": "Gain (All time)",
            "gain_value": total_gain,
            "gain_pct": (total_gain / total_invested * 100) if total_invested > 0 else 0,
            "holding_since": holding_since,
            "holding_for": holding_for,
        }

    if start_prices is None:
        start_prices = get_period_start_prices([h.ticker for h in holdings], rng)
    tracked_current = period_start_total = 0.0
    for h in holdings:
        sp = start_prices.get(h.ticker, 0.0)
        if sp:
            tracked_current += h.current_value
            period_start_total += h.units * sp
    period_gain = tracked_current - period_start_total
    label = PERIOD_LABELS.get(rng, rng)
    return {
        "total_value": total_value,
        "secondary_label": f"Value {label} ago",
        "secondary_value": period_start_total,
        "gain_label": f"Gain (Past {label})",
        "gain_value": period_gain,
        "gain_pct": (period_gain / period_start_total * 100) if period_start_total > 0 else 0,
        "holding_since": holding_since,
        "holding_for": holding_for,
    }


@login_required
def overview(request):
    profile = _get_or_create_profile(str(request.user.id))
    holdings = list(profile.holdings.select_related("exit_rule").all())

    _refresh_prices(holdings)

    total_invested = sum(h.units * h.avg_purchase_price for h in holdings)
    total_value    = sum(h.current_value for h in holdings)
    total_gain     = total_value - total_invested
    gain_pct       = (total_gain / total_invested * 100) if total_invested > 0 else 0
    totals         = _totals_dict(holdings, "ALL")

    strategy_data = profile.approved_strategy_data or None
    target_amount = (strategy_data or {}).get("total_target_amount") if strategy_data else None
    coverage_pct  = None
    if target_amount and target_amount > 0:
        coverage_pct = min(100, total_value / target_amount * 100)

    category_coverage = _compute_category_coverage(strategy_data, holdings)

    _hcd: dict[str, float] = {}
    for h in holdings:
        label = h.plan_category or h.get_asset_type_display()
        _hcd[label] = _hcd.get(label, 0.0) + (h.current_value or 0.0)
    holdings_chart_data = [
        {"label": k, "value": round(v)}
        for k, v in sorted(_hcd.items(), key=lambda x: -x[1])
        if v > 0
    ]

    return render(request, "portfolio/overview.html", {
        "profile":             profile,
        "holdings":            holdings,
        "total_invested":      total_invested,
        "total_value":         total_value,
        "total_gain":          total_gain,
        "gain_pct":            gain_pct,
        "totals":              totals,
        "strategy_data":       strategy_data,
        "target_amount":       target_amount,
        "coverage_pct":        coverage_pct,
        "category_coverage":   category_coverage,
        "holdings_chart_data": holdings_chart_data,
        # Growth Simulation tab defaults — return is the blended trailing 1Y
        # gain of the holdings themselves (falls back to the long-run default).
        "sim_start_amount":    round(total_value),
        "sim_monthly":         round(profile.monthly_investment_budget),
        "sim_years":           10,
        "sim_return_pct":      _weighted_annual_return(holdings),
        # Discover attribute filters — sourced from the shared type catalog.
        "discover_categories":  catalog.category_names(),
        "discover_asset_types": catalog.ASSET_TYPES,
    })


@login_required
def upload_page(request):
    return render(request, "portfolio/upload.html")


@login_required
@require_POST
def upload_csv(request):
    """Parse CSV export."""
    profile = _get_or_create_profile(str(request.user.id))
    csv_file = request.FILES.get("csv_file")
    if not csv_file:
        return HttpResponse("No file uploaded", status=400)

    try:
        df = pd.read_csv(io.StringIO(csv_file.read().decode("utf-8")))
        required = {"ticker", "units", "avg_purchase_price"}
        if not required.issubset(df.columns):
            return HttpResponse("CSV missing required columns: ticker, units, avg_purchase_price", status=400)
        for _, row in df.iterrows():
            ticker = str(row.get("ticker", "")).strip()
            if not ticker:
                continue
            h, created = Holding.objects.update_or_create(
                profile=profile,
                ticker=ticker,
                defaults={
                    "isin":               str(row.get("isin", "")).strip(),
                    "asset_type":         str(row.get("asset_type", "etf_acc")).strip(),
                    "units":              float(row.get("units", 0)),
                    "avg_purchase_price": float(row.get("avg_purchase_price", 0)),
                },
            )
            if h.purchase_date is None:
                h.purchase_date = date.today()
                h.save(update_fields=["purchase_date"])
            ExitRule.objects.get_or_create(holding=h)
    except Exception as e:
        return HttpResponse(f"Error parsing CSV: {e}", status=400)

    return redirect("/portfolio/")


@login_required
@require_POST
def add_manual(request):
    """Add a single holding manually via form."""
    profile = _get_or_create_profile(str(request.user.id))
    h = Holding.objects.create(
        profile            = profile,
        ticker             = request.POST["ticker"].strip().upper(),
        isin               = request.POST.get("isin", "").strip(),
        asset_type         = request.POST["asset_type"],
        units              = float(request.POST["units"]),
        avg_purchase_price = float(request.POST["avg_purchase_price"]),
        purchase_date      = date.today(),
    )
    ExitRule.objects.create(holding=h)
    return redirect("/portfolio/")


@login_required
def holdings_partial(request):
    """
    HTMX partial for the holdings table. Shows each holding's all-time gain/loss
    versus its average purchase price, and OOB-swaps the header totals to match.
    """
    profile  = _get_or_create_profile(str(request.user.id))
    holdings = list(profile.holdings.select_related("exit_rule").all())

    _refresh_prices(holdings)

    totals = _totals_dict(holdings, "ALL")
    totals_html = render_to_string("portfolio/totals.html", {"totals": totals})
    table_html = render_to_string("portfolio/holdings.html", {"holdings": holdings})
    oob = f'<div id="portfolio-totals" hx-swap-oob="true">{totals_html}</div>'
    return HttpResponse(table_html + oob)


@login_required
@require_POST
def simulate_partial(request):
    """HTMX partial — projected growth from start amount + monthly contributions."""
    def _f(name, default):
        try:
            return float(request.POST.get(name, default) or default)
        except (TypeError, ValueError):
            return float(default)

    start_amount = _f("start_amount", 0)
    monthly      = _f("monthly_contribution", 0)
    return_pct   = _f("annual_return_pct", DEFAULT_ANNUAL_RETURN_PCT)
    years        = int(_f("years", 10))
    years        = max(1, min(years, 50))
    asset_type   = request.POST.get("asset_type", "etf_acc")

    proj = project_growth(
        start_amount=start_amount,
        monthly_contribution=monthly,
        annual_return_pct=return_pct,
        years=years,
        asset_type=asset_type,
    )
    return render(request, "portfolio/simulation_partial.html", {"proj": proj})


@login_required
def tax_partial(request):
    """HTMX partial for the tax summary panel."""
    profile  = _get_or_create_profile(str(request.user.id))
    holdings = profile.holdings.all()
    allowance = sparerpauschbetrag_limit(profile.is_married)
    tax_rows  = []
    total_vp  = 0.0

    for h in holdings:
        vp  = vorabpauschale(h.current_value, h.asset_type)
        tax = tax_on_exit(h.unrealised_gain, h.asset_type)
        total_vp += vp
        tax_rows.append({
            "holding":        h,
            "vorabpauschale": vp,
            "tax_if_sold":    tax,
            "rate":           effective_rate(h.asset_type) * 100,
            "teilfreistellung_pct": round(teilfreistellung_pct(h.asset_type) * 100),
            "tax_note":       teilfreistellung_note(h.asset_type),
        })

    return render(request, "portfolio/tax_summary.html", {
        "tax_rows":  tax_rows,
        "total_vp":  total_vp,
        "allowance": allowance,
    })


# ── DISCOVER ──────────────────────────────────────────────────────────────────

DISCOVER_SYSTEM_PROMPT = """You are InvestBuddy, a financial assistant for expats in Germany.
Suggest 6 specific ETFs or stocks available on European exchanges (XETRA .DE or Euronext .AS preferred).
Include both European-domiciled ETFs AND UCITS-compliant versions of American ETFs (e.g. iShares S&P 500 UCITS SXR8.DE, Xtrackers Nasdaq 100 XNAS.DE, Amundi MSCI USA) — NOT the US-listed versions (SPY, QQQ, VTI are not valid here).
Focus on instruments accessible via German brokers (Trade Republic, Scalable Capital, ING).

For each suggestion return a JSON object with EXACTLY these fields:
{
  "ticker": "VWCE.DE",
  "name": "Vanguard FTSE All-World UCITS ETF",
  "asset_type": "etf_acc",
  "exchange": "XETRA",
  "plan_category": "Core World ETF",
  "allocation_pct": 60,
  "rationale": "One sentence why this fits the user."
}

asset_type must be one of: etf_acc, etf_dist, stock
plan_category MUST be chosen from this fixed list (so suggestions line up with the user's strategy and holdings):
{CATEGORY_LIST}

Rules:
- Prefer accumulating ETFs (etf_acc) for German tax efficiency (Teilfreistellung)
- Include a mix: global/world ETFs, US/S&P 500 UCITS ETFs (e.g. SXR8.DE, XNAS.DE, VUSA.AS), emerging markets, bonds/stability, thematic or satellite positions, individual stocks where appropriate
- allocation_pct values do NOT need to sum to 100 — they represent the suggested budget share for each plan category
- Never recommend leveraged or derivative products
- Return ONLY valid JSON: {"suggestions": [...]}
- This is educational — not personal investment advice.
"""


def _filter_instructions(filters):
    """Turn the Discover filter form values into plain LLM steering lines."""
    if not filters:
        return ""
    lines = []
    cat = (filters.get("category") or "").strip()
    if cat and cat in catalog.category_names():
        lines.append(f"- Only suggest instruments in the '{cat}' category.")
    atype = (filters.get("asset_type") or "").strip()
    labels = catalog.asset_type_labels()
    if atype in labels:
        lines.append(f"- Only suggest instruments of type {labels[atype]} ({atype}).")
    if (filters.get("tax_efficient") or "").strip() in ("1", "true", "on", "yes"):
        lines.append(
            "- Only suggest tax-efficient funds that qualify for the 30% equity "
            "Teilfreistellung (accumulating or distributing equity ETFs)."
        )
    theme = (filters.get("theme") or "").strip()
    if theme:
        lines.append(f"- Focus on this theme/preference: {theme}.")
    if not lines:
        return ""
    return "\n\nUser filters (honour these strictly):\n" + "\n".join(lines)


def _generate_suggestions(profile, goals, filters=None):
    from django.conf import settings
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    goals_text = ", ".join(g.name for g in goals) if goals else "general wealth growth"
    system_prompt = DISCOVER_SYSTEM_PROMPT.replace(
        "{CATEGORY_LIST}", ", ".join(catalog.category_names())
    )
    user_context = f"""
User profile:
- Risk profile: {profile.risk_profile}
- Monthly investment budget: €{profile.monthly_investment_budget:.0f}
- Tax bracket: {profile.tax_bracket * 100:.0f}%
- Married (double Sparerpauschbetrag): {profile.is_married}
- Goals: {goals_text}
{_filter_instructions(filters)}

Return a JSON object with key "suggestions" containing an array of exactly 6 suggestion objects.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_context},
        ],
        max_tokens=1200,
    )
    data = json.loads(response.choices[0].message.content)
    return data.get("suggestions", [])


@login_required
@require_POST
def suggestions_partial(request):
    """HTMX partial — AI-generated ETF/stock suggestions."""
    profile = _get_or_create_profile(str(request.user.id))
    goals = list(profile.goals.all())
    filters = {
        "category":      request.POST.get("category", ""),
        "asset_type":    request.POST.get("asset_type", ""),
        "tax_efficient": request.POST.get("tax_efficient", ""),
        "theme":         request.POST.get("theme", ""),
    }
    suggestions = _generate_suggestions(profile, goals, filters)
    # Review gate: flag any ticker that doesn't look UCITS / EU-domiciled before the
    # user ever sees it — recommending a US-listed fund to a German resident loses
    # the Teilfreistellung exemption and adds §18 InvStG reporting.
    validate_etf_suggestions(suggestions)
    tickers = [s["ticker"] for s in suggestions]
    prices = get_prices(tickers)
    for s in suggestions:
        s["current_price"] = prices.get(s["ticker"], 0.0)
        s["teilfreistellung_pct"] = round(teilfreistellung_pct(s.get("asset_type", "")) * 100)
        s["tax_note"] = teilfreistellung_note(s.get("asset_type", ""))
        returns = get_period_returns(s["ticker"])
        s["period_perf"] = [
            {"code": code, "label": PERIOD_LABELS.get(code, code), "gain": returns.get(code, 0.0)}
            for code in DISCOVER_PERIODS
        ]
    buckets = _bucket_suggestions(suggestions)
    return render(request, "portfolio/suggestions_partial.html", {
        "suggestions": suggestions,  # kept flat for the alloc-chart JSON script
        "buckets": buckets,          # grouped by plan_category for the bucket tabs
    })


def _bucket_suggestions(suggestions):
    """
    Group suggestions by their plan_category (allocation bucket), preserving first-seen
    order, so the UI can present one tab per bucket (e.g. Global Developed Markets /
    Emerging Markets / Other). Returns a list of {name, slug, items} dicts.
    """
    by_cat: dict[str, list] = {}
    order: list[str] = []
    for s in suggestions:
        cat = (s.get("plan_category") or "Other").strip() or "Other"
        if cat not in by_cat:
            by_cat[cat] = []
            order.append(cat)
        by_cat[cat].append(s)
    return [{"name": cat, "slug": slugify(cat), "items": by_cat[cat]} for cat in order]


@login_required
@require_POST
def quick_add(request):
    """HTMX — add a suggestion directly as a holding from the suggestions panel."""
    profile = _get_or_create_profile(str(request.user.id))
    ticker = request.POST.get('ticker', '').strip().upper()
    asset_type = request.POST.get('asset_type', 'etf_acc')
    plan_category = request.POST.get('plan_category', '')
    current_price_str = request.POST.get('current_price', '0') or '0'
    try:
        units = float(request.POST.get('units', 0) or 0)
        avg_price = float(request.POST.get('avg_purchase_price', 0) or 0)
        current_price = float(current_price_str)
    except ValueError:
        return HttpResponse('<p class="add-error">Invalid units or price.</p>', status=400)

    if not ticker or units <= 0 or avg_price <= 0:
        return HttpResponse('<p class="add-error">Enter valid units and average price.</p>', status=400)

    h, created = Holding.objects.get_or_create(
        profile=profile,
        ticker=ticker,
        defaults={
            'asset_type':         asset_type,
            'units':              units,
            'avg_purchase_price': avg_price,
            'current_price':      current_price,
            'current_value':      units * current_price,
            'plan_category':      plan_category,
            'purchase_date':      date.today(),
        },
    )
    if not created:
        h.units += units
        h.plan_category = plan_category or h.plan_category
        h.save(update_fields=['units', 'plan_category'])
    ExitRule.objects.get_or_create(holding=h)

    # Recompute category bars for OOB swap
    holdings = list(profile.holdings.all())
    strategy_data = profile.approved_strategy_data
    category_coverage = _compute_category_coverage(strategy_data, holdings)
    bars_html = render_to_string(
        'portfolio/category_bars.html',
        {'category_coverage': category_coverage},
    )

    response = f'<div class="add-done">✓ Added {ticker} to Holdings</div>'
    oob = f'<div id="category-bars" hx-swap-oob="true">{bars_html}</div>'
    return HttpResponse(response + oob)


_AI_REVIEW_SYSTEM = """You are InvestBuddy, a financial assistant for expats in Germany.
Provide a concise, educational review of the user's investment portfolio.
Focus on: diversification, German tax efficiency (Teilfreistellung, Vorabpauschale), risk alignment, and 1-2 actionable improvements.
Keep the total response under 200 words. Use plain text, no markdown headers. Not personal investment advice."""


@login_required
@require_POST
def ai_review_partial(request):
    """HTMX partial — AI review of the invested portfolio."""
    from django.conf import settings
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    profile = _get_or_create_profile(str(request.user.id))
    holdings = list(profile.holdings.all())

    if not holdings:
        return HttpResponse('<p style="color:var(--muted);font-size:0.85rem;">No holdings to review.</p>')

    holdings_lines = "\n".join(
        f"- {h.ticker} ({h.get_asset_type_display()}): {h.units} units @ €{h.avg_purchase_price:.2f} avg, "
        f"current value €{h.current_value:.0f}, gain {h.unrealised_gain_pct:.1f}%"
        for h in holdings
    )
    strategy_context = (
        f"\n\nApproved strategy:\n{profile.approved_strategy_text}"
        if profile.approved_strategy_text else ""
    )
    user_prompt = (
        f"Risk profile: {profile.risk_profile}\n"
        f"Monthly budget: €{profile.monthly_investment_budget:.0f}\n"
        f"Tax bracket: {profile.tax_bracket * 100:.0f}%\n\n"
        f"Holdings:\n{holdings_lines}"
        f"{strategy_context}"
    )

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _AI_REVIEW_SYSTEM},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=350,
    )
    review_text = response.choices[0].message.content
    return HttpResponse(f'<div class="ai-review-text">{review_text}</div>')


@login_required
@require_POST
def clear_portfolio(request):
    profile = _get_or_create_profile(str(request.user.id))
    profile.holdings.all().delete()
    return redirect("/portfolio/")


@login_required
@require_POST
def clear_strategy(request):
    profile = _get_or_create_profile(str(request.user.id))
    profile.strategy_approved = False
    profile.approved_strategy_text = ""
    profile.approved_strategy_data = None
    profile.strategy_approved_at = None
    profile.save(update_fields=[
        "strategy_approved", "approved_strategy_text",
        "approved_strategy_data", "strategy_approved_at",
    ])
    return redirect("/portfolio/")


@login_required
def search_partial(request):
    """HTMX partial — look up a single ticker via yfinance."""
    ticker = request.GET.get("ticker", "").strip().upper()
    result = error = None
    if ticker:
        try:
            info = yf.Ticker(ticker).info
            if not info.get("longName") and not info.get("shortName"):
                error = f"No data found for '{ticker}'. Try an exact symbol like VWCE.DE or IWDA.AS."
            else:
                asset_type = "etf_acc" if info.get("quoteType", "") == "ETF" else "stock"
                result = {
                    "ticker":        ticker,
                    "name":          info.get("longName") or info.get("shortName", ticker),
                    "exchange":      info.get("exchange", ""),
                    "currency":      info.get("currency", "EUR"),
                    "asset_type":    asset_type,
                    "current_price": get_price(ticker),
                    "teilfreistellung_pct": round(teilfreistellung_pct(asset_type) * 100),
                    "tax_note":      teilfreistellung_note(asset_type),
                }
        except Exception:
            error = f"Could not look up '{ticker}'. Check the symbol and try again."
    return render(request, "portfolio/search_partial.html", {
        "result": result,
        "error":  error,
        "query":  ticker,
    })
