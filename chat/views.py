from django.shortcuts import render
from django.http import HttpResponse
from django.views.decorators.http import require_POST
from agent.graph import graph, USER_ID, THREAD_CONFIG


def _to_dict(msg) -> dict:
    """Convert a LangChain BaseMessage or plain dict to {"role":..., "content":...}."""
    if isinstance(msg, dict):
        return msg
    role = "assistant" if getattr(msg, "type", "human") in ("ai", "tool") else "user"
    return {"role": role, "content": getattr(msg, "content", str(msg))}


def _get_messages(state) -> list[dict]:
    raw = state.values.get("messages", []) if state else []
    return [_to_dict(m) for m in raw]


def chat_page(request):
    """Main chat interface. Loads existing message history from LangGraph memory."""
    try:
        state = graph.get_state(THREAD_CONFIG)
        messages = _get_messages(state)
    except Exception:
        messages = []

    if not messages:
        # Initialize graph state (pauses before intake due to interrupt_before)
        graph.invoke(
            {"user_id": USER_ID, "intake_step": 0, "messages": []},
            THREAD_CONFIG,
        )
        # Resume with None — LangGraph 0.6 resumes from interrupt without new input
        graph.invoke(None, THREAD_CONFIG)
        state = graph.get_state(THREAD_CONFIG)
        messages = _get_messages(state)

    return render(request, "chat/chat.html", {"messages": messages})


@require_POST
def send_message(request):
    """
    HTMX endpoint. Receives user message, resumes the graph,
    returns the new messages as HTML partials appended to the chat.
    """
    user_text = request.POST.get("message", "").strip()
    if not user_text:
        return HttpResponse("")

    # Track message count before invoking
    try:
        state_before = graph.get_state(THREAD_CONFIG)
        count_before = len(state_before.values.get("messages", [])) if state_before else 0
    except Exception:
        count_before = 0

    # Step 1: merge user message into checkpoint state (pauses before intake again)
    graph.invoke(
        {"messages": [{"role": "user", "content": user_text}]},
        THREAD_CONFIG,
    )
    # Step 2: resume so the intake/approval node actually runs with the user's input
    graph.invoke(None, THREAD_CONFIG)

    # Get updated state and return only new messages as dicts for the template
    state_after = graph.get_state(THREAD_CONFIG)
    all_messages = _get_messages(state_after)
    new_messages = all_messages[count_before:]

    return render(request, "chat/message.html", {"messages": new_messages})


@require_POST
def reset_session(request):
    """
    Clear LangGraph conversation memory for this thread.
    Keeps Django DB records (UserProfile, Holdings) as long-term memory.
    """
    import sqlite3
    from django.conf import settings

    try:
        conn = sqlite3.connect(settings.LANGGRAPH_DB_PATH)
        # LangGraph 0.2 checkpoint tables
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            try:
                conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", [USER_ID])
            except sqlite3.OperationalError:
                pass  # table may not exist yet
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Re-initialise with fresh state
    graph.invoke(
        {"user_id": USER_ID, "intake_step": 0, "messages": []},
        THREAD_CONFIG,
    )
    graph.invoke(None, THREAD_CONFIG)

    return HttpResponse('<script>window.location.href="/chat/"</script>')
