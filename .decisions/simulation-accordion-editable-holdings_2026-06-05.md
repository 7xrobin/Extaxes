# Simulation accordion + editable holdings

## What
- Moved Growth Simulation from a standalone card into the Holdings tab panel as a collapsible `<details>` accordion (closed by default)
- Wrapped Discover tab panel content in a `<details open>` accordion (expanded by default, collapsible)
- Made holdings `units` and `avg_purchase_price` inline-editable via HTMX inputs in the table
- Added `update_holding` view + `/portfolio/holdings/<pk>/update/` URL that recomputes derived fields (current_value, unrealised_gain, unrealised_gain_pct) without refetching live prices

## Why
User wanted the simulation to feel connected to holdings (not a separate card), the discover content to be collapsible, and holdings amounts to be directly editable.

## Trade-offs
- Inline holding updates reuse the current_price already in the DB rather than fetching live prices — avoids slow API calls on every keystroke but means edits don't reflect the very latest price. A page refresh will refresh prices.
- `hx-trigger="change"` fires on blur/Enter, so the whole holdings table re-renders after the user leaves the field — clean but loses any pending edits in other rows if multiple rows were being edited simultaneously (edge case).
- `<details>` native browser accordion requires no JS, but lacks animated open/close — acceptable given the existing app uses the same pattern elsewhere.

## Files changed
- `portfolio/views.py` — added `update_holding` view
- `portfolio/urls.py` — added `holdings/<int:pk>/update/` route
- `portfolio/templates/portfolio/holdings.html` — units + avg_purchase_price now `<input>` with HTMX post
- `portfolio/templates/portfolio/overview.html` — simulation moved inside holdings tab accordion; discover wrapped in open accordion; standalone simulation card removed
- `static/css/main.css` — `.sim-accordion`, `.sim-summary`, `.sim-summary-hint`, `.sim-accordion-body`, `.holding-edit-input`, `.discover-accordion-body`
