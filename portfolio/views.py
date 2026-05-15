from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.template.loader import render_to_string
import pandas as pd
import io
import json
import yfinance as yf
from openai import OpenAI
from .models import UserProfile, Holding, ExitRule
from agent.tax_engine import tax_on_exit, vorabpauschale, effective_rate, sparerpauschbetrag_limit
from agent.price_service import get_price, get_prices


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


@login_required
def overview(request):
    profile = _get_or_create_profile(str(request.user.id))
    holdings = list(profile.holdings.select_related("exit_rule").all())

    tickers = [h.ticker for h in holdings]
    prices = get_prices(tickers) if tickers else {}

    for h in holdings:
        price = prices.get(h.ticker, h.current_price)
        cost  = h.units * h.avg_purchase_price
        h.current_price       = price
        h.current_value       = h.units * price
        h.unrealised_gain     = h.current_value - cost
        h.unrealised_gain_pct = (h.unrealised_gain / cost * 100) if cost > 0 else 0
        h.save(update_fields=[
            "current_price", "current_value", "unrealised_gain", "unrealised_gain_pct"
        ])

    total_invested = sum(h.units * h.avg_purchase_price for h in holdings)
    total_value    = sum(h.current_value for h in holdings)
    total_gain     = total_value - total_invested
    gain_pct       = (total_gain / total_invested * 100) if total_invested > 0 else 0

    strategy_data = profile.approved_strategy_data or None
    target_amount = (strategy_data or {}).get("total_target_amount") if strategy_data else None
    coverage_pct  = None
    if target_amount and target_amount > 0:
        coverage_pct = min(100, total_value / target_amount * 100)

    category_coverage = _compute_category_coverage(strategy_data, holdings)

    return render(request, "portfolio/overview.html", {
        "profile":            profile,
        "holdings":           holdings,
        "total_invested":     total_invested,
        "total_value":        total_value,
        "total_gain":         total_gain,
        "gain_pct":           gain_pct,
        "strategy_data":      strategy_data,
        "target_amount":      target_amount,
        "coverage_pct":       coverage_pct,
        "category_coverage":  category_coverage,
    })


@login_required
def upload_page(request):
    return render(request, "portfolio/upload.html")


@login_required
@require_POST
def upload_csv(request):
    """Parse Trade Republic CSV export."""
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
            h, _ = Holding.objects.update_or_create(
                profile=profile,
                ticker=ticker,
                defaults={
                    "isin":               str(row.get("isin", "")).strip(),
                    "asset_type":         str(row.get("asset_type", "etf_acc")).strip(),
                    "units":              float(row.get("units", 0)),
                    "avg_purchase_price": float(row.get("avg_purchase_price", 0)),
                },
            )
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
    )
    ExitRule.objects.create(holding=h)
    return redirect("/portfolio/")


@login_required
def holdings_partial(request):
    """HTMX partial for the holdings table."""
    profile  = _get_or_create_profile(str(request.user.id))
    holdings = profile.holdings.select_related("exit_rule").all()
    return render(request, "portfolio/holdings.html", {"holdings": holdings})


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
        })

    return render(request, "portfolio/tax_summary.html", {
        "tax_rows":  tax_rows,
        "total_vp":  total_vp,
        "allowance": allowance,
    })


# ── DISCOVER ──────────────────────────────────────────────────────────────────

DISCOVER_SYSTEM_PROMPT = """You are Kyron, a financial assistant for expats in Germany.
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
  "exit_timeframe": "Long-term 5+ years",
  "rationale": "One sentence why this fits the user."
}

asset_type must be one of: etf_acc, etf_dist, stock

Rules:
- Prefer accumulating ETFs (etf_acc) for German tax efficiency (Teilfreistellung)
- Include a mix: global/world ETFs, US/S&P 500 UCITS ETFs (e.g. SXR8.DE, XNAS.DE, VUSA.AS), emerging markets, bonds/stability, thematic or satellite positions, individual stocks where appropriate
- allocation_pct values do NOT need to sum to 100 — they represent the suggested budget share for each plan category
- Never recommend leveraged or derivative products
- Return ONLY valid JSON: {"suggestions": [...]}
- This is educational — not personal investment advice.
"""


def _generate_suggestions(profile, goals):
    from django.conf import settings
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    goals_text = ", ".join(g.name for g in goals) if goals else "general wealth growth"
    user_context = f"""
User profile:
- Risk profile: {profile.risk_profile}
- Monthly investment budget: €{profile.monthly_investment_budget:.0f}
- Tax bracket: {profile.tax_bracket * 100:.0f}%
- Married (double Sparerpauschbetrag): {profile.is_married}
- Goals: {goals_text}

Return a JSON object with key "suggestions" containing an array of exactly 6 suggestion objects.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": DISCOVER_SYSTEM_PROMPT},
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
    suggestions = _generate_suggestions(profile, goals)
    tickers = [s["ticker"] for s in suggestions]
    prices = get_prices(tickers)
    for s in suggestions:
        s["current_price"] = prices.get(s["ticker"], 0.0)
    return render(request, "portfolio/suggestions_partial.html", {"suggestions": suggestions})


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
                result = {
                    "ticker":        ticker,
                    "name":          info.get("longName") or info.get("shortName", ticker),
                    "exchange":      info.get("exchange", ""),
                    "currency":      info.get("currency", "EUR"),
                    "asset_type":    "etf_acc" if info.get("quoteType", "") == "ETF" else "stock",
                    "current_price": get_price(ticker),
                }
        except Exception:
            error = f"Could not look up '{ticker}'. Check the symbol and try again."
    return render(request, "portfolio/search_partial.html", {
        "result": result,
        "error":  error,
        "query":  ticker,
    })
