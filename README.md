# Kyron

Investment strategy assistant for expats in Germany. Tracks ETFs and stocks, explains German tax rules in plain English, and generates a weekly digest — powered by a LangGraph agent with human-in-the-loop conversation.

---

## What it does

1. **Onboards** you through six questions (savings, emergency fund, monthly budget, goals, risk tolerance, salary) in a conversational chat interface.
<!-- TODO: Add support for bank CSV exports from other banks (N26, Scalable etc.) -->
2. **Reads your portfolio** from a Bank CSV export or manual entry.
3. **Calculates taxes** — Abgeltungsteuer, Teilfreistellung, Vorabpauschale, Sparerpauschbetrag — using the current German tax rules, without calling an external service.
4. **Proposes a strategy** via LLM Model: allocation across asset categories, exit rules, and one plain-English tax insight.
5. **Iterates on the plan** until you approve it.
<!-- TODO: Use chrones recorrency to set a time to generate the digest, default to weekly but make configurable -->
6. **Generates a weekly digest** on demand: portfolio snapshot, tax status, plan check, and one action item.

---

## Tech stack

| Layer | Technology |
|---|---|
| Web framework | Django 5 + SQLite |
| Agent / conversation | LangGraph 0.6 with `SqliteSaver` checkpointer |
| LLM | GPT-4o (OpenAI) — only for plan and digest nodes |
| Live prices | yfinance (no API key required) |
| Frontend | HTMX (served as local static file, no build step) |
| Static files | WhiteNoise |
| German tax engine | Pure Python, no external dependencies |
| Package manager | uv |

---

## Project layout

```
invest-tax/
├── agent/                  # LangGraph agent — no Django views here
│   ├── graph.py            # StateGraph definition, SqliteSaver wiring
│   ├── state.py            # AgentState TypedDict and sub-TypedDicts
│   ├── nodes.py            # All node functions and routing logic
│   ├── tax_engine.py       # Pure Python German tax calculations
│   ├── price_service.py    # yfinance wrapper
│   └── tests.py
│
├── portfolio/              # Django app — long-term storage
│   ├── models.py           # UserProfile, Goal, Holding, ExitRule
│   ├── views.py            # Overview, upload CSV, add manual, tax partial
│   └── tests.py
│
├── chat/                   # Django app — HTMX chat interface
│   ├── views.py            # Bridges HTTP requests to LangGraph graph
│   └── tests.py
│
├── digest/                 # Django app — weekly digest trigger
│   ├── views.py            # Calls digest_node directly, returns HTML
│   └── tests.py
│
├── kyron/                  # Django project config
│   ├── settings.py
│   └── urls.py
│
├── db.sqlite3              # Django ORM database (long-term memory)
├── langgraph_memory.sqlite3 # LangGraph checkpoint database (conversation state)
├── pyproject.toml          # Dependencies (managed by uv)
└── uv.lock                 # Auto-generated lock file — do not edit
```

---

## Memory architecture

Kyron uses **two completely separate SQLite databases** with different lifetimes and purposes. This is the most important architectural decision in the project.

### 1. Django database (`db.sqlite3`) — long-term memory

Stores structured, durable user data via the Django ORM. This data survives conversation resets.

```
UserProfile      one per user  savings, emergency fund, risk profile, tax bracket
Goal             many per user investment goals with target amounts and dates
Holding          many per user portfolio positions (ticker, units, purchase price)
ExitRule         one per Holding  when to sell or review a position
```

This database is written to by the portfolio views (manual entry, CSV upload) and read by `upload_node` in the agent. It represents facts that should persist across sessions: "this user holds 10 units of VWCE.DE bought at €90".

### 2. LangGraph database (`langgraph_memory.sqlite3`) — conversation memory

Stores the full LangGraph checkpoint state for each conversation thread. This is what enables the agent to resume mid-conversation — across HTTP requests, server restarts, and browser refreshes.

**Tables written by LangGraph:**
- `checkpoints` — one row per graph execution step, keyed by `thread_id + checkpoint_id`
- `checkpoint_blobs` — binary blobs for large state fields (message lists, holdings lists)
- `checkpoint_writes` — pending writes that haven't been committed to a checkpoint yet

