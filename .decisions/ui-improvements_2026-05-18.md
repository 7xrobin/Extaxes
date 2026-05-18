# UI & Content Improvements

## What
Four improvements to Kyron's UX and content consistency:

1. **Agent intro** — Added a welcome message to the first chat turn in `intake_node`. Combined with the first intake question into one message to avoid two consecutive agent bubbles.

2. **Delete chat** — Added `delete_session` view (POST), URL `chat/delete/<id>/`, and an inline × button in each sidebar session row. Button is hidden by default and revealed on hover via CSS.

3. **ETF type explanations** — Added a static `<div class="asset-type-hints">` below the Asset Type select in the Add Holdings form, explaining the tax treatment of each type in plain English.

4. **Terminology consistency** — `plan_category` (e.g. "Core World ETF") is now the primary label in both the holdings table and overview portfolio table, with a small `*Acc`/`*Dist` superscript indicator for ETF accumulation type. Fixed `_tax_note()` to say "Savings / Cash" instead of "Savings product".

## Why
- The chat had no onboarding context — users didn't know what Kyron was before being asked questions.
- Sessions accumulated with no way to clean them up.
- Users unfamiliar with German tax law (the target audience — expats) couldn't distinguish Acc vs Dist ETFs when adding holdings.
- The holdings table used `get_asset_type_display` ("Accumulating ETF") while the plan and overview used `plan_category` ("Core World ETF"), creating inconsistent terminology across views.

## Trade-offs
- The intro + first question are combined in one message bubble. This keeps the conversation clean but means the first message is longer than subsequent ones.
- ETF hints are always visible (not a tooltip/popover). Simpler and more accessible, but adds vertical space to the form.
- `*Acc`/`*Dist` badge only shows for ETF types — stocks and savings need no indicator since the category name is already self-explanatory.

## Files changed
- `agent/nodes.py` — intake intro, savings terminology fix
- `chat/views.py` — `delete_session` view
- `chat/urls.py` — delete URL
- `chat/templates/chat/base.html` — session-row + delete button
- `portfolio/templates/portfolio/holdings.html` — plan_category primary + badge
- `portfolio/templates/portfolio/overview.html` — badge added
- `portfolio/templates/portfolio/upload.html` — ETF type hints
- `static/css/main.css` — `.session-row`, `.btn-delete-session`, `.type-badge`, `.asset-type-hints`
