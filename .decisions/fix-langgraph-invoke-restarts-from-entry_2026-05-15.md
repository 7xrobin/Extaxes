# Root-cause fix: `graph.invoke(input, ...)` restarted the LangGraph flow from the entry point

## What was done

Replaced `graph.invoke({"messages": [user_msg]}, thread_config)` with `graph.update_state(thread_config, {"messages": [user_msg]})` in `chat/views.py:send_message`. The follow-up `graph.invoke(None, thread_config)` was kept — it now correctly resumes from the current interrupt instead of from a freshly-reset entry point.

## Why

The user kept reporting that after the plan was sent, asking any question caused the agent to re-send the intake-completion message, the upload message, and a new plan. We previously suspected this was a classifier-misclassification problem and tightened the prompt, lowered `temperature`, added defense-in-depth keyword guards. None of it helped.

Direct LangGraph state inspection revealed the actual cause:

```
BEFORE first invoke: next=('qa',)        ← graph correctly paused at qa interrupt
AFTER  first invoke: next=('intake',)    ← restarted to entry point!
```

The pattern `graph.invoke(input_dict, config)` on an interrupted checkpointed graph is treated by LangGraph as a **new run**, not as a state update on the existing run. It resets `next` to the entry node (`intake`). The follow-up `invoke(None)` then resumed from `intake`, which ran `intake_node` → `upload_node` → `analysis_node` → `plan_node` → pause at `qa`. `qa_node` was never invoked. Every user message after intake completed effectively rewound the agent to the beginning of post-intake flow and produced a brand-new plan.

A debug print inside `qa_node` and `intake_node` confirmed this empirically: `intake_node running. intake_step=6, current_node=qa` appeared in the server log every time a question was asked. `qa_node` never logged.

After the fix, the same debug check showed only `qa_node running` for a question, and `state.next` stayed at `('qa',)` across the state update.

## Why this approach over alternatives

- `Command(resume=...)` (the explicit resume API) requires restructuring how user input is delivered to the graph. `update_state` is a one-line change and matches existing semantics — the `messages` field has an `add_messages` reducer, so the dict update appends correctly.
- We considered (and previously committed) a chain of classifier hardening: deterministic temperature, exact-match instead of substring, defense-in-depth keyword check, tighter system prompt. Those are still valuable as guards, but they were treating symptoms — even a perfect classifier wouldn't have helped because `qa_node` wasn't being called at all.

## Trade-offs / caveats

- `chat_page` and `new_chat` still use `graph.invoke(input_dict, ...)` for **initial** chat creation. That's correct: a brand-new thread has no checkpoint, so starting from entry is the desired behavior.
- Existing chat sessions saved against the pre-fix code may have weird state (intake_step incremented past 6, duplicate plan/upload messages from previous bug runs). New chats start clean.
- The explorer agent's earlier reading of `langgraph/pregel/_loop.py` concluded the double-invoke pattern was correct. That conclusion turned out to be wrong in practice — empirical state inspection beat the static reading. Lesson: when LangGraph behavior is in question, verify with `graph.get_state(config).next` before and after each call, not by reading source.

## Files changed

- `chat/views.py` — `send_message` now uses `graph.update_state` for the message append; second `invoke(None)` unchanged.
- `agent/nodes.py` — debug prints in `intake_node` / `qa_node` / `plan_node` removed after diagnosis. Earlier classifier hardening (temperature=0, seed, exact-match, defense-in-depth `_ADJUST_PHRASES`, tightened `_APPROVAL_CLASSIFIER_PROMPT`) retained as guards.