**What a checkpoint contains:** the entire `AgentState` dict at a point in time — all messages, `intake_step`, `savings_total`, `holdings`, `tax`, `approved_strategy`, and `current_node`. LangGraph replays from the latest checkpoint on resume.

**Reset behaviour:** the chat reset button deletes rows from the three LangGraph tables for the current `thread_id`. It does **not** touch `db.sqlite3`. After reset, `UserProfile` and `Holding` records remain intact — `upload_node` will re-read them from the Django DB on the next run. The user does not need to re-enter their portfolio.

---

## LangGraph: how the agent works

### The graph

```
        ┌──────────────────────────────────┐
        │                                  │
        ▼                                  │
[INTERRUPT] → intake → route_after_intake ─┤
                                           │
                                     "upload" ▼
                                        upload
                                           │
                                           ▼
                                        analysis
                                           │
                                           ▼
                              ┌─────── plan ◄────┐
                              │         │        │
                              ▼         ▼        │
                         [INTERRUPT] → approval ─┘
                              route_after_approval
                                         │
                                     "done" ▼
                                         END

[digest] ────────────────────────────────────────► END
(called directly by digest/views.py, outside the graph flow)
```

### `interrupt_before` — the human-in-the-loop mechanism

The graph is compiled with:
```python
interrupt_before=["intake", "approval"]
```

This tells LangGraph to **pause execution before those nodes** and hand control back to the caller. The graph does not run to completion in a single `invoke` call — it suspends and waits for the next message.

**Why two interrupts?**
- `intake` — pauses before every onboarding question so the user can answer it. The answer is merged into state, then `invoke(None)` resumes execution so the node can parse the answer and ask the next question.
- `approval` — pauses after the plan is shown so the user can approve or request changes. The user's reply is merged into state, then `invoke(None)` resumes so `approval_node` can decide whether to loop back to `plan` or go to `END`.

### The double-invoke pattern

Every user message in the chat requires **two sequential `graph.invoke()` calls**:

```python
# Step 1: merge the user's message into the checkpoint state
# This triggers an interrupt (pauses BEFORE intake/approval) with the new message in state
graph.invoke(
    {"messages": [{"role": "user", "content": user_text}]},
    THREAD_CONFIG,
)

# Step 2: resume from the interrupt — the node now runs with the user message available
graph.invoke(None, THREAD_CONFIG)
```

If you call `invoke` only once with the user message, it merges the message and immediately re-interrupts before the node runs — the node never sees the answer. The second `invoke(None)` is what actually executes `intake_node` or `approval_node`.

### State schema

`AgentState` extends LangGraph's `MessagesState` (which provides the `messages` field with built-in append semantics) and adds typed fields for every piece of data the agent needs:

```python
class AgentState(MessagesState):
    # Flow control
    user_id: str
    current_node: str       # routing signal — not the LangGraph node name
    intake_step: int        # which onboarding question we're on (0–5)

    # User profile (filled by intake_node)
    savings_total: float
    emergency_fund_floor: float
    investable_surplus: float
    monthly_investment_budget: float
    goals: list[GoalState]
    risk_profile: str       # "conservative" | "balanced" | "growth"
    tax_bracket: float      # estimated marginal rate (0.14 – 0.42)
    is_married: bool        # affects Sparerpauschbetrag allowance

    # Portfolio (filled by upload_node + analysis_node)
    holdings: list[HoldingState]
    total_invested: float
    total_current_value: float
    total_unrealised_gain: float
    allocation: dict

    # Tax summary (filled by analysis_node)
    tax: TaxState

    # Strategy (filled by plan_node + approval_node)
    approved_strategy: dict
    monthly_split: dict
```

`current_node` is a routing signal written by each node to tell the conditional edge functions where to go next. It is **not** the LangGraph internal node name — it is a field in the application state used by `route_after_intake` and `route_after_approval`.

### Nodes summary

