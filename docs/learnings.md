# Learnings

## muster (PR #pending, 2026-10-07)

- `python3 -m pytest` assumed by the plan, but the system python3 has no pytest → verify commands fail as written; run them inside a project `.venv` (`uv venv .venv && uv pip install --python .venv/bin/python pytest`, then `. .venv/bin/activate`).
- A contract test file that mixes modules (test_kanban_contract imported rc_intake and rc_run) cannot port whole in the first module's task → assign each test to the task that ports the module it imports, or it silently drops.
