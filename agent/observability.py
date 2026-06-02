"""
Observability glue for the agent layer.

Centralises LangSmith integration so the rest of the codebase never has to care
whether tracing is installed or enabled:

- `traceable(...)`  — decorator that registers a function as a LangSmith span when
  LangSmith is available, and is a transparent no-op otherwise.
- `instrument_openai(client)` — wraps an OpenAI client so its calls show up as
  spans in LangSmith; returns the client unchanged when tracing is off.
- `tracing_enabled()` — single source of truth for "are we tracing right now".

Tracing is driven entirely by environment variables (set in kyron/settings.py):
LANGCHAIN_TRACING_V2, LANGCHAIN_API_KEY, LANGCHAIN_PROJECT. LangGraph node runs are
traced automatically by LangSmith when those are set; this module adds the tool
spans and the raw-OpenAI spans on top.
"""
import os
import logging

logger = logging.getLogger("agent.observability")

try:
    from langsmith import traceable as _ls_traceable
    from langsmith.wrappers import wrap_openai as _ls_wrap_openai
    _LANGSMITH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the optional dep
    _LANGSMITH_AVAILABLE = False
    logger.debug("langsmith not installed — tracing decorators are no-ops")


def tracing_enabled() -> bool:
    """True only when LangSmith is importable AND tracing is switched on via env."""
    flag = os.getenv("LANGCHAIN_TRACING_V2", "").strip().lower()
    return _LANGSMITH_AVAILABLE and flag == "true"


def traceable(**kwargs):
    """
    Decorator factory mirroring ``langsmith.traceable``. Use as::

        @traceable(run_type="tool", name="fetch_prices")
        def fetch_prices(...): ...

    When LangSmith is unavailable the function is returned untouched, so importing
    and calling decorated functions never depends on the optional dependency.
    """
    def decorator(func):
        if _LANGSMITH_AVAILABLE:
            return _ls_traceable(**kwargs)(func)
        return func
    return decorator


def instrument_openai(client):
    """
    Wrap an OpenAI client so completions are traced in LangSmith. No-ops (returns
    the original client) when LangSmith is unavailable or tracing is disabled.
    """
    if not tracing_enabled():
        return client
    try:
        wrapped = _ls_wrap_openai(client)
        logger.info("OpenAI client instrumented for LangSmith tracing")
        return wrapped
    except Exception:  # pragma: no cover - defensive; never break the agent
        logger.warning("Failed to wrap OpenAI client for LangSmith", exc_info=True)
        return client
