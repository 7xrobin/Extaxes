# Discover UI Redesign

## What
- Removed bucket (plan_category) tabs from AI suggestions output
- Replaced tabs with a flat list of all suggestion cards
- Added allocation percentage badge (`alloc-badge`) to each card showing the strategy-aligned allocation
- Moved filters below the suggestions output; filters are disabled (opacity + pointer-events) until there is AI or search output
- Category filter now acts as a client-side filter on displayed suggestion cards (via `filterSuggestionCards()`), not just a pre-generation prompt input
- AI prompt now receives the user's approved strategy allocation (categories + %) so `allocation_pct` values on suggestions match the actual strategy

## Why
User requested: tabs replaced by category filter, allocation % shown on cards, filters only active after output, AI suggestions tied to saved strategy allocation.

## Trade-offs
- Filters (theme, type, tax-efficient) are also disabled before first generation; users must generate first then refine. This is a "generate then filter" paradigm rather than "pre-configure then generate".
- Category filter serves dual purpose: client-side card filter AND prompt pre-filter for regeneration. Resetting it to "All categories" on each new generation prevents stale filter state.
- Strategy allocation context in the AI prompt improves result quality but adds tokens to every generation request.

## Files changed
- `portfolio/templates/portfolio/suggestions_partial.html`
- `portfolio/templates/portfolio/overview.html`
- `portfolio/views.py` (`_generate_suggestions`)
- `static/css/main.css`
