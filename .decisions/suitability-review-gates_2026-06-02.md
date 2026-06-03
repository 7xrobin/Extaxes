# Suitability review gates: UCITS validation, plan alignment, bucketed output

## What

Added two LLM-output review gates and restructured the discover suggestions UI, in
response to a project review (three points: ETF validation, intent/plan validation,
bucketed output structure).

1. **ETF / UCITS domiciliation gate** — new `agent/validators.validate_etf_suggestions()`.
   Called from `portfolio.views.suggestions_partial` before suggestions reach the user.
   Annotates each suggestion with a `warning` when it doesn't look UCITS / EU-domiciled
   (hard blocklist of known US-listed funds like SPY/VTI/QQQ, plus EU ticker-suffix and
   exchange-field heuristics). Never drops a suggestion — surfaces a warning chip on the card.

2. **Plan / risk-profile alignment gate** — new `agent/validators.validate_plan_alignment()`.
   Wired into `plan_node` as a **bounded self-correcting loop**: generate plan → check
   equity weight against the user's risk band → if misaligned, re-prompt once with a
   correction → if still misaligned, append a transparent note to the plan prose.

3. **Bucketed suggestions output** — `suggestions_partial` now groups suggestions by
   `plan_category` via `_bucket_suggestions()` and the template renders one tab per
   allocation bucket (e.g. Global Developed Markets / Emerging Markets / Other).

Both validators are `@traceable`, so they appear as LangSmith spans like the tools in
`agent/tools.py`.

## Why this approach over alternatives

- **Deterministic heuristics instead of `yf.Ticker().info`** (which the review suggested):
  the suggestions view already makes 2 network calls per ticker (prices + period returns);
  adding a third per-ticker `.info` call (slow, rate-limited, flaky) would noticeably hurt
  the "Generate Suggestions" latency. Ticker-suffix + exchange-field + known-US-ticker
  checks are instant, offline, and good enough as a *flag-and-verify* gate (we never block).

- **In-node loop for plan alignment instead of a graph node that routes back to `plan`:**
  `plan_node` appends the plan text + the "Does this look right?" confirmation messages to
  chat state. A LangGraph loop back into `plan` (as the review sketched) would re-run the
  node and **duplicate those messages** in the same turn, and complicate the `qa` interrupt.
  Keeping the retry inside the node produces the confirmation messages exactly once. The
  loop is bounded by `MAX_PLAN_ALIGNMENT_RETRIES = 1` so it can't spin or blow up latency.

- **Warn, don't drop / don't hard-fail:** the product is educational ("not personal
  investment advice"). Silently removing a suggestion or refusing to present a plan would
  be worse UX than transparently flagging it and letting the user decide.

## Trade-offs / caveats

- The UCITS gate is heuristic: a UCITS fund listed somewhere outside the suffix/exchange
  lists would get a (false-positive) "verify" warning. That's the safe direction — it asks
  the user to verify rather than asserting suitability. Extend `VALID_EXCHANGES` /
  `EU_SUFFIXES` / `KNOWN_NON_UCITS` in `agent/validators.py` as needed.
- Equity-weight detection relies on category-name keywords (`_EQUITY_HINTS`). An oddly named
  equity category could be missed. Risk bands are deliberately wide to avoid nagging.
- The alignment loop adds at most one extra GPT-4o call to `plan_node` (only when the first
  proposal is misaligned).

## Files changed

- `agent/validators.py` (new) — `validate_etf_suggestions`, `validate_plan_alignment`.
- `agent/nodes.py` — `plan_node` self-correcting alignment loop; import + retry constant.
- `agent/tests.py` — 10 new unit tests for both validators.
- `portfolio/views.py` — call ETF gate in `suggestions_partial`; add `_bucket_suggestions`.
- `portfolio/templates/portfolio/suggestions_partial.html` — bucket tabs + warning chip.
- `static/css/main.css` — `.suggestion-warning`, `.bucket-tabs`, `.bucket-count` styles.

## Verification

- `manage.py test agent` → 112 passed (incl. 10 new validator tests).
- `manage.py check` → no issues; graph compiles (11 nodes).
- Pre-existing portfolio test failures (login-redirect) are unchanged by this work
  (11 failures / 3 errors on both clean tree and this branch).
- Template render verified: buckets group correctly, tabs + warning chip present.
