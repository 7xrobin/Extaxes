from django.shortcuts import render
from django.views.decorators.http import require_POST
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from agent.graph import graph
from agent.nodes import digest_node, upload_node, analysis_node
from chat.views import _get_active_session, _make_thread_config
from portfolio.models import UserProfile


@login_required
def digest_page(request):
    """Show the last generated digest, or prompt to generate one."""
    session = _get_active_session(request)
    digest_msg = None

    if session:
        thread_config = _make_thread_config(session)
        try:
            state = graph.get_state(thread_config)
            if state:
                messages = state.values.get("messages", [])
                for msg in reversed(messages):
                    content = msg.get("content", "")
                    if (msg.get("role") == "assistant"
                            and len(content) > 200
                            and any(w in content.lower() for w in ["portfolio", "tax", "allowance", "educational"])):
                        digest_msg = content
                        break
        except Exception:
            pass

    return render(request, "digest/digest.html", {"digest": digest_msg})


def _build_digest_state(user_id: str, checkpoint_state: dict) -> dict:
    """Merge LangGraph checkpoint state with fresh DB data for digest generation."""
    state = {**checkpoint_state, "user_id": user_id}

    # Load holdings + compute analysis whenever the checkpoint is stale/empty
    state = upload_node(state)
    state = analysis_node(state)

    # Inject approved strategy from DB if missing in checkpoint
    if not state.get("approved_strategy"):
        profile, _ = UserProfile.objects.get_or_create(user_id=user_id)
        if profile.strategy_approved:
            state = {
                **state,
                "approved_strategy": {
                    "plan_text": profile.approved_strategy_text,
                    "data": profile.approved_strategy_data,
                },
            }

    return state


@login_required
@require_POST
def generate_digest(request):
    """Trigger the digest node directly and return result as HTMX partial."""
    session = _get_active_session(request)
    if not session:
        return HttpResponse('<div class="digest-placeholder">No active session.</div>')

    thread_config = _make_thread_config(session)
    try:
        state = graph.get_state(thread_config)
        checkpoint = state.values if state else {}
        current_state = _build_digest_state(str(request.user.id), checkpoint)
        updated = digest_node(current_state)
        digest_text = updated["messages"][-1]["content"]
    except Exception as e:
        digest_text = f"Could not generate digest: {e}"

    safe_html = digest_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    formatted  = safe_html.replace("\n", "<br>")

    return HttpResponse(
        f'<div class="digest-content">{formatted}</div>'
    )
