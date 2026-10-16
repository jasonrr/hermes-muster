# Plan: hold possible duplicate error issues; retire closed runs (issue #34)

Design: `docs/plans/2026-10-16-incident-dedupe-design.md` (v3, approved 2026-10-16). Read `docs/learnings.md`
first, especially #5 (outbox: save first, every move reads state), #28 (only an explicit `archived` retires) and
"Hermes idempotency returns only a non-archived card" (kanban_db.py:1201).

Verify everything with: `. .venv/bin/activate && python -m pytest -q` (create the venv per learnings if absent).
Commit each task with explicit paths, no trailer.

## Task 1: `muster/hold.py`: the rule and the hold setup

**Files.** New `muster/hold.py`, `muster/core.py` (`launch`, `intake`), new `tests/test_hold.py`.

**Exists because:** #493/#494/#495 launched three builders in one tick (design §1).

**Nearest patterns.**

- `core.launch` (record, then `save_json`, then `topic`, then `subscribe`): the order and the error net.
- `cleanup.escalate`: creates the card with an idempotency key, then `subscribe`, then a block only if `ready`.
- `runs.enqueue`: an atomic json write.

### Test first (`tests/test_hold.py`)

Use a stateful fake: a board dict keyed by idempotency key, issues with `created_at`, and `World` from
`fake_herdr`. Model it on `test_core.fake_world`, but with three Sentry issues in one repository, using the
`sentry_rule` fixture from `test_core.py` (move it to `conftest.py` if needed).

1. **Replay:** issues 493/494/495, created 23:34:11/13/16Z, returned by the API in order 495, 493, 494.
   - One `herdr worktree create` (for 493).
   - The holds: 494's candidates are [493]; 495's are [493, 494]. Self and later issues are never in the list.
   - Each held card is blocked `needs_input` once, with text starting "Possible duplicate; not dispatched."
   - 493's ledger has exactly one comment containing `hold <card494>` and one containing `hold <card495>`.
2. **A second tick** adds no create, block, comment or launch.
3. **Window:** 495 created 61 min after 493 launches. A human-approved issue inside the window launches.
4. **Crash injection** (`World.crash_after`, or a `fail_on` that raises KeyboardInterrupt) after each of: the
   held.json write, notify-subscribe, the first candidate comment, and the block. The next tick reaches the same
   end state, with exactly one comment per candidate and one block.
5. **A ready card with no held.json and no launch.json** (a tick killed after create) is held, not launched,
   when earlier candidates exist.

### Code

- **`launch()`:** add `"created": issue.get("created_at")` to the record. This is a one-line change; old records
  lack it and are never candidates.
- **`hold.live(repo)`:** yields records from `core.intake_dir()/*/launch.json` and `*/held.json` where
  `rec["repo"] == repo`, `rec.get("auto")` is set, `rec.get("created")` is set, and the hold resolution is not
  `closed`.
- **`hold.candidates(repo, issue, card)`:** sorted by (created, issue). Keep records where (created, issue) is
  strictly before (issue.created_at, number), created >= this created − `WINDOW` (3600 s), the card is not this
  card, and `events.status(ledger) != "archived"`. An unreadable status counts as live, because holding is the safe
  side.
- **`hold.hold(repo, issue, card, event, auto, cands)`:**
  1. Write `held.json` atomically (`core.save_json`) with: card, repo, issue, title, event id, auto, created,
     candidates, `held_at`, `steps: {}` and `resolution: None`.
  2. Run `reconcile(card)` under `core.launch_lock(core.intake_dir()/card)`.
