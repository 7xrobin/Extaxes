from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from django.http import HttpResponse
import pandas as pd
import io
from .models import UserProfile, Holding, ExitRule
from agent.tax_engine import tax_on_exit, vorabpauschale, effective_rate, sparerpauschbetrag_limit
from agent.price_service import get_prices

USER_ID = "demo"


def get_or_create_profile():
    profile, _ = UserProfile.objects.get_or_create(user_id=USER_ID)
    return profile


def overview(request):
    profile = get_or_create_profile()
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

    return render(request, "portfolio/overview.html", {
        "profile":        profile,
        "holdings":       holdings,
        "total_invested": total_invested,
        "total_value":    total_value,
        "total_gain":     total_value - total_invested,
    })


def upload_page(request):
    return render(request, "portfolio/upload.html")


@require_POST
def upload_csv(request):
    """Parse Trade Republic CSV export."""
    profile = get_or_create_profile()
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


@require_POST
def add_manual(request):
    """Add a single holding manually via form."""
    profile = get_or_create_profile()
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


def holdings_partial(request):
    """HTMX partial for the holdings table."""
    profile  = get_or_create_profile()
    holdings = profile.holdings.select_related("exit_rule").all()
    return render(request, "portfolio/holdings.html", {"holdings": holdings})


def tax_partial(request):
    """HTMX partial for the tax summary panel."""
    profile  = get_or_create_profile()
    holdings = profile.holdings.all()
    allowance = sparerpauschbetrag_limit(profile.is_married)
    tax_rows  = []
    total_vp  = 0.0

    for h in holdings:
        vp  = vorabpauschale(h.current_value, h.asset_type)
        tax = tax_on_exit(h.unrealised_gain, h.asset_type)
        total_vp += vp
        tax_rows.append({
            "holding":       h,
            "vorabpauschale": vp,
            "tax_if_sold":   tax,
            "rate":          effective_rate(h.asset_type) * 100,
        })

    return render(request, "portfolio/tax_summary.html", {
        "tax_rows":  tax_rows,
        "total_vp":  total_vp,
        "allowance": allowance,
    })
