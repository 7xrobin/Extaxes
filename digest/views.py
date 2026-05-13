from django.shortcuts import render
from django.views.decorators.http import require_POST
from django.http import HttpResponse
from agent.graph import graph, THREAD_CONFIG
from agent.nodes import digest_node


def digest_page(request):
    """Show the last generated digest, or prompt to generate one."""
    digest_msg = None
    try:
        state = graph.get_state(THREAD_CONFIG)
        if state:
            messages = state.values.get("messages", [])
            # Find the last assistant message that looks like a digest
            # (heuristic: longer assistant messages containing portfolio/tax keywords)
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


@require_POST
def generate_digest(request):
    """Trigger the digest node directly and return result as HTMX partial."""
    try:
        state = graph.get_state(THREAD_CONFIG)
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
