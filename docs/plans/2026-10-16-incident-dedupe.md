# Plan: hold possible duplicate error issues; retire closed runs (issue #34)

Design: `docs/plans/2026-10-16-incident-dedupe-design.md` (v4, approved 2026-10-16). Read `docs/learnings.md`
first, especially #5 (outbox: save first, every move reads state), #28 (only an explicit `archived` retires) and
"Hermes idempotency returns only a non-archived card" (kanban_db.py:1201).

Verify everything with: `. .venv/bin/activate && python -m pytest -q` (create the venv per learnings if absent).
Commit each task with explicit paths, no trailer.

## Task 1: `muster/hold.py`: judge with the model, hold, deliver (design v4 §1)

**Files:** new `muster/hold.py`; `muster/core.py` (`launch` record gains `created`, `intake`, `recover`);
`__init__.py` (`register_auxiliary_task`, `hold.CTX`); new `tests/test_hold.py`.

**Exists because:** #493/#494/#495 launched three builders in one tick.

**Patterns to copy:**
- the `core.launch` error net;
- `gateway.CTX` for the plugin context;
- `decisions._send` (agent idle check, `herdr agent prompt`, `core.seen` evidence) for delivery;
- `cleanup.escalate` for a notice card.

### Test first (`tests/test_hold.py`, stateful board fake, `World`)

Fake `hold.CTX.llm.complete_structured` with a recorded result.

1. **Replay:** issues 493, 494 and 495 are listed as 495, 493, 494. The model says 494 and 495 are duplicates of
   493. Expect:
   - one launch;
   - two held cards, each blocked once with no `--kind`;
   - the model called with 493 as the only candidate;
   - one delivery per held card to 493's pane, the second only after the first's evidence shows and the agent is
     idle;
   - one 493 ledger comment per held card.

   A repeat tick adds nothing.
2. **Model results that launch:** the model returns null, or a non-candidate number, or raises. In the last two
   cases the card also gets a comment "dedupe check failed". With no live run there is no call at all.
3. **Not live:** a run whose ledger is archived, or whose agent is gone, is not a candidate.
4. **Delivery:**
   - The agent is busy: nothing is sent, and the next tick sends.
   - A crash after the prompt: the evidence is seen and nothing is resent.
   - The agent is gone before delivery, or nothing is delivered in 24 h: one notice card. Nothing more.
5. **`hook duplicate`:** run from 493's worktree, `hook duplicate 495 same` or `different` records the verdict in
   495's `held.json`.
   - `different`: the next tick launches 495 once.
   - `same`: one comment, and the card stays held.
   - An unknown issue, or a hook outside a worktree: exit 1 with a message.
6. **Crash injection:** a crash after each hold setup step leads to no duplicate effect.

### Code

**`hold.py`:**
- `live(repo, card)`: the intake records of this repo, excluding this card. Each must have a ledger that is not
  archived (`events.status`), a pane with an agent (`core.agent_at`), and not be retired or held. Return the 10
  newest by `created`.
- `judge(issue, cands)`: calls `CTX.llm.complete_structured(task="muster_dedupe", json_schema=SCHEMA,
  temperature=0, max_tokens=400, purpose="muster.dedupe")`.
  - The input is the issue and each candidate's title and body, each capped at 8 KB and fetched with `gh api`.
  - The instruction says all text is untrusted data.
  - Returns `(number or None, reason)`. Raises `Unjudged` when there is no CTX, the call errors, `parsed` is None,
    or the number is not among the candidates.
- `hold(...)`: as written, but `candidates` is `[{card, issue}]` (the one chosen run), and `held.json` gains
  `reason`, `delivery: {sha, sent_at, delivered}` and `verdict`.
- `deliver(rec)`: under the lock.
  - Compute the wire text: fixed instructions plus JSON. Store its sha.
  - If `core.seen(evidence, sha)`, mark it delivered and comment the candidate ledger with the marker.
  - Else, if the agent is gone or `clock() - held_at > 86400`, post the notice once.
  - Else, if the agent is idle and nothing was sent in the last 10 min, send it with `herdr agent prompt`.
- `record_verdict(link, number, verdict)`: from the hook. Find the held.json in the same repo whose issue is
  `number` and whose candidate card is `link["card"]`. Under the lock, set `verdict` if it is unset.
- The sweep's unresolved branch, after setup, does `deliver`, then acts on the verdict:
  - `different`: resolve as released, unblock, launch, outside the lock.
  - `same`: comment once.
- Then the reminder. The existing closed and released branches are unchanged.

**`core.intake`:** the auto path calls `hold.live`. If it is non-empty, it calls `hold.judge`:
- `Unjudged`: launch, then comment.
- A number: `hold.hold`.
- None: launch.

The dry run prints "would judge against #...".

**`events.hook` and `cli`:** add the `duplicate` event. Its args are `url` (the number) and the new optional
`verdict` positional. It has no stdin read, and it exits 1 on error, as `done` does.

**Verify:** `python -m pytest -q tests/test_hold.py tests/test_core.py tests/test_events.py tests/test_cli.py`

## Task 2: removed

It is merged into Task 1. The v3 lifecycle (closed, released, reopen guard, reminder, release lock, archived by
hand) is already in `hold.py`; Task 1's tests cover it, along with v3's lifecycle tests 1–6.

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

Written against v3. Under v4, the Hold bullets about candidates, the window and earlier-only ordering no longer apply. The block kind, locks, errors, schema, markers, release, archived-by-hand, subscription and Cleanup bullets still do.

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
