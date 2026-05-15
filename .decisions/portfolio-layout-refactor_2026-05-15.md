# Portfolio Page Layout Refactor

## What
Restructured the portfolio overview page into four distinct sections:
1. **Holdings card** (full width, top) — replaces the old balance card; shows totals + HTMX-loaded holdings table in compact style
2. **Discover card** (full width) — AI Suggestions and Search by Ticker tabs; Holdings tab removed (moved to top)
3. **Strategy Summary card** (bottom-left, grid-2) — pie chart with legend showing allocation %, strategy text; removed "Portfolio vs Target" fill meter and category bars
4. **Invested Portfolio Review card** (bottom-right, grid-2) — compact inline holdings table from template context + AI Review button that calls new `ai_review_partial` view

## Why
User requested a clearer information hierarchy: holdings first, discovery second, strategy/review side-by-side. The old layout mixed holdings into the Discover accordion (hard to find) and had the balance card separate from the holdings list it described.

## Trade-offs / Caveats
- The `#category-bars` OOB swap in `quick_add` now targets a non-existent element; HTMX silently ignores it (no breakage, just no visual update after quick-add). Category bars were intentionally removed per spec.
- The "Invested Portfolio Review" compact table is rendered from template context (not HTMX), so it reflects state at page load, not live. The top Holdings card (HTMX) is the live view.
- AI Review uses `gpt-4o` with max 350 tokens — same pattern as suggestions.

## Files Changed
- `portfolio/templates/portfolio/overview.html` — complete restructure
- `portfolio/views.py` — added `ai_review_partial` view
- `portfolio/urls.py` — added `ai-review/` route
- `static/css/main.css` — added styles for new components
