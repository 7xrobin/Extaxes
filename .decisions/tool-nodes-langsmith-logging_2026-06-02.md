# Agent tools as graph nodes + LangSmith + structured logging

## What was done

Three related changes to make the agent observable and its tool use explicit.

### 1. Agent tools promoted to graph nodes
The agent's "tools" used to be private helpers called inline inside nodes
(`get_prices` in `analysis_node`, `_tax_rag_context` and `_projection_context`
inside `qa_node`/`digest_node`). They were invisible to any tracer and tangled
into the LLM nodes.

- New `agent/tools.py` holds the three capabilities as standalone, `@traceable`
  functions: `fetch_prices`, `retrieve_tax_context`, `compute_projection`.
- New thin node wrappers in `agent/nodes.py` (`fetch_prices_node`,
  `retrieve_node`, `simulate_node`) invoke those tools and write results into
  state.
- `qa_node` was split: it is now a **pure router** (classify → save / adjust /
  answer) at the interrupt point, and a new `answer_node` does the LLM reply
  using context gathered by the tool nodes.

New graph shape:
```
upload → fetch_prices → analysis → plan → qa (interrupt)
qa → approval (save) → qa
qa → plan   (revise)
qa → retrieve → simulate → answer → qa   (question path)
```

### 2. LangSmith monitoring
- `agent/observability.py`: `traceable()` (no-op when LangSmith absent),
  `instrument_openai()` (wraps the raw OpenAI client so LLM calls are traced),
  `tracing_enabled()`.
- `kyron/settings.py` normalises `LANGSMITH_*` (or legacy `LANGCHAIN_*`) env vars
  into the `LANGCHAIN_*` vars LangGraph/LangSmith read, **before** the graph is
  imported. Tracing is opt-in and requires both `LANGSMITH_TRACING=true` and a key.
- LangGraph node runs trace automatically; tool spans come from `@traceable`;
  raw OpenAI calls come from `instrument_openai`.

### 3. Structured Django logging
- Full `LOGGING` dict in settings: `verbose` (file) + `concise` (console)
  formatters, console + 5 MB rotating file handler (`logs/kyron.log`), per-app
  loggers (`agent`, `rag`, `portfolio`, `chat`, `digest`, `accounts`).
- Levels via `DJANGO_LOG_LEVEL` / `APP_LOG_LEVEL`. `logs/` is gitignored.
- Tool/node/graph code logs at info/warning instead of silently swallowing.

## Why this approach over alternatives

- **Tools as nodes vs. LangChain bind_tools/ToolNode**: the LLM calls go through
  the raw `openai` SDK, not LangChain chat models, and the tool selection here is
  deterministic (keyword/intent driven), not model-chosen. Making them explicit
  graph nodes gives the requested "tools = nodes" with full trace visibility
  without rewriting the prompting layer to function-calling.
- **Router/answer split**: keeps the carefully tuned approve/adjust/question
  classification intact (see `qa-flow-graph-restructure_2026-05-15.md`) while
  letting the RAG and projection tools run as their own observable steps before
  the answer is generated.
- **Env-var-driven LangSmith**: zero code change to toggle; safe default off;
  no hard dependency (all wrappers degrade to no-ops).

## Trade-offs / caveats

- **Node name `qa` preserved** so existing LangGraph SQLite checkpoints (whose
  `next == ("qa",)`) still resume. New state keys (`prices`, `retrieved_context`,
  `projection_context`) default to missing and are read with `.get()`.
- `analysis_node` keeps a **fallback** direct `get_prices` call when `prices` is
  absent from state, so it stays correct when called standalone (unit tests).
- The question path now runs **two extra nodes** (retrieve, simulate) per turn.
  Each is cheap (RAG retrieval + deterministic math) and short-circuits to "".
- `langsmith` was already importable as a transitive dep; pinned explicitly in
  `requirements.txt` (`~=0.8`).
- Pre-existing `portfolio` CSV-upload test failures (11 fail / 3 err) are
  unrelated to this change — confirmed identical on a clean stash.

## Files changed

- `agent/observability.py` (new) — LangSmith glue
- `agent/tools.py` (new) — traceable tools
- `agent/nodes.py` — tool nodes, qa router/answer split, instrumented client, logging
- `agent/graph.py` — new nodes + edges (fetch_prices/retrieve/simulate/answer)
- `agent/state.py` — `prices`, `retrieved_context`, `projection_context`
- `agent/tests.py` — fixed stale `route_after_approval` import; added tool-node tests
- `kyron/settings.py` — LangSmith config + `LOGGING`
- `requirements.txt`, `.env.example`, `.gitignore`
