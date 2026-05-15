# Fix: Chat Input Clearing & QA Approval Flow

## What was done

Two bugs fixed:

### Bug 1 — Input clears too late
Moved `input.value = ''` from `htmx:afterRequest` to `htmx:beforeRequest` in the chat template. The input now clears immediately when the user submits, not when the agent's response arrives.

### Bug 2 — QA flow re-sends plan on user questions
Three changes to `agent/nodes.py`:

1. **Stricter `_APPROVAL_CLASSIFIER_PROMPT`**: `"adjust"` now requires an explicit change request (e.g. "make it more conservative", "change the allocation"). Explanatory questions are classified as `"question"`.
2. **New `"show"` intent in `approval_node`**: when the user says "show plan" / "review it", the agent re-displays the already-stored `plan_text` (from `approved_strategy["plan_text"]`) without re-running `plan_node` or `analysis_node`.
3. **Updated question re-ask**: after answering a question, the agent now asks "Would you like to review the plan before deciding?" with a 'Show plan' option, instead of auto-showing the plan or re-generating it.
4. **`QA_SYSTEM_PROMPT` update**: added instruction not to repeat the full strategy text in Q&A answers — only reference specific parts when directly relevant.

## Why this approach

- **Input clearing**: moving to `beforeRequest` matches standard chat UX — the input should feel "sent" immediately, not linger until the server responds.
- **Classifier strictness**: the original prompt had no examples distinguishing informational questions from adjustment requests, causing GPT-4o to misclassify questions as `"adjust"`, triggering `plan_node` re-execution (perceived as analysis+plan being re-sent).
- **`"show"` intent over auto-show**: showing the plan on every question re-ask would be noisy. Letting the user explicitly request it gives them control and avoids repetitive output.
- **No re-running `plan_node` for review**: re-running `plan_node` makes a GPT-4o call, regenerates portfolio analysis context, and produces output that looks like "analysis + plan" to the user. Re-displaying stored `plan_text` is instant, free, and idempotent.

## Trade-offs / caveats

- The `"show"` intent depends on the classifier correctly recognising phrases like "show plan", "yes show it", "review it". Edge cases (e.g. "let me see" without "plan") may fall through to `"question"`, which is a safe fallback.
- The `_QUESTION_SYSTEM_PROMPT` still ends with "Would you like to adjust anything…?" — this text is now slightly redundant since `approval_node` adds its own re-ask. Could be cleaned up later.
- If `approved_strategy["plan_text"]` is empty (e.g. plan parsing failed), the `"show"` path returns "No plan on file." — acceptable but could be improved.

## Files changed

- `chat/templates/chat/chat.html` — input clearing timing
- `agent/nodes.py` — classifier prompt, approval_node (show + question handlers), QA_SYSTEM_PROMPT
