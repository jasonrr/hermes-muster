# Plan: hardening batch (issue #7)

Approved design: the ten items of issue #7. Item 5 keeps herdr's native layout. `config.require()`
rejects two repos whose clone directories share a basename, and `worktree_path` is unchanged.
One commit per task, on `muster/7`.

Setup, done once: `uv venv .venv && uv pip install --python .venv/bin/python pytest`
(docs/learnings.md: the system python3 has no pytest). Each task's verify command is
`.venv/bin/python -m pytest -q <file>`. After the last task, run the full suite with
`.venv/bin/python -m pytest -q`.

Every stub of `core.run` in the tests is a `fake_run(argv)` that branches on `argv[:2]` or on
`argv[4]` (the kanban verb). New stubbed commands follow that pattern.

## Task 1: a wait reason is never read as a flag

- Files: `muster/events.py` (`open_wait`: `core.kanban("block", "--kind", "needs_input", "--", card, reason)`).
  Also the `block` branch of the fakes in `tests/test_events.py` and `tests/test_runs.py`: the card is the
  argument after `"--"` when present, else `argv[7]`. Also `tests/test_kanban_contract.py`.
- Test first: in `test_kanban_contract.py`, `k("block", "--kind", "needs_input", "--", card, "--kind=x boom")`
  succeeds and the card is blocked. In `test_events.py`, a notification whose message is `"--kind=capability"`
  blocks the wait card and the reason in the call starts with `--kind=capability`.
- Verify: `.venv/bin/python -m pytest -q tests/test_events.py tests/test_runs.py tests/test_kanban_contract.py`
- Exists because: hermes `block` is `task_id [reason ...]` (argparse), so a reason starting with `--`
  is parsed as an option, the block fails, and nobody is pinged. Every other block reason starts with fixed text.

## Task 2: durable writes; a bad file never wedges a card

- Files: `muster/core.py`. `save_json` writes with `open`, then `flush` and `os.fsync` before `os.replace`.
  `take_worktree`'s two `OWNER_FILE` writes become `save_json`. `seen` skips a line that fails `json.loads`.
  `muster/runs.py`: `drain` reads each outbox and sent entry inside a `try`. On `ValueError`
  (JSONDecodeError is one) it logs `"<card> <file name>: unreadable, dropped: <error>"`, unlinks the file
  and continues.
- Test first (`tests/test_runs.py`, with the `board`/`run1` fixtures): write `outbox/1-notification.json`
  containing `{"ev`, then a valid later event. `runs.flush()` delivers the later one, removes the bad file
  and logs "unreadable". Do the same for a bad file in `sent/`. In `tests/test_launch.py` or `test_core.py`,
  `core.seen(dir, sha)` returns True when a truncated last line follows the matching one.
- Verify: `.venv/bin/python -m pytest -q tests/test_runs.py tests/test_core.py tests/test_launch.py`
- Exists because: a file cut short by a crash makes `json.loads` raise outside the try. The flush logs it
  and stops, every minute, for good.

## Task 3: flush marks a run closed under the run's launch lock

- Files: `muster/runs.py` `flush`. Wrap the `"closed"` save in `with core.launch_lock(directory):`.
  If the lock is busy, `LaunchFailure` (a LaunchError, in ERRORS) is logged and the next flush tries again.
- Test first (`tests/test_runs.py`): a finished run, with `core.launch_lock(run1)` held around `runs.flush()`,
  is not marked `closed`. After release, the next flush marks it closed.
- Verify: `.venv/bin/python -m pytest -q tests/test_runs.py`
- Exists because: `runs.relaunch` saves run.json under `launch.lock`. A flush writing `{**load(), closed}`
  at the same moment can overwrite the launch record a recover just saved.

## Task 4: config refuses what it would later crash on

- Files: `muster/config.py` `require`. Reject a `label` that is not a non-empty str, a `board` that is not
  a non-empty str, and an `approver_id` that is not an `int` (a bool is not one) or is ≤ 0. Reject two
  `repos()` entries whose clone `.name` is equal, with the message `repos X and Y share the clone directory
  name <name>; give one a path with repos: owner/name=/other/dir`.
  `muster/core.py` `board_exists`: a `CommandError`, `ValueError` or `AttributeError` raises
  `config.ConfigError(f"muster: cannot list kanban boards: {error}")`.
- Test first (`tests/test_config.py`): parametrize label `""`, board `5`, approver_id `"7"`, `7.0` and
  `True`. Each raises ConfigError naming the key. `["a/tools", "b/tools"]` raises with the exact message.
  `["a/tools", "b/tools=/x/tools2"]` passes the check. (`tests/test_core.py`) A boards list that fails or
  prints non-JSON makes `tick()` print the ConfigError through `cli.main`, or raise ConfigError from
  `board_exists`.
- Verify: `.venv/bin/python -m pytest -q tests/test_config.py tests/test_core.py`
- Exists because: today these values pass `require` and crash later with a traceback from cron. Two repos
  that share a basename map to the same `worktree_path`, because herdr's layout is `<name>/<branch>`.

## Task 5: hook TypeError is logged; recover names a record without "launch"

- Files: `muster/events.py` `hook`: add `TypeError` to the caught tuple. `muster/core.py` `recover_card`:
  load into `loaded`, and if `"launch"` is missing, print `recover <card>: <path> has no launch record;
  remove and re-apply <label>` to stderr and return 1. Only after the check is `record` bound.
- Test first: in `test_events.py`, a `move` that raises TypeError makes `done` return 1 and write the
  events.log line. In `test_core.py`, `launch.json` `{"card": "t_x"}` gives `recover("t_x") == 1` and that
  message, with no traceback.
