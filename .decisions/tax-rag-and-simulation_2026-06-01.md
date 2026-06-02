# Tax RAG + Investment Simulation

## What
Two capabilities added to Kyron:

1. **Tax RAG** — a new `rag` Django app. Trusted tax URLs are added in Django admin
   (`/admin/rag/taxsource/`), fetched, cleaned (stdlib `html.parser`), chunked, and embedded with
   OpenAI `text-embedding-3-small`. Vectors are stored as JSON on `TaxChunk`. The agent retrieves
   the top passages via in-memory cosine similarity (numpy) and injects them as the **primary
   source of truth** (with cited URLs) into the chat Q&A (`qa_node`) and the weekly digest
   (`digest_node`).
2. **Investment simulation + time range** —
   - `agent/simulation.py`: deterministic `project_growth` (monthly-compounded, gross + net-of-tax),
     `required_monthly_for_target`, `recommend_investment`.
   - Portfolio "Growth Simulation" card (`/portfolio/simulate/` → `simulation_partial.html`) with a
     Chart.js line chart (gross / net / contributed) and live inputs.
   - Time-range selector (1M/3M/6M/1Y/YTD/All) on the holdings card; period gain/loss computed from
     yfinance history (`get_period_start_prices`) and OOB-swapped into the header totals.

## Why this approach
- **Embeddings + SQLite + numpy cosine**, not a vector DB: the trusted-source set is tiny and
  curated, so a whole-table cosine scan is fast and adds zero infra. No new heavy deps (numpy via
  pandas, requests via yfinance — now declared explicitly).
- **Admin-only source management** (user decision): avoids building a bespoke CRUD UI; admin already
  gives add/edit/delete + a re-index action and indexes on save.
- **Deterministic compute injected into LLM context, not tool-calling**: the agent is an
  interrupt-based LangGraph state machine. Refactoring it into a function-calling agent would be
  high-risk. Instead, when a user asks a how-much/projection question, the math runs in Python and
  the exact figures are fed into the prompt ("use these EXACT figures") so numbers are never
  hallucinated. The same engine powers the portfolio card.
- **Net-of-tax** reuses `agent/tax_engine.py` so take-home estimates match the rest of the app.

## Trade-offs / caveats
- RAG retrieval is loaded fully into memory per query — fine for a curated set, would need a real
  vector index at large scale.
- `get_period_start_prices` makes one yfinance call per ticker per range change (network latency);
  holdings without history for a window show "—" and are excluded from the period total.
- Net projection line in the chart applies the effective rate to each year's gain as "if sold that
  year" — a simplification (no Vorabpauschale drag modelled in the projection).
- RAG ingestion needs network + an OpenAI key; failures are captured on the source as `failed`
  with the error, never raised.
- Pre-existing: `agent/tests.py` imports a non-existent `route_after_approval` and errors at import
  (stale after an earlier graph refactor) — the whole agent test module was already red before this
  work. New simulation tests were therefore placed in `agent/test_simulation.py` so they run
  independently. Not fixed here to avoid scope creep; worth a follow-up.

## Files changed
- New: `rag/` (models, ingest, retriever, admin, apps, tests, migrations), `agent/simulation.py`,
  `agent/test_simulation.py`, `portfolio/templates/portfolio/{simulation_partial,totals}.html`.
- Modified: `agent/nodes.py` (RAG + projection injection), `agent/price_service.py`
  (`get_period_start_prices`), `portfolio/views.py` (`simulate_partial`, range logic, helpers),
  `portfolio/urls.py`, `portfolio/templates/portfolio/{overview,holdings}.html`,
  `static/css/main.css`, `kyron/settings.py`, `requirements.txt`, `pyproject.toml`, `Justfile`.

## Verification
- `agent.test_simulation` + `rag` tests: 19/19 pass (`just test` also runs them).
- `manage.py check`: clean. Module import smoke: clean. All new templates render offline.
- `/portfolio/simulate/` authenticated smoke: 200 with gross/net/chart data present.