| Node | LLM? | Reads | Writes |
|---|---|---|---|
| `intake_node` | No | `intake_step`, `messages` | `savings_total`, `emergency_fund_floor`, `goals`, `risk_profile`, `tax_bracket`, next question message |
| `upload_node` | No | Django DB (`Holding`, `UserProfile`) | `holdings` list in AgentState |
| `analysis_node` | No | `holdings` (from state), yfinance prices | `holdings` (with live prices), `tax` (TaxState), totals |
| `plan_node` | GPT-4o | Full AgentState context | `approved_strategy`, plan message |
| `approval_node` | No | Last user message | `current_node` ("done" or "adjust") |
| `digest_node` | GPT-4o | Full AgentState context | New digest message appended to `messages` |

`digest_node` is called **directly** by `digest/views.py`, bypassing the graph routing entirely. It is also registered as a graph node so it can be called in future automated flows.

### SqliteSaver configuration

```python
conn = sqlite3.connect(settings.LANGGRAPH_DB_PATH, check_same_thread=False)
memory = SqliteSaver(conn)
```

`check_same_thread=False` is required because Django handles each HTTP request in a separate thread, but the SQLite connection is created once at module import time and reused across all requests. Without this flag, SQLite raises a `ProgrammingError` on any request after the first.

`SqliteSaver.from_conn_string()` was the API in LangGraph ≤0.3. In LangGraph 0.4+, it returns a context manager, not an instance — using it directly causes a `TypeError`. The correct API in 0.4+ is `SqliteSaver(conn)` with a pre-opened connection.

---

## German tax engine

All calculations are in `agent/tax_engine.py`. No external service is called. Each constant has a source citation.

### Abgeltungsteuer (§32d EStG)

The flat capital gains tax rate in Germany:
- 25% Abgeltungsteuer + 5.5% Solidaritätszuschlag surcharge
- Effective rate: **26.375%** on gains

### Teilfreistellung (§20 InvStG)

Partial tax exemption on investment funds. Rationale: fund companies already pay corporate tax on their income before it reaches investors, so the state reduces the investor's tax to avoid double taxation.

- Equity ETFs (≥51% stocks): **30% of gains exempt** → effective rate ~18.46%
- Bond ETFs (<25% equities): 0% exempt → full 26.375%
- Individual stocks: 0% exempt → full 26.375%

This is why accumulating ETFs are tax-efficient for long-term investors in Germany.

### Vorabpauschale (§18 InvStG)

An annual advance tax on accumulating (thesaurierend) ETFs. Because acc. ETFs never pay dividends, the tax authority collects a proxy tax each January based on a theoretical return:

```
Basisertrag = fund_value_jan1 × Basiszins × 0.70
Vorabpauschale = max(0, Basisertrag − distributions_paid) × (1 − 0.30 Teilfreistellung)
Tax = Vorabpauschale × 26.375%
```

`Basiszins` is set annually by the Deutsche Bundesbank / BMF. The 2026 rate is 3.20%. Only accumulating ETFs (`etf_acc`) attract this tax — distributing ETFs (`etf_dist`) are taxed when dividends are paid, so no advance tax applies.

### Sparerpauschbetrag (§20(9) EStG)

Annual tax-free allowance on capital income:
- Single: **€1,000/year**
- Married (filing jointly): **€2,000/year**

Applied at the portfolio level. Kyron tracks remaining allowance in `TaxState.sparerpauschbetrag_remaining` and uses it to shade the tax estimates shown in the portfolio view.

### Exit tax (§19(3) InvStG + Jahressteuergesetz 2024)

If you leave Germany with a portfolio whose total acquisition cost exceeds **€500,000**, the departure is treated as a deemed disposal — you owe capital gains tax as if you sold everything on the day you left, even though you haven't.

Kyron flags this as a warning when `total_invested > €500,000`. This rule applies from 1 January 2025.

---

## Django apps

### `portfolio` — long-term memory + portfolio UI

Owns the four Django models. Data here persists independently of the agent conversation.

**Views:**
- `GET /portfolio/` — fetches live prices via yfinance, updates holding values, renders the overview
- `POST /portfolio/manual/` — adds a single holding by form
- `POST /portfolio/upload/csv/` — parses a Trade Republic CSV, upserts holdings via `update_or_create`
- `GET /portfolio/holdings/` — HTMX partial: holdings table
- `GET /portfolio/tax/` — HTMX partial: tax summary panel

