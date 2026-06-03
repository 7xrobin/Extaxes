"""
German investment tax engine.
All calculations are educational estimates — not tax advice.
Sources: InvStG (Investment Tax Act), EStG (Income Tax Act).
"""

# SOURCE: §32d EStG — Abgeltungsteuer (flat withholding tax on capital gains)
# 25% tax + 5.5% Solidaritätszuschlag surcharge = 26.375% effective rate
ABGELTUNGSTEUER = 0.25
SOLI = 0.055
STANDARD_RATE = ABGELTUNGSTEUER * (1 + SOLI)  # 0.26375

# SOURCE: §20 InvStG — Teilfreistellung (partial tax exemption on funds)
# Prevents double taxation (companies pay corp tax before paying dividends)
# Equity ETF (≥51% stocks): 30% of gains exempt → effective rate ~18.46%
# Bond ETF (<25% stocks): 0% exempt → full 26.375%
# Individual stocks: 0% exempt → full 26.375%
TEILFREISTELLUNG = {
    "etf_acc":  0.30,
    "etf_dist": 0.30,
    "stock":    0.00,
    "savings":  0.00,
}

# SOURCE: §18 InvStG — Vorabpauschale (annual advance tax on acc. ETFs)
# Basiszins set annually by Deutsche Bundesbank / BMF publication
BASISZINS_2026 = 0.0320   # SOURCE: BMF Basiszins publication Jan 2026
BASISERTRAG_FACTOR = 0.70  # SOURCE: §18(1) InvStG — 70% of Basiszins

# SOURCE: §20(9) EStG — Sparerpauschbetrag (annual tax-free allowance)
SPARERPAUSCHBETRAG_SINGLE  = 1_000.0
SPARERPAUSCHBETRAG_MARRIED = 2_000.0

# SOURCE: §19(3) InvStG + Jahressteuergesetz 2024 (Annual Tax Act 2024)
# Exit taxation on investment units introduced Jan 1, 2025
# Triggered when total acquisition cost exceeds threshold and taxpayer leaves Germany
EXIT_TAX_THRESHOLD = 500_000.0


def effective_rate(asset_type: str) -> float:
    """
    Effective capital gains tax rate after Teilfreistellung.
    SOURCE: §32d EStG + §20 InvStG.
    ETF equity: 0.26375 * (1 - 0.30) = ~18.46%
    Stock:      0.26375 * (1 - 0.00) = 26.375%
    """
    exemption = TEILFREISTELLUNG.get(asset_type, 0.0)
    return STANDARD_RATE * (1 - exemption)


def teilfreistellung_pct(asset_type: str) -> float:
    """
    §20 InvStG partial-exemption fraction for an asset type (e.g. 0.30 for equity
    ETFs, 0.0 for individual stocks). Used to surface the implication per holding.
    """
    return TEILFREISTELLUNG.get(asset_type, 0.0)


def teilfreistellung_note(asset_type: str) -> str:
    """
    One-line plain-English implication of the Teilfreistellung for an asset type,
    suitable for a tooltip on a holding or suggestion card.
    """
    exempt = TEILFREISTELLUNG.get(asset_type, 0.0)
    rate = effective_rate(asset_type) * 100
    if exempt > 0:
        return (
            f"{exempt*100:.0f}% of gains are tax-exempt (Teilfreistellung, §20 InvStG) — "
            f"effective capital-gains rate ≈ {rate:.1f}% instead of {STANDARD_RATE*100:.1f}%."
        )
    return (
        f"No Teilfreistellung — gains taxed at the full Abgeltungsteuer rate "
        f"≈ {rate:.1f}%."
    )


def tax_on_exit(gain: float, asset_type: str) -> float:
    """
    Estimated tax owed if position is fully sold today.
    SOURCE: §32d EStG + §20 InvStG Teilfreistellung.
    Note: Sparerpauschbetrag applied at portfolio level separately.
    Returns 0 if gain <= 0 (losses are not taxed).
    """
    if gain <= 0:
        return 0.0
    return gain * effective_rate(asset_type)


def vorabpauschale(
    value_jan1: float,
    asset_type: str,
    distributions: float = 0.0,
) -> float:
    """
    Annual advance tax on accumulating ETFs.
    SOURCE: §18 InvStG.
    - Only applies to etf_acc (thesaurierend / accumulating).
    - Deducted automatically by German broker each January.
    - distributions: any dividends paid out reduce the Vorabpauschale
      SOURCE: §18(1) sentence 3 InvStG.
    - Returns 0 if ETF lost value during the year.
    """
    if asset_type != "etf_acc":
        return 0.0

    basisertrag = value_jan1 * BASISZINS_2026 * BASISERTRAG_FACTOR
    vorab = max(0.0, basisertrag - distributions)
    taxable = vorab * (1 - TEILFREISTELLUNG["etf_acc"])  # 30% exempt
    return taxable * STANDARD_RATE


def sparerpauschbetrag_limit(is_married: bool = False) -> float:
    """
    Annual tax-free allowance on capital income.
    SOURCE: §20(9) EStG.
    """
    return SPARERPAUSCHBETRAG_MARRIED if is_married else SPARERPAUSCHBETRAG_SINGLE


def exit_tax_applies(total_acquisition_cost: float) -> bool:
    """
    Whether exit taxation warning should be shown.
    SOURCE: §19(3) InvStG + Jahressteuergesetz 2024.
    Applies from Jan 1, 2025 when leaving Germany with portfolio
    above the threshold — treated as deemed disposal on departure date.
    """
    return total_acquisition_cost > EXIT_TAX_THRESHOLD


def compare_etf_vs_stock(gain: float) -> dict:
    """
    Side-by-side tax comparison for a given gain amount.
    Used by the ETF vs stock comparison tool in the chat.
    """
    etf_tax   = tax_on_exit(gain, "etf_acc")
    stock_tax = tax_on_exit(gain, "stock")
    return {
        "gain": gain,
        "etf": {
            "tax": etf_tax,
            "you_keep": gain - etf_tax,
            "effective_rate_pct": effective_rate("etf_acc") * 100,
        },
        "stock": {
            "tax": stock_tax,
            "you_keep": gain - stock_tax,
            "effective_rate_pct": effective_rate("stock") * 100,
        },
        "etf_advantage": stock_tax - etf_tax,
    }
