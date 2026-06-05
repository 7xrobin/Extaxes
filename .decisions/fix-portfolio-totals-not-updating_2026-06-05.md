# Fix: portfolio summary not updating after holding changes

## What
- `update_holding` view was returning only the holdings table HTML with no OOB swap for `#portfolio-totals`, so inline edits (units / avg price) updated the table but left the header summary stale until a full page refresh.
- Upgraded the search result card from a plain link to `/portfolio/upload/` to an inline quick-add form using the existing `/portfolio/quick-add/` endpoint — matching the suggestions flow and keeping the user on the page.

## Why this approach
`holdings_partial` already had the pattern: render both partials, append an `hx-swap-oob="true"` div for `#portfolio-totals`. The fix mirrors that exactly so both code paths stay consistent.

The search form reuses `quick_add` (which already handles OOB totals + OOB table) rather than a new endpoint, keeping the surface area small.

## Trade-offs / caveats
- `update_holding` does not call `_refresh_prices` before computing totals — totals are computed from the DB-stored prices last fetched on page load. This is the same behaviour as before the fix; prices stay accurate until the next full load.
- The search quick-add doesn't include a `plan_category` field (the suggestions flow sets this from the AI recommendation). Holdings added via search will have an empty `plan_category` and fall back to the asset-type display label everywhere it's referenced.

## Files changed
- `portfolio/views.py` — `update_holding` now builds OOB totals swap identical to `holdings_partial`
- `portfolio/templates/portfolio/search_partial.html` — replaced static link with inline quick-add form