- **`hold.reconcile(card)`:** reads `held.json`. While it is unresolved:
  1. Subscribe, skipped if `steps.subscribed`. `core.subscribe` already reads back. Then set the flag.
  2. Comment on each candidate ledger not in `steps.commented`. First read `kanban show <ledger> --json`
     comments; skip the comment if one body contains `hold <card>`. Otherwise write
     `hold <card>: possible duplicate report <repo>#<n> held, not dispatched`. Then append the ledger to the list.
  3. Block `needs_input` only if `events.status(card) == "ready"`, using `--` before the positionals (learnings,
     #7). Then set `steps.blocked`.

  Each flag is written immediately after its step.
- **`intake()`:**
  - Sort issues by `(created_at, number)`.
  - Before the create, call `hold.resolved(repo, number, event_id)`. It is true when a held.json resolved `closed`
    or a launch.json `retired` has that (repo, issue, event). When it is true, print "approval event <id> already
    resolved; a new label event is needed" and continue.
  - After the create, when `held.json` exists for the card: `continue`. The sweep in Task 2 owns it.
  - Keep the existing "card exists" check.
  - Then, if `auto`, compute `hold.candidates`; if there are any, call `hold.hold` and `continue`.
  - Dry run: print "would hold" plus the candidates, and write nothing.

**Verify:** `python -m pytest -q tests/test_hold.py tests/test_core.py`.

## Task 2: hold resolution (closed, released, retired candidates, reminder)

**Files.** `muster/hold.py`, `muster/core.py` (`recover`, `recover_card`, `intake`), `tests/test_hold.py`.

**Exists because:** without a durable resolution, reconcile would re-block a released card, and a closed held
issue would stay blocked forever (design §1, durable resolution).

**Nearest patterns.**

- `core.recover_card`: approval re-check, unblock once.
- `conversation`'s state swaps under a lock.

### Test first

1. A held issue closed `duplicate` → `resolution.closed` with the reason. One comment "not dispatched: issue
   closed (duplicate)". The card is archived, and so is the reminder if there is one. Rerun: no new calls. A crash
   after the resolution write → the next tick finishes the comment and the archive without repeating them.
2. A reopen of that issue with the same approving event → no create. A new labeled event → a new card under the
   normal rules (here: held or launched by the rule).
3. `recover <held card>`:
   - Approval unchanged → `released` is written, the card is unblocked, and the issue launches with the normal
     brief and the bug flag.
   - Revoked approval → refused, and the card stays held.
   - With the launch lock held by a sweep (simulated by holding `launch_lock`) → the recover reports busy.
   - A sweep while the recover holds the lock → it skips the card.
   - After release, ticks never block, comment or remind.
4. Crash after `released` with no launch.json:
   - Card blocked → recover is re-runnable and launches once.
   - Card ready → the tick launches once.
5. All candidates archived → exactly one comment "the earlier run(s) are retired; still not dispatched". The
   card stays blocked.
6. Reminder (advance `hold.clock`):
   - At 23 h: none.
   - At 24 h: one card keyed `muster-hold-reminder:<card>`, subscribed with ledger=card and blocked once.
   - At 48 h: nothing new.
   - After a release or close: the reminder is archived.

### Code

- **`hold.sweep(repo)`:** at the start of `intake(repo)`, for every `held.json` of the repository whose card is
  not archived, or whose resolution needs finishing:
  - Take `core.launch_lock` non-blocking; skip the card on `LaunchFailure("busy")`.
  - Fetch the issue (`gh api repos/<repo>/issues/<n>`).
  - Then, in order:
    - **Closed issue, unresolved:** write `resolution {state: closed, reason: state_reason, at}`.
    - **Resolution `closed`:**
      1. Comment, marker-checked with `not dispatched <card>`.
      2. Archive the card unless it is already archived.
      3. Archive the reminder unless it is already archived.
    - **Resolution `released`:**
      1. Archive the reminder if one is open.
      2. If there is no launch.json and the card is `ready`, call `core.launch(repo, issue, card, event, auto)`.
         `event` is the `{"id": ...}` from held.json.
    - **Unresolved:**
      1. `reconcile(card)`, the setup steps.
      2. If every candidate's ledger is `archived` and `steps.retired_noted` is unset: comment (marker-checked),
         then set the flag.
      3. If `clock() - held_at >= 86400` and `steps.reminded` is unset: create the reminder, `core.subscribe`
         it, block it if it is `ready`, then record its id.
- **`core.recover`:** route a card that has `held.json` and no launch.json to `hold.release(card)`, under the tick
  lock as today. `hold.release`:
  1. Under `launch_lock`, re-check the approval: issue open, labeled, and `core.approve` event id ==
     held.json event.
  2. Write `released`.
  3. Archive the reminder.
  4. Run `kanban unblock` if the card is `blocked`.
  5. Release the lock, then call `core.launch` (it takes the lock itself).

  `launch`'s readback requires `ready`, which the unblock provides.

**Verify:** `python -m pytest -q tests/test_hold.py tests/test_core.py`.

## Task 3: cleanup: the archived gate, no active owner, fail closed

**Files.** `muster/cleanup.py` (`coding`, `merged_gate` caller, `step`), `muster/decisions.py` (a strict read),
`tests/test_cleanup.py`.

**Exists because:** closed-unmerged runs strand forever under a merged-only gate (#496/#498 workspaces w1K, w1N).

**Nearest patterns.**

- `cleanup.coding`: the fingerprint, warning and gate.
- `step`: the recheck before removal.
- `decisions.for_ledger`.

### Test first (`tests/test_cleanup.py` World)

Add `self.cards["t_1"]` as the ledger, and a closed-unmerged PR.

1. Ledger `archived`, PR closed unmerged, clean, pushed, quiet → removed after 30 min, and the topic finishes.
2. Each of the following keeps the worktree and logs a reason:
   - ledger `done` or `blocked` (not archived), or `show` fails;
   - an open PR;
   - a decision request for `t_1` that is `open`, `answered` or `executing`;
   - an unreadable decision file;
   - a pending outbox file in `runs_dir()/t_1/outbox`;
   - the launch lock held at removal time (`core.launch_lock(core.intake_dir()/"t_1")`);
   - the PR lookup raising or returning non-JSON;
   - `ps` failing;
   - a card file naming another workspace (a reused id).
3. Dirty and archived → the existing "Unsaved work" card, never a removal.
4. Archived, but the ledger is unarchived during the window → kept (the fingerprint includes the status).

### Code

- **The PR lookup:** add `--paginate --slurp`, then flatten. Anything that is not a list of dicts raises
  `NotReady("pull request lookup incomplete")`.
- **`coding()`:**
  - Compute `retire`: whether the ledger is known (`link["card"]["card"]`, or a run record's ledger),
    `events.status(ledger) == "archived"`, and no PR is open. An exception from `status` is a NotReady.
  - Then call `owner_free(ledger)`: `decisions.for_ledger(ledger, strict=True)` must hold none with status in
    `decisions.OPEN`, and `runs.pending(ledger)` must be empty. Any failure is a NotReady.
  - The gate is `None` if `merged_gate(...)` is None, or if `retire` holds.
  - Add the archived status to the fingerprint digest.
- **`decisions.read_all(archive, strict=False)`:** with `strict=True`, raise on an unreadable file instead of
  logging it. `for_ledger` passes the flag through.
- **`step()`:** wrap the clear+remove block in `core.launch_lock(<ledger dir>)` when the target has a ledger.
  Busy → "kept, a launch or recover holds the run", and not a failure. The ledger dir is `runs.run_dir(ledger)`
  if that has `run.json`, else `core.intake_dir()/ledger`.

**Verify:** `python -m pytest -q tests/test_cleanup.py tests/test_decisions.py`.

## Task 4: cleanup: external closure and stranded notice

**Files.** `muster/cleanup.py`, `tests/test_cleanup.py`.

**Exists because:** #495's issue was closed `not_planned` and its PR closed unmerged, but nothing retires its run.
#493's run (PR #496 closed, issue open) needs a human signal (design §2).

### Test first

1. Issue 15 closed `not_planned`, PR closed unmerged, ledger `done`:
   - `launch.json` (in `intake_dir()/t_1`) gains `retired`.
   - One ledger comment "retired: o/r#15 closed (not_planned); PRs #16 closed unmerged; no open PR; branch kept".
   - The ledger is archived. The worktree is removed after the quiet window (Task 3's gate).
   - A rerun repeats nothing. A failing archive is retried on the next run.
2. `duplicate` behaves the same. `completed`, or an open PR, retires nothing.
3. #494's shape (issue `not_planned`, PR open) → untouched.
4. #493's shape (issue open, every PR closed unmerged, quiet, ledger not archived) → one escalate card "PR #16
   closed unmerged; archive ledger t_1 to retire this workspace, or keep it". Same state again → no second card.
5. Dirty and closed `not_planned` → the existing Unsaved-work card; still archived (nothing is removed).

### Code

In `coding()`, for an issue link only, when the PR lookup is complete and no PR is open, fetch the issue
(`gh api repos/<repo>/issues/<n>`). A failure is a NotReady.

- **`state_reason` in (`not_planned`, `duplicate`) and the ledger not archived:** return a `retire` action.
  `step()` performs it (never in dry run), only after the quiet window, with each part read first:
  1. Mark `launch.json` `retired`.
  2. Comment, marker-checked with `retired:`.
  3. Archive.
- **Otherwise, all PRs closed unmerged and the ledger not archived:** a warning-style notice through `escalate`.
  Use the signature `digest([link, "closed-unmerged", prs_seen])` and the body text from the design.

**Verify:** `python -m pytest -q tests/test_cleanup.py`.

## Task 5: docs

**Files.** `README.md` (How work flows, Cleanup), `docs/learnings.md`.

**Exists because:** the README is the operator manual: hold, release and retire are new operator actions.

- **README:**
  - A "Held issues" bullet: the rule, the 60 min window, the message, `recover` to release, close the issue to
    drop, one reminder at 24 h.
  - The Cleanup paragraph: the archived gate, external closure, the stranded notice, fail closed.
- **learnings:** add a bullet for each surprise found while building.

**Verify:** `python -m pytest -q && hermes plugins validate .`.

## Plan attack fixes (two fresh reviews, 2026-10-16)

These override the tasks above wherever they conflict.

### Hold mechanics

- **Block kind:** a held card is blocked with no `--kind` (generic). Two same-kind blocks around an unblock send a
  card to triage (test_kanban_contract.py:58), and the run later blocks `needs_input` (session end) or
  `capability` (launch failure). A contract test covers generic, then unblock, then `needs_input`: the card stays
  blocked.
- **Locks:**
  - Every `held.json` write happens under `core.launch_lock(intake_dir()/card)`, including the first one.
  - `core.launch` takes that lock itself, so the sweep and `release` decide under the lock, leave the `with`, then
    call `launch`.
  - Recover already holds the tick lock, so busy is reachable only from cleanup or a manual lock. Tests hold the
    lock directly.
- **Errors:** every held card in the sweep sits in its own `except (*core FAILURES, core.LaunchError)`. A failure
  sets `failed` and the run carries on. In a dry run the sweep only prints "would ..." and writes nothing.
- **`held.json` schema:** `{card, repo, issue, title, event, auto, created, candidates: [{card, issue, created}],
  held_at, steps: {subscribed, commented: [], blocked, retired_noted}, reminder: <card id>|null,
  resolution: null|{state, reason, at}, finished: bool}`.
  - `finished` is set once a resolution's side effects are complete. The sweep skips finished holds.
  - A null `state_reason` is shown as "unspecified".
- **Markers** carry a delimiter: `hold <card>:`, `not dispatched <card>:`, `retired <card>:`.
- **Release:**
  - It refuses unless `resolution` is null or `released`, and the card reads `blocked` or `ready`.
  - `auto` comes from a fresh `core.approve`; the issue comes from a fresh `gh api` fetch.
  - It prints "recovered … (first prompt: …)" and returns 0, or returns 1 if `launch` returned None.
  - Routing in `core.recover`: after the `run.json` check, before `recover_card`.
- **Archived held card:** a held card that reads `archived` without a resolution (dismissed by hand) is resolved
  `closed`, reason "archived", so `resolved()` keeps intake from re-creating it.
- **Subscription after release:** the held card is subscribed to the main chat. After a successful launch with a
  topic, release unsubscribes the main-chat row, which `notify-list` then confirms. This is the pattern of
  gateway.py:999.
- **Candidates:**
  - `live()` dedupes by card and filters by repo and window before any `kanban show`.
  - A `released` hold counts as live.
  - ISO times are parsed as cleanup.py:310 does.
  - The window is measured from issue `created_at`, never from launch time.
- **Clock:** `hold.clock = time.time` provides `WINDOW = 3600` and `REMIND_AFTER = 86400`.
- **`intake` order:**
  1. `approve`.
  2. `resolved()`, which skips the issue.
  3. The dry-run print.
  4. Create.
  5. If `held.json` exists: `continue`.
  6. The existing card-exists check.
  7. The hold rule.
  8. Launch.

  `created_at` is read with `.get`.
- **Reminder card:** created with `--created-by muster`, idempotency key `muster-hold-reminder:<card>`, and the title
  "Still held: <short>#<n> not dispatched". It is subscribed with ledger=card and blocked generically once.
- **Fakes:** a new stateful board fake in `tests/test_hold.py`, which serves:
  - create keyed by idempotency key, ignoring archived cards as Hermes does;
  - show with comments, comment, block (refusing a second block), unblock, archive;
  - notify-subscribe/list/unsubscribe, reusing `fake_hermes.Subs`;
  - `gh api` routed separately for issues lists, `/issues/<n>` and `/timeline`;
  - a `World` for launch.

  Sentry constants are duplicated in the test file.

### Cleanup mechanics

- **Ledger status:** read only when `merged_gate` is not None and a ledger is known, so the merged path is unchanged.
  `coding()` gains a `ledger` parameter: `link["card"]["card"]` for issue runs, the record directory for ad-hoc
  runs, None for `coding` records.
- **`owner_free(ledger)`:**
  - It reads `decisions/` strictly, with no archive scan, since only terminal requests are archived.
  - It requires `runs.pending(ledger)` to be empty.
  - It raises NotReady on any error.
  - It is required for both the archived gate and the external retire.
- **PR lookup:** `gh api --paginate --slurp`. The result must be a list of lists of dicts, which is flattened;
  anything else is NotReady. The fake returns `[pulls]`.
- **External retire:**
  - It is carried in the target dict, the way `copies` is, never in the check tuple.
  - `step()` runs it only after the quiet window, after the warning branch, and never in a dry run. A dirty
    worktree gets the Unsaved-work card and is not retired until it is clean.
  - Under the ledger lock it writes `launch.json["retired"] = {reason, at}`, if the record has a `launch` dict.
    Then it comments (marker-checked), then archives.
  - The fingerprint then changes, so removal follows one more quiet window.
  - With zero PRs the comment says "no pull request".
- **Stranded notice:** `escalate()` gains `title`, `body` and `block` parameters with the current defaults.
- **Removal lock:** `step()` runs the recheck and the removal inside the ledger lock. The lock directory is
  `runs.run_dir(ledger)` if `run.json` exists, else `intake_dir()/ledger`, and only if that directory exists.
  `LaunchFailure` is caught: "kept, a launch or recover holds the run", with the quiet record kept.
