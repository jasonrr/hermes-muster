# Learnings

## muster (PR #1, 2026-10-07)

- `python3 -m pytest` assumed by the plan, but the system python3 has no pytest → verify commands fail as written; run them inside a project `.venv` (`uv venv .venv && uv pip install --python .venv/bin/python pytest`, then `. .venv/bin/activate`).
- A contract test file that mixes modules (test_kanban_contract imported rc_intake and rc_run) cannot port whole in the first module's task → assign each test to the task that ports the module it imports, or it silently drops.
- The plan inventoried scripts, not the live crontab: the source ran a third every-minute job (`rc_run.py flush`) the plan never named → read `crontab -l` (or the scheduler) when porting cron-driven code; muster-flush.sh was added at Task 7.
- `hermes plugins validate` uses a RecordingContext whose get_config returns the default, so it never sees Hermes rejecting a plugin setting key whose first segment is reserved (`model`, `plugins`, `security`, `settings`; hermes_cli/plugins_state.py) → every command crashed under real Hermes; only a temp-HERMES_HOME run of the real CLI caught it (setting renamed `agent_model`).
- Hand-mirroring a host path resolver (board db) missed profiles, the `HERMES_KANBAN_DB` pin and slug normalisation → when code runs inside the host process, import the host's resolver instead of copying it.

## base branch (issue #4, 2026-10-07)

- A config suffix on a free-form path (`owner/name=/path@base`) → any `@` already in the path parses as a branch that `check-ref-format` accepts, so the clone path is silently cut; exempt only the unambiguous form (`/@`), document the rest, and validate every git ref argument (`--base -x` reached `git fetch` as an option) in the one function all launches route through (`core.plan`).

## hardening (issue #7, 2026-10-07)

- Free text passed as an argparse positional after options (`kanban block --kind K <card> <reason>`) → a reason starting with `--kind=` is parsed as the option and the block fails with exit 2; put `--` before the positionals whenever any of them is agent- or human-written.

## 2026-10-07 carried from rc-intake docs/learnings.md

- A hermes call without `HERMES_HOME` set → it reads another home's board; set it before any `hermes` call. Do not pin `HERMES_KANBAN_HOME` to it: hermes shares one board root across profiles (`kanban_home()`), so pinning forks the board away from the gateway; ask `hermes_cli.kanban_db.kanban_db_path` for the db path.
- A hook that can fail the agent → the agent stalls on a hook error; every fallible step goes inside the retry/log net, exit 0 (only `done` with no context exits 1).
- A Notification hook matcher that includes `idle_prompt` → a wait card for every idle ping; exclude it.
- A hook that opens a wait card without reading the ledger first → a wait card on a finished or blocked ledger; read the ledger first.
- Removing a worktree without checking the pane's agent state yourself → a live agent loses its checkout; check state before `worktree remove`.
- Idle judged from a clock or a single sample → false idle; idle is a persisted first-seen time of an unchanged fingerprint.
- Starting the agent with the task in its argv → the brief is lost or mangled when the agent is slow to start; start the agent empty, then prompt.
- Judging a prompt delivered by the call returning → a brief that never arrived looks sent; save "sending" before the prompt call and judge delivery by herdr state + `completion_seq` + the UserPromptSubmit hook's sha256.
- Trusting `kanban show --json` for the block kind → it lacks `block_kind`; read it from the board sqlite read-only.
- Treating an untrusted-path `agent_not_ready` as a failure → a retry loop on a question only a person can answer; it is a person-wait.
- A test fake that keys a resource globally when the real system keys it per repo (fake herdr: branch unique across all clones) → a cross-repo bug cannot even be reproduced; key fakes the way the real system does.

## issue-run outbox (issue #5, 2026-10-07)

