# Remove the goals onboarding question

## What
Removed the "What are you investing for?" (goals) question from the agent intake flow.

- `agent/nodes.py`: dropped the `goals` entry from `INTAKE_QUESTIONS`, the `goals`
  branch in `_parse_intake_answer`, the `_parse_goals` helper, and the goals
  references in the strategy/QA prompts.
- `agent/state.py`: removed the `GoalState` TypedDict and the `goals` field on
  `AgentState`.
- `agent/tests.py`: removed `ParseGoalsTest` and the `_parse_goals` import.
- `README.md`: "six questions" → "five questions"; removed goals from the state
  schema and the `intake_node` output column.

Onboarding now collects: savings, emergency fund, monthly budget, risk tolerance, salary.

## Why
The user asked to remove the goals question. Since the agent no longer collects
goals, leaving `state.get('goals', [])` references scattered through prompts would
be dead plumbing that always renders `Goals: []`, so the full agent-layer removal
was the clean choice over a surface-level deletion.

## Trade-offs / caveats
- The Django `portfolio` app keeps its own `Goal` model and `_generate_suggestions`
  still reads `profile.goals`. This was left intact deliberately — it's a separate
  persistence/DB concern, not "the question", and it already falls back to
  "general wealth growth" when there are no goals. Removing the model would be a
  migration-level change beyond the request.

## Files changed
- agent/nodes.py
- agent/state.py
- agent/tests.py
- README.md
