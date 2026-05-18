# Fix: Weekly Digest Not Reading Saved Strategy

## What
`generate_digest` now loads the approved strategy from `UserProfile` and injects it into the agent state before calling `digest_node`, when the strategy is not already present in the LangGraph checkpoint state.

## Why
`digest_node` reads `state.get('approved_strategy')` — a transient LangGraph state key only populated during a live approval conversation. When the digest is triggered independently (Digest tab button), the checkpoint state from a prior session doesn't carry this key, so the digest always fell back to "Not yet defined".

The strategy is durably persisted in `UserProfile.approved_strategy_text` and `UserProfile.approved_strategy_data` — the fix bridges the gap by reading from DB when the state is missing it.

## Trade-offs
- The fix is intentionally minimal: it only injects if the key is absent (`not current_state.get("approved_strategy")`), so a live session with the strategy already in state takes precedence.
- `get_or_create` is used for safety (consistent with the rest of the codebase), though in practice the profile should always exist by the time a user generates a digest.

## Files changed
- `digest/views.py`