### `chat` — conversation interface

A thin bridge between HTMX HTTP requests and the LangGraph graph. Contains no business logic.

**Views:**
- `GET /chat/` — loads message history from the latest checkpoint; bootstraps the graph (two invokes) if no messages exist yet
- `POST /chat/message/` — double-invoke pattern; returns only the new messages as an HTML partial via `chat/message.html`
- `POST /chat/reset/` — deletes LangGraph checkpoint rows for the thread, preserves Django DB, re-bootstraps

### `digest` — weekly digest

**Views:**
- `GET /digest/` — scans the message history for the last assistant message that looks like a digest (heuristic: length > 200, contains "portfolio", "tax", "allowance", or "educational")
- `POST /digest/generate/` — calls `digest_node(state.values)` directly, returns the result as an HTML partial with characters escaped

---

## Running locally

```bash
# Install dependencies (creates .venv automatically)
uv sync

# Configure environment
cp .env.example .env        # add OPENAI_API_KEY and DJANGO_SECRET_KEY

# Set up Django
just migrate
just static
just server
```

Open `http://localhost:8000` — redirects to `/chat/` and starts the intake flow.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | Used only in `plan_node` and `digest_node` |
| `DJANGO_SECRET_KEY` | Yes (prod) | Django session signing key |
| `DEBUG` | No | Defaults to `True` |

### Running tests

```bash
just test
```

132 tests covering: tax engine functions, price service (mocked yfinance), node helpers, routing logic, all portfolio views, chat view double-invoke logic, and digest view HTML escaping.

---

## Key design decisions

**Two databases, not one.** The Django DB and the LangGraph DB are kept separate intentionally. Mixing them would mean the conversation checkpoint format leaks into the relational schema, and resetting a conversation would risk deleting portfolio data. The separation makes the contract explicit: Django owns facts, LangGraph owns conversation flow.

**GPT-4o for two nodes only.** `plan_node` and `digest_node` call the LLM. All other nodes — including all tax calculations — are deterministic Python. This keeps costs predictable, makes the logic auditable, and means the tax figures are traceable to statute, not to model output.

**`interrupt_before`, not `interrupt_after`.** The interrupts are placed before `intake` and `approval` so that when the graph resumes, the node runs with the user's input already in state. If the interrupt were after the node, the node would run before the user had answered, producing empty or stale output.

**WhiteNoise for static files.** No nginx or CDN required in development or production. WhiteNoise serves compressed static files directly from Django. The `runserver_nostatic` app replaces Django's built-in dev static server so the same WhiteNoise path is used in both environments.

**HTMX from local static file.** The CDN URL was unreliable in the preview browser. HTMX is downloaded to `static/js/htmx.min.js` and served by WhiteNoise. This also means the app works fully offline once the server is running.

**`update_or_create` for CSV import.** The portfolio CSV upload uses `update_or_create` (not `get_or_create`) because `Holding.units` and `Holding.avg_purchase_price` are required fields with no default. `get_or_create` would attempt to INSERT without those values and fail on a fresh database. `update_or_create` passes them in the `defaults` dict, which is used both for creation and for updating an existing row.

---

## Limitations and known issues

- **Single-user demo.** `USER_ID = "demo"` and `THREAD_CONFIG` are module-level constants. There is no authentication or multi-tenancy.
- **EIMI.DE (iShares EM IMI) returns €0 from yfinance.** This XETRA ticker is not reliably resolved. Use `EIMI.L` (London) or `IS3N.DE` as an alternative.
- **Tax calculations are estimates.** Church tax (Kirchensteuer), loss carryforward offsets, and foreign tax credits are not modelled. The Sparerpauschbetrag is not automatically deducted from individual position tax figures — it is tracked at the portfolio level only.
- **Vorabpauschale uses a fixed Basiszins.** The 2026 rate (3.20%) is hardcoded. Update `BASISZINS_2026` in `agent/tax_engine.py` each January.
- **No persistent approved strategy.** The approved plan lives in the LangGraph checkpoint. If the checkpoint is reset, the strategy is lost. A future improvement would write the approved plan to `UserProfile` in the Django DB.
