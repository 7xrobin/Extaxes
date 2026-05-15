from uuid import uuid4
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from agent.graph import graph
from .models import ChatSession


def _to_dict(msg) -> dict:
    """Convert a LangChain BaseMessage or plain dict to {"role":..., "content":...}."""
    if isinstance(msg, dict):
        return msg
    role = "assistant" if getattr(msg, "type", "human") in ("ai", "tool") else "user"
    return {"role": role, "content": getattr(msg, "content", str(msg))}


def _get_messages(state) -> list[dict]:
    raw = state.values.get("messages", []) if state else []
    return [_to_dict(m) for m in raw]


def _get_active_session(request) -> ChatSession | None:
    session_id = request.session.get('active_chat_session_id')
    if not session_id:
        return None
    try:
        return ChatSession.objects.get(id=session_id, user=request.user)
    except ChatSession.DoesNotExist:
        return None


def _make_thread_config(session: ChatSession) -> dict:
    return {"configurable": {"thread_id": session.thread_id}}


def _create_session(request) -> ChatSession:
    thread_id = f"u{request.user.id}_{uuid4().hex[:8]}"
    session = ChatSession.objects.create(user=request.user, thread_id=thread_id)
    request.session['active_chat_session_id'] = session.id
    return session


@login_required
def chat_page(request):
    session = _get_active_session(request)
    if not session:
        session = _create_session(request)

    thread_config = _make_thread_config(session)
    user_id = str(request.user.id)

    try:
        state = graph.get_state(thread_config)
        messages = _get_messages(state)
    except Exception:
        messages = []

    if not messages:
        graph.invoke(
            {"user_id": user_id, "intake_step": 0, "messages": []},
            thread_config,
        )
        graph.invoke(None, thread_config)
        state = graph.get_state(thread_config)
        messages = _get_messages(state)

    return render(request, "chat/chat.html", {"messages": messages, "session": session})


@login_required
@require_POST
def send_message(request):
    user_text = request.POST.get("message", "").strip()
    if not user_text:
        return HttpResponse("")

    session = _get_active_session(request)
    if not session:
        return HttpResponse("")

    thread_config = _make_thread_config(session)

    # Update title from first real user message
    if session.title == 'New Chat':
        session.title = user_text[:60]
        session.save(update_fields=['title'])

    try:
        state_before = graph.get_state(thread_config)
        count_before = len(state_before.values.get("messages", [])) if state_before else 0
    except Exception:
        count_before = 0

    graph.invoke(
        {"messages": [{"role": "user", "content": user_text}]},
        thread_config,
    )
    # With interrupt_before, the first invoke enqueues the user message and stops
    # before the next node. The second invoke(None) resumes and actually runs it.
    graph.invoke(None, thread_config)

    state_after = graph.get_state(thread_config)
    all_messages = _get_messages(state_after)
    # Only return new assistant messages — the user message is shown optimistically
    # in the frontend before this request even completes.
    new_messages = [m for m in all_messages[count_before:] if m["role"] == "assistant"]

    return render(request, "chat/message.html", {"messages": new_messages})


@login_required
@require_POST
def new_chat(request):
    session = _create_session(request)
    thread_config = _make_thread_config(session)
    user_id = str(request.user.id)

    graph.invoke(
        {"user_id": user_id, "intake_step": 0, "messages": []},
        thread_config,
    )
    graph.invoke(None, thread_config)

    return redirect('/chat/')


@login_required
def switch_session(request, session_id):
    session = get_object_or_404(ChatSession, id=session_id, user=request.user)
    request.session['active_chat_session_id'] = session.id
    return redirect('/chat/')
