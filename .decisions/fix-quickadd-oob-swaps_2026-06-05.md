# Fix: quick_add missing OOB swaps for holdings table and totals

## What
After confirming an ETF holding via the Discover tab's quick-add form, the holdings table and invested summary now update instantly without requiring a page refresh.

## Why this approach
The `quick_add` view was already using HTMX OOB (out-of-band) swaps for the category bars, but it never updated the holdings table (`#holdings-table-container`) or the portfolio totals (`#portfolio-totals`). Both elements already had the correct IDs / were known to the HTMX OOB pattern — the holdings table container just needed an `id` attribute added.

Added `id="holdings-table-container"` to the holdings tab panel div in `overview.html`, then appended two more OOB swap blocks to the `quick_add` response:
- `hx-swap-oob="true"` for `#portfolio-totals` (outerHTML swap, consistent with how `holdings_partial` does it)
- `hx-swap-oob="innerHTML"` for `#holdings-table-container` (innerHTML only, preserving the `hx-get`/`hx-trigger` HTMX attributes on the outer div)

## Bug also fixed
The "not created" path (accumulating units onto an existing holding) never recomputed `current_value`, `unrealised_gain`, or `unrealised_gain_pct`. The saved holding had stale derived figures until `holdings_partial` refreshed prices. Now those fields are recomputed inline from the already-known `current_price` so the response is consistent.

## Trade-offs / caveats
- `_totals_dict` is called with `"ALL"` range, so the summary always shows all-time gain after a quick-add. If the user was previously viewing a period range (e.g. 1M), that filter resets — acceptable since the user just navigated to Discover.
- Prices shown in the holdings table after quick-add come from the DB (last stored price) rather than a fresh yfinance fetch. The holding just added uses the price from the form. This avoids a slow network round-trip in the confirm response.

## Files changed
- `portfolio/templates/portfolio/overview.html` — added `id="holdings-table-container"` to the holdings tab panel div
- `portfolio/views.py` — `quick_add()`: fixed derived field recompute on accumulation; added OOB swaps for totals and holdings table
