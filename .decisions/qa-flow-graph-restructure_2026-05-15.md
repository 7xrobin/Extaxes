# Architectural fix: plan → qa direct routing (Option A)

## What was done

Restructured the LangGraph flow so `plan` routes directly to `qa`, bypassing the old `approval` loop for questions. `approval_node` is now a thin, non-interrupting save-only step.

**Old flow:**
```
plan → approval (interrupt) → re_ask → approval (loop)
                            → done → qa
                            → adjust → plan
```

**New flow:**
```
plan → qa (interrupt, universal post-plan handler)
qa   → approval (save, no interrupt) → qa
qa   → plan (user wants to revise)
qa   → qa (loop)
```

## Why this approach

The root bug was architectural: questions during plan review stayed in the `approval → re_ask → approval` loop, where repeated classifier calls could eventually misclassify a question as `"adjust"` and re-trigger `plan_node`. The loop also prevented the user from asking follow-up questions freely.

Option A removes the loop entirely. `qa_node` is the single conversational handler for all post-plan interactions. It uses `strategy_saved` state flag to switch between two modes:
- **Pending approval**: classifies intent (approve / adjust / question) before answering. On approve/adjust it sets `current_node` and returns immediately; on question it calls GPT-4o with `_QA_PENDING_PROMPT` which ends with a re-ask.
- **Approved**: regular Q&A with `_QA_APPROVED_PROMPT` (no re-ask, no approval detection).

`route_after_qa` reads `current_node` to route to `"approval"` (save) or `"plan"` (revise).

Also fixed: circular import `from agent.graph import graph` inside `agent/graph.py` (pre-existing bug surfaced by dev server restart) and removed debug `print(graph.get_graph().draw_ascii())`.

## Trade-offs / caveats

- `approval` is removed from `interrupt_before` — it runs immediately when triggered, no user input needed. This is intentional (it's just a DB write).
- `strategy_saved: bool` added to `AgentState`. Existing threads in LangGraph SQLite won't have this field set; they default to `False` (pending approval), which is safe — the user will just be re-asked for confirmation on their next message.
- The `_QA_PENDING_PROMPT` always ends with "Does the plan look good to you…?" This means every question during pending state gets an approval ask appended. Could feel repetitive, but it's the clearest UX signal.
- Revise keywords in `route_after_qa` (`"revise plan"`, `"change plan"`, etc.) are only checked in the approved state path; in pending state, `"adjust"` intent is detected via the classifier, which is more reliable.

## Files changed

- `agent/state.py` — added `strategy_saved: bool`
- `agent/graph.py` — new edges, removed approval from interrupt_before, fixed circular import
- `agent/nodes.py` — rewrote `qa_node`, `route_after_qa`, `approval_node`; removed `route_after_approval`, `_QUESTION_SYSTEM_PROMPT`; updated `plan_node` to set `strategy_saved: False`
