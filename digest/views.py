from django.shortcuts import render
from django.views.decorators.http import require_POST
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from agent.graph import graph
from agent.nodes import digest_node
from chat.views import _get_active_session, _make_thread_config


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
        current_state = state.values if state else {}
        updated = digest_node(current_state)
        digest_text = updated["messages"][-1]["content"]
    except Exception as e:
        digest_text = f"Could not generate digest: {e}"

    safe_html = digest_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    formatted  = safe_html.replace("\n", "<br>")

    return HttpResponse(
        f'<div class="digest-content">{formatted}</div>'
    )
