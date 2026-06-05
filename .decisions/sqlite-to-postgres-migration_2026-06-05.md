# SQLite → PostgreSQL migration

## What
Moved both of InvestBuddy's persistence layers off SQLite onto a Dockerised
PostgreSQL 16 instance:

1. **Django ORM** — `DATABASES["default"]` now uses
   `django.db.backends.postgresql` (psycopg3), configured from `POSTGRES_*`
   env vars.
2. **LangGraph checkpointer** — `agent/graph.py` swapped `SqliteSaver`
   (raw `sqlite3` connection) for `PostgresSaver` backed by a
   `psycopg_pool.ConnectionPool`. `memory.setup()` creates the checkpoint
   tables idempotently on first compile.

Both share a single Postgres database (`investbuddy`). Django tables and the
LangGraph `checkpoint*` tables coexist without clashing.

Supporting changes:
- `docker-compose.yml` — `postgres:16` service, named volume `pgdata`,
  healthcheck, published on **host port 5433** (see trade-offs).
- `pyproject.toml` / `requirements.txt` — dropped
  `langgraph-checkpoint-sqlite`, added `langgraph-checkpoint-postgres`,
  `psycopg[binary]`, `psycopg-pool`.
- `.env` / `.env.example` — `POSTGRES_*` vars + optional `LANGGRAPH_DB_URL`.
- `Justfile` — `db-up` / `db-down` helpers.

Per the user's choice: **fresh start** (no data carried over from the old
SQLite files) and a **hard cut** to Postgres (no SQLite fallback).

## Why
- Postgres is the realistic production target; SQLite's single-writer model is
  a poor fit for Django's threaded request handling plus a concurrently-written
  LangGraph checkpointer.
- A connection pool with `autocommit=True` + `row_factory=dict_row` is what
  `PostgresSaver` requires, and `prepare_threshold=0` keeps it friendly to
  connection poolers.
- One Postgres DB for both stores keeps the dev setup to a single container;
  the LangGraph tables are namespaced (`checkpoints`, `checkpoint_blobs`,
  `checkpoint_writes`, `checkpoint_migrations`) and don't collide with Django.

## Trade-offs / caveats
- **Host port 5433, not 5432.** A local Postgres already owns 5432 on this
  machine. Inside the container it's still 5432; only the published host port
  is 5433. `POSTGRES_PORT` controls it everywhere.
- **No data migration.** The old `db.sqlite3` / `langgraph_memory.sqlite3`
  files are left on disk (gitignored) but unused. Delete them once you're
  confident, or run `dumpdata`/`loaddata` later if you want the old ORM rows.
- **RAG embeddings** stay as a Django `JSONField` (Postgres `jsonb`); cosine
  similarity is still computed in-memory with numpy. No pgvector introduced.
- **Pre-existing test failures are unrelated to this change.** 24 tests in
  `chat`/`portfolio` were already failing at HEAD: the views are
  `@login_required` but those tests never `force_login` (302 redirects), and
  `chat.tests.ResetSessionTest` posts to a `/chat/reset/` view/URL that no
  longer exists (404) while patching `sqlite3.connect` for removed checkpoint-
  clearing logic. Verified the DB layer is healthy: migrations apply, the
  Postgres test DB creates/destroys, system check is clean, and non-login
  tests pass. The stale `ResetSessionTest` should be rewritten or removed
  since the SQLite reset path it asserts on is gone.

## Files changed
- `docker-compose.yml` (new)
- `investbuddy/settings.py` — `DATABASES`, `LANGGRAPH_DB_URL`
- `agent/graph.py` — `PostgresSaver` + `ConnectionPool`
- `pyproject.toml`, `requirements.txt`
- `.env`, `.env.example`
- `Justfile` — `db-up` / `db-down`
