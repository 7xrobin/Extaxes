# InvestBuddy improvements — shared taxonomy, Discover/Holdings UX, full rebrand

## What

Seven improvements landed in one pass:

1. **Shared type taxonomy** — new `agent/catalog.py` is the single source of truth for
   asset types (`etf_acc/etf_dist/stock/savings`) and allocation categories. Exposed to the
   agent via a new `@traceable get_available_types()` tool in `agent/tools.py`. The Holding
   model, the strategy (`plan_node`), Discover suggestions, and the risk validator all read
   from it, so a category means the same thing everywhere.
2. **Teilfreistellung surfaced per asset** — `teilfreistellung_pct()` / `teilfreistellung_note()`
   in `tax_engine.py`; `Holding.teilfreistellung_pct()` / `Holding.tax_note()` model methods;
   a `.tag-tax` badge ("TF 30%" / "Teilfreistellung 30%", hover for the implication) on the
   holdings table, review table, suggestion cards, and search results.
3. **Removed the "Long-term" tag** — dropped the `exit_timeframe` tag from suggestion cards
   and the field from the Discover prompt schema.
4. **Growth Simulation moved** — out of the Holdings card tabs to its own card *below* the
   holdings list; no longer auto-loads (`hx-trigger` is now `submit` only) — runs on a
   "Run Simulation" button.
5. **Discover merged into Holdings card** — Holdings card tabs are now **Holdings | Discover**;
   the separate Discover card and its sub-tabs are gone. The Discover tab unifies ticker
   search + attribute filters + AI suggestions.
6. **Refined Discover filters** — Category (from catalog), Type (from catalog), free-text
   Theme, and a Tax-efficient-only toggle. They `hx-include` into the suggestions POST and
   steer the LLM prompt.
7. **Full rebrand Kyron → InvestBuddy** — including the Django project package
   `kyron/` → `investbuddy/` (settings module, WSGI, ROOT_URLCONF, log filename,
   LANGSMITH_PROJECT) and every user-facing string + AI-prompt identity.

## Why (vs alternatives)

- **Catalog as a curated Python module + tool** rather than deriving types live from yfinance:
  deterministic, fast, and gives the LLM a fixed vocabulary so strategy/discovery/holdings
  actually line up (the user's "strategy category match" goal). Live derivation was rejected as
  slow and unreliable for a stable taxonomy.
- **Filters steer the LLM** rather than hard-filtering a fixed product list — Discover already
  generates fresh suggestions each call, so prompt steering fits the existing design.
- **`Holding.ASSET_TYPES` values kept identical** → choices-only change, no DB migration
  (`makemigrations --check` confirms "No changes").
- **Full package rename** was chosen by the user over user-facing-only; blast radius was just
  3 import lines + the dir move, verified before committing to it.

## Trade-offs / caveats

- Renaming the package dir requires restarting any running server process. DB and migrations
  untouched (no app rename, only the project package).
- The pre-existing `portfolio.tests` failures (11 fail + 3 error, all 302/auth/mock-setup
  issues) are unchanged by this work — confirmed by stashing to the committed tree and
  re-running. The agent suite is 120/120 green (8 new tests for the catalog/tool + TF helpers).
- A `git stash` used during verification briefly swept the working tree; recovered cleanly via
  `git stash pop` after clearing a stale `investbuddy/__pycache__`. No work lost.
- The simulation + Discover "Type" dropdowns now list all four asset types (incl. Savings/Cash)
  from the catalog; harmless but Savings in a growth sim is a slightly odd option.

## Files changed

- New: `agent/catalog.py`
- Renamed: `kyron/` → `investbuddy/`
- Edited: `agent/{tools,nodes,validators,tax_engine,tests}.py`, `agent/{graph,observability,simulation}.py`
  (rebrand), `portfolio/{models,views}.py`,
  `portfolio/templates/portfolio/{overview,holdings,suggestions_partial,search_partial}.html`,
  `static/css/main.css`, `manage.py`, `investbuddy/{settings,wsgi}.py`, README + remaining
  Kyron strings across chat/accounts/digest templates and `rag/ingest.py`.

## Verification

- `python manage.py check` → no issues; `makemigrations --check` → no changes.
- `python manage.py test agent` → 120 passed. `portfolio` failure count unchanged from baseline.
- Template render smoke test confirms the TF badge renders on holdings.
