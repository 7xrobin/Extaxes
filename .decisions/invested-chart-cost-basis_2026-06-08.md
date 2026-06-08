# Invested Portfolio Chart — Switch to Cost Basis

## What
Changed `_compute_holdings_chart_data` to group holdings by their **cost basis** (`units × avg_purchase_price`) instead of `current_value`.

## Why
The live price API was returning 0 for some tickers (emerging market ETFs in particular). Because `_compute_holdings_chart_data` used `current_value`, any holding whose live price fetch failed produced a 0 value and was filtered out by the `if v > 0` guard — making entire categories silently disappear from the Invested Portfolio Review chart.

Using cost basis removes the dependency on live prices entirely for this chart. It also better matches the "Invested" framing: the chart now shows _how much was put in_ per category, which is more stable and meaningful as a portfolio allocation view.

## Trade-offs
- The chart no longer reflects unrealised gains (i.e. a category that has grown a lot looks the same as one that hasn't). This is intentional — it shows allocation intent, not performance.
- The chart tooltip was updated to say "€X invested" to make this explicit.

## Files changed
- `portfolio/views.py` — `_compute_holdings_chart_data` (line ~35)
- `portfolio/templates/portfolio/overview.html` — holdings chart tooltip label
