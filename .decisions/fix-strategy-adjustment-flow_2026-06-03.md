# Fix: Strategy Adjustment Flow Saved the Pre-Adjustment Plan

## What
When a user adjusted a proposed strategy before approving it (e.g. "increase the
bond portion to 70%"), the *original* plan — not the adjusted one — was being
saved to `UserProfile` and shown in the Portfolio.

Root cause was in `agent/nodes.py::_classify_intent`. A defensive phrase guard
downgraded a correct LLM `adjust` classification back to `question` whenever the
message didn't contain one of a small hardcoded list of phrases
(`_ADJUST_PHRASES`). For phrasings like "increase the bond portion to 70%" the
LLM correctly returned `adjust`, but the guard rewrote it to `question`, so the
graph routed to the answer node instead of `plan`. No re-planning happened,
`approved_strategy` stayed as Plan A, and the subsequent "looks good" saved the
stale plan.

Reproduced end-to-end by driving the real graph topology (intake → plan →
adjust → approve) with a mocked LLM: with the bug, the DB saved "Plan A" even
though the LLM classified the edit as `adjust`.

## Changes
- Replaced the narrow `_ADJUST_PHRASES` downgrade with `_looks_like_edit()`, a
  high-recall edit detector (broad verb/phrase list) that **excludes
  question-shaped messages** (ending in `?`) so hypothetical "what if I had more
  bonds?" asks still route to Q&A.
- `_classify_intent` now treats the LLM verdict as primary. `_looks_like_edit`
  can only **upgrade** a `question` to `adjust` — it never downgrades a confident
  `adjust`. Also normalises unexpected LLM output to `question`.
- The approved-state revise path in `qa_node` now reuses `_looks_like_edit`
  (replacing the even narrower `_REVISE_KEYWORDS`), so post-approval edits are
  detected consistently.
- Added unit tests: `LooksLikeEditTest` and `ClassifyIntentTest` (incl. a
  regression test for the exact downgrade bug).

## Follow-up (same day): approved-state edits still kept the previous plan

Symptom reported after the first fix: editing the strategy *after* it was already
saved still showed the previous strategy in the Portfolio.

Cause: the approved-state branch of `qa_node` detected edits with `_looks_like_edit`
(keyword matching) **only** — there was no LLM classifier on this path, unlike the
pending state. Natural phrasings without a keyword (e.g. "I'd prefer mostly bonds
from now on") fell straight through to the answer node: the assistant chatted about
the change but never re-ran `plan`, so `approved_strategy` stayed put and the next
approval re-saved the unchanged plan. Reproduced end-to-end (intake → save →
keyword-less edit → "looks good") — the DB kept Plan A.

Fix: the approved-state branch now calls `_classify_intent` (the same LLM + keyword
logic as the pending state). An `adjust` verdict routes back to `plan` (which resets
`strategy_saved=False`), so the next approval re-saves the revised plan. `approve`
and `question` verdicts stay in Q&A. Added `QaNodeApprovedStateTest`.

## Why this approach
The asymmetry is deliberate: re-planning when the user merely asked a question is
recoverable (they see a fresh plan and can clarify), but silently dropping a real
edit saves the *wrong* strategy — the worst failure for this flow. So we bias
toward honouring edits. We keep the LLM classifier (good few-shot prompt,
deterministic temp=0/seed=42) as the primary signal rather than replacing it with
pure keyword matching.

## Trade-offs / caveats
- Keyword lists are inherently leaky; a question that happens to contain an edit
  verb but no `?` (rare) could trigger an unnecessary re-plan. Considered
  acceptable given the recoverable cost and the high cost of the opposite error.
- The deterministic state/graph flow was already correct — when `adjust` is
  classified, the adjusted plan is saved. The fix is purely in intent routing.

## Files changed
- `agent/nodes.py`
- `agent/tests.py`

## Note (out of scope)
The `portfolio`, `chat`, and `digest` test modules have 24 pre-existing
failures + 12 errors on a clean tree (unrelated to this change). The `agent`
suite is fully green (133/133).