- Saving an event only after its delivery failed (the design the issue proposed) → a hook killed mid-move still loses it; save first, deliver from the outbox, and make every move read state first so a replay never repeats one.
- A routing check on "the directory exists" (`core.recover`: `runs/<card>/` means an ad-hoc run) → it misroutes as soon as another kind writes there (issue cards' outboxes); key on the record file (`run.json`), not its directory.

## automatic approvers (issue #2, 2026-10-08)

- A rule that requires "both labels" from one dict keyed by label name → an entry whose label equals the main label collapses it to one label event, silently weakening the rule; reject the collision in `config.require()`, and reject unknown entry keys so a typo (`lable`) cannot silently fall back to the default.

## Project status (issue #3, 2026-10-08)

- The issue named `gh project field-list` as the source of the ids, but `item-edit --project-id` needs the Project's node id, which only `gh project view` returns → read the real `gh` JSON (`--format json` against a live Project) before writing a fake, not the issue's sketch.

## proposal snapshots (issue #14, 2026-10-08)

- A command the agent runs from its own shell, routed through the hook entry point (`hook propose`) → it reads stdin like a hook and hangs on the pane's tty until the 30 s timeout; exempt agent-run events (`done`, `propose`) from the stdin read in both `events.hook` and `runs.hook`.
- A PreToolUse gate keyed on the tool payload alone (`tool_name` + `tool_input`) → PostToolUse carries the same payload, so the answered question's close was denied and its wait card stayed open; gate on the hook event (`notification` = PreToolUse here), not on the payload.

## actionable decisions (issue #17, 2026-10-08)

- Answering a Claude dialog by keystrokes (Escape + prompt) → fragile and interrupts work; a `PermissionRequest` hook runs in parallel with the pane's dialog and its `updatedInput.answers` answers an AskUserQuestion natively (verified live, also under `--permission-mode auto`), so answer through the hook and let whichever side answers first win.
- A `PermissionRequest` hook that exits 2 (argparse on an unknown subcommand, an older plugin) → Claude reads it as a deny; register it as `<cmd> permission || exit 0` so any failure leaves the dialog to the pane.
- `gh pr checks` (with or without `--required`) exits 1 with "no checks reported" on a repository without checks → a merge gate built on it refuses every merge there; gate on `gh pr view --json mergeStateStatus` and let `gh pr merge --match-head-commit` stay GitHub's authority.
- Switching a card to the `wake` delivery mode → Hermes sends no ping at all; make it conditional on muster being able to page the human itself (platform, bridge hook in the pane's settings, a fresh gateway heartbeat), or idle and old-pane waits go silent.
- A plain message to a Telegram group bot in privacy mode → never reaches the gateway (only replies to the bot, @mentions and commands do); after "Other", send a `ForceReply(selective=True)` that mentions the human, so their phone opens a reply bound to the question.
- PostToolUse's `tool_input` for an AskUserQuestion gains `answers` and other fields → a fingerprint of the whole input never matches the PermissionRequest's; fingerprint `questions` only.
- A kanban wake is an internal event and skips `pre_gateway_dispatch` → a plugin cannot swallow the woken agent's commentary there; skip the wake (no subscription when muster pages itself) and have the skill reply a bare `[SILENT]`, which Hermes drops on internal turns, when it has nothing new.
- A subagent's permission prompt (payload carries `agent_id`, `agent_type`) → Claude shows its dialog only after the PermissionRequest hook returns (2.1.295), so herdr never shows the pane blocked and a bridge that waits for `blocked` drops it; skip that check for subagents, and hold one only while the gateway is up.
- A muster pane runs `--permission-mode auto`, so a harmless command never prompts → to test prompts live, add a `permissions.ask` rule for one exact command to the throwaway worktree's ignored `.claude/settings.local.json` after launch; Claude reloads it live and the ask rule overrides auto mode.
- Herdr's `agent_status` is read off the screen, so a scrolled-back pane read `idle` while its dialog was still up, and the gateway staled a live request as "Answered in the pane". Claude sends the PermissionRequest hook SIGTERM when the pane answers first (2.1.295), so take that as the signal and never herdr's screen status.
- muster rebuilt Hermes surfaces it could have reused: its own guard, heartbeat, lock, typed-answer parser, message edits and outcome tracking. It also shipped permission prompts as numbered clarify buttons over JSON, because Hermes's approval card was "private". A simplicity review against Hermes's source cut about 230 lines. Now the rule is in AGENTS.md: approved surfaces first, then any unavoidable private use segregated in `muster/hermes_private.py` and tracked upstream (#20).
