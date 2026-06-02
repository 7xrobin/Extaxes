# Holdings period info + simulation rework

## What
Three coupled UI/logic changes to the Portfolio page:

1. **Holdings table** — removed the time-range selector (1M/3M/6M/YTD/1Y) and all
   period-relative gain columns. The table now only shows each holding's all-time
   gain/loss versus its average purchase price (the gain since it was bought).
2. **Discover** — each AI suggestion now has a period dropdown (Past 1M / 3M / 6M /
   YTD / 1Y) that shows that instrument's trailing % return over the chosen window.
3. **Simulation** — moved out of its own card into a second tab inside the Holdings
   card ("Holdings" | "Simulation"). Its default `Return %/yr` is now the
   value-weighted trailing 12-month return of the holdings actually owned, rather
   than a flat 7% assumption. All inputs remain editable.

## Why
- The per-period gain table conflated "how the asset moved" with "how my position
  did", which was confusing. All-time gain answers the question a holder actually
  asks. Per-period performance is more naturally a *discovery* signal, so it moved
  to the Discover cards as a dropdown.
- Tabbing the simulation under Holdings keeps the projection next to the portfolio
  it projects, and de-clutters the page.
- Weighted 1Y trailing return was chosen (over a fixed per-asset-type assumption)
  because the user asked the sim to reflect "their average annual gains" — i.e. the
  ETFs they hold, not a generic market figure.

## How
- `agent/price_service.py`: added `get_period_returns(ticker)` — one 1y history
  fetch, sliced into 1M/3M/6M/1Y/YTD trailing % returns. Never raises (0.0 on gaps).
- `portfolio/views.py`:
  - `_weighted_annual_return(holdings)` reuses the batched `get_period_start_prices(.., "1Y")`
    to blend each holding's trailing return weighted by current value; falls back to
    `DEFAULT_ANNUAL_RETURN_PCT` when no usable history.
  - `overview` passes that as `sim_return_pct`.
  - `holdings_partial` simplified — no `range`/period branch, always all-time.
  - `suggestions_partial` attaches `period_perf` (ordered list of {code,label,gain})
    per suggestion.
- Templates: `holdings.html` slimmed to all-time columns; `overview.html` tabs the
  sim (re-renders the Chart.js sim chart on tab activation so it sizes correctly
  after being hidden); `suggestions_partial.html` adds the dropdown + tiny JS.
- `static/css/main.css`: replaced the dead `.range-*` rules with `.suggestion-perf`
  styling.

## Trade-offs / caveats
- `overview` now does roughly 2N yfinance calls per load (current prices + 1Y starts
  for the weighted return). Acceptable at current portfolio sizes; revisit with
  caching if it gets slow.
- `suggestions_partial` adds one 1y history fetch per suggestion (~6). Runs behind an
  already-slow LLM call, so net UX impact is small.
- Weighted 1Y trailing return is noisy by nature (a single strong/weak year skews the
  default). It's only a prefilled default and the field stays editable.
- Sim chart in a hidden tab is 0-sized until the tab is opened; handled by re-calling
  `renderSimChart()` in `switchHoldingsTab`.

## Follow-ups (same day)
- **Holding age in header**: added a "Holding for" stat to the Holdings top header
  (`totals.html`), computed from the oldest `purchase_date` across holdings
  (`_earliest_purchase` + `_humanize_since` in views). `purchase_date` existed on the
  model but was never populated, so it's now stamped with `date.today()` on creation in
  `add_manual`, `quick_add`, and `upload_csv` (only when still null — re-uploads don't
  reset it). No migration: the field already existed. Older holdings with no date show "—".
- **Simulation input validation bug**: the sim number inputs used `step="100"/"50"/"0.5"`,
  which makes the browser reject any value that isn't an exact multiple (e.g. 1735, or the
  auto-filled weighted return like 11.43%). Changed start/monthly/return to `step="any"`
  and raised the return `max` from 30 to 50. Years stays integer `step="1"`.

## Files changed
- agent/price_service.py
- agent/test_simulation.py
- portfolio/views.py
- portfolio/templates/portfolio/holdings.html
- portfolio/templates/portfolio/overview.html
- portfolio/templates/portfolio/suggestions_partial.html
- static/css/main.css
