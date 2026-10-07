# Learnings

## muster (PR #pending, 2026-10-07)

- `python3 -m pytest` assumed by the plan, but the system python3 has no pytest → verify commands fail as written; run them inside a project `.venv` (`uv venv .venv && uv pip install --python .venv/bin/python pytest`, then `. .venv/bin/activate`).
- A contract test file that mixes modules (test_kanban_contract imported rc_intake and rc_run) cannot port whole in the first module's task → assign each test to the task that ports the module it imports, or it silently drops.

## 2026-10-07 carried from rc-intake docs/learnings.md

- A hermes call without `HERMES_HOME`/`HERMES_KANBAN_HOME` set → it reads another home's board; set both before any `hermes` call.
- A hook that can fail the agent → the agent stalls on a hook error; every fallible step goes inside the retry/log net, exit 0 (only `done` with no context exits 1).
- A Notification hook matcher that includes `idle_prompt` → a wait card for every idle ping; exclude it.
- A hook that opens a wait card without reading the ledger first → a wait card on a finished or blocked ledger; read the ledger first.
- Removing a worktree without checking the pane's agent state yourself → a live agent loses its checkout; check state before `worktree remove`.
- Idle judged from a clock or a single sample → false idle; idle is a persisted first-seen time of an unchanged fingerprint.
- Starting the agent with the task in its argv → the brief is lost or mangled when the agent is slow to start; start the agent empty, then prompt.
- Judging a prompt delivered by the call returning → a brief that never arrived looks sent; save "sending" before the prompt call and judge delivery by herdr state + `completion_seq` + the UserPromptSubmit hook's sha256.
- Trusting `kanban show --json` for the block kind → it lacks `block_kind`; read it from the board sqlite read-only.
- Treating an untrusted-path `agent_not_ready` as a failure → a retry loop on a question only a person can answer; it is a person-wait.
- The plan inventoried scripts, not the live crontab: the source ran a third every-minute job (`rc_run.py flush`) the plan never named → read `crontab -l` (or the scheduler) when porting cron-driven code; muster-flush.sh was added at Task 7.