- Verify: `.venv/bin/python -m pytest -q tests/test_events.py tests/test_core.py`
- Exists because: a TypeError skips the log and the retry. In recover, the `except` handler itself reads
  `record["launch"]` and raises KeyError.

## Task 6: tokens are redacted where command errors are made

- Files: `muster/core.py`: move `SECRET` here from `cleanup.py`. `run()` puts `SECRET.sub("[redacted]", stderr)`
  into the CommandError. `muster/cleanup.py` uses `core.SECRET`.
- Test first (`test_core.py`): call `core.run` on a real `sh -c 'echo ghp_ABC123 >&2; exit 1'` and check
  the CommandError text has `[redacted]` and no `ghp_`.
- Verify: `.venv/bin/python -m pytest -q tests/test_core.py tests/test_cleanup.py`
- Exists because: CommandError text lands on card bodies (`setup_trouble`) and in logs. `gh` and `git`
  can echo a token in stderr.

## Task 7: `done` refuses a pull request from another branch

- Files: `muster/core.py` `links()` adds `"branch": rec["branch"]`. `muster/events.py` `hook`, done path,
  after the URL match: `head = json.loads(core.run(["gh", "pr", "view", url, "--json", "headRefName"]))["headRefName"]`.
  The expected branch is `link.get("branch") or f"{branch_prefix}{issue}"`. On a mismatch, print and log
  `done: <url> is from <head>, not <branch>` and return 1. If the check itself fails (CommandError,
  ValueError, KeyError, TypeError), print and log it and return 1.
- Test first (`test_events.py`): the fake answers `gh pr view` with `{"headRefName": board["head"]}`.
  A head of `muster/397` completes. `other` returns 1 and the card stays ready. A failing gh returns 1.
  Update `test_core.py`'s links assertion to include `"branch": "muster/397"`.
- Verify: `.venv/bin/python -m pytest -q tests/test_events.py tests/test_core.py`
- Exists because: `done` only checks that the URL's repo matches, so any PR URL of the repo completes
  the ledger.

## Task 8: a card left without a launch is launched by the next tick

- Files: `muster/core.py` `intake`. In the "card exists" branch, if `task.get("status") == "ready"` and
  `intake_dir()/task["id"]/"launch.json"` is not a file, fall through to `launch()`. Otherwise print the
  existing line and continue.
- Test first (`test_core.py`): `card_age=60` with no launch.json launches (one `herdr agent prompt`). Keep
  `test_an_existing_card_launches_nothing`, with a launch.json written first.
- Verify: `.venv/bin/python -m pytest -q tests/test_core.py`
- Exists because: a tick killed between `kanban create` and `launch()`'s first save leaves a ready card
  that every later tick skips. The board holds no cards from before launch records (confirmed by the human).

## Task 9: one log and lock helper; no unread version

- Files: `muster/core.py`: delete `LAUNCH_VERSION` and the `"version"` key in `plan`. Add `log_path(name)`
  (`data_dir()/logs/<name>.log`), `log(name, line)` (mkdir, append `"<ts> <folded line>"`) and
  `lock_path(name)` (`data_dir()/logs/<name>.lock`). `runs.log`/`log_path` and `events.log`/`log_path`
  become one-line wrappers, kept because tests and callers use them. `core.lock_path("tick")` replaces
  `core.lock_path()`, and `cleanup.lock_path` returns `core.lock_path("cleanup")`. Update the
  `tests/test_launch.py:92` version assertion.
- Test first: `test_events.py` asserts an events.log line starts with a timestamp (`\d{4}-\d\d-\d\dT`).
- Verify: `.venv/bin/python -m pytest -q`
- Exists because: nothing reads `version`, and two hand-rolled log writers have already drifted (events.log
  has no timestamp).

## After the last task

A fresh reviewer runs the full suite, probes each changed surface and traces the callers. Then add a
docs/learnings.md bullet for any surprise and open one PR against main ending `Closes #7`.

## Plan-attack findings applied

- T1: the `block` card is the arg after `"--"` and the reason is the one after that. Update the `c[8]`
  reason reads in test_events.py and the `block_text` helper in test_runs.py so both block forms parse.
  The capability and session-end blocks keep the old form. test_core.py:548 and test_cleanup.py:196 are
  unaffected.
- T2: guard only the `json.loads` of each entry. An entry that is not a dict or has no `"event"` counts
  as bad. Log it, unlink it, `continue`. `idle.json` gets the same treatment (unlinking it restarts the
  idle clock). `seen` catches `(ValueError, AttributeError)`. `prompt_seen` starts its line with `"\n"`
  when the file does not end in one, so a torn tail never swallows the next record. `closed()` reads
  `sent/` only after `drain` has dropped bad files.
- T3: `load(card)` moves inside the lock. A busy lock logs once per flush. The threat, kept because it is
  a stated requirement: a flush overwriting a run.json that a concurrent launch or recover just saved.
- T4: the `isinstance(int)` check replaces the `int()` check, and the failing keys join `missing`.
  `board_exists` also catches `TypeError`. The clone name check compares `.name.lower()` (APFS ignores
  case) and runs after the slug parse.
- T5: the `except` handler pops `"launch"` only from a dict.
- T6: redact the whole formatted CommandError message (argv can carry a token too).
- T7: refusals go to stderr with exit 1. The check runs before `move` and has no retry, so the agent
  reruns `done`. Limits: `headRefName` is a bare name (a fork's same-named branch passes), and an older
  links file falls back to the current prefix.
- T9: callers of `core.lock_path()` are core.py `start`, `recover` and `tick`, and test_core.py:412-413.
  Fix the record doc line naming `version`.
- After every task the full suite stays green.
