# Design: hold possible duplicate error issues; retire closed runs (issue #34) — v3

## Problem (verified)

- radicalcandorai #493, #494 and #495 were opened by Sentry 5 s apart (23:34:11–16Z) with the auto-approval labels.
  One tick launched three builders 17–30 s apart (the tick lock serializes intake). Nothing checks for siblings.
- The issue bodies carry no trace or request id. #494 is only `ApiError: API Error 500`. Muster has no Sentry
  access. Muster cannot compute correlation; it can only notice reports arriving close together and ask.
- Cleanup removes a worktree only when its PR is merged, so closed-unmerged runs strand. The canonical PR #497 comes
  from branch `muster/494`, whose issue is closed `not_planned`. Retirement therefore cannot key on the issue alone.

## 1. Hold before dispatch (tick): a conservative throttle, not deduplication

**Rule.** An automatic approval is held when the same repository has an earlier live automatic run, launched or
held. "Earlier" and "live" are defined below. Whether a held issue is a duplicate is the human's call. The Hermes
agent advises, because the card wakes it with `muster:escalation`. Muster never merges reports by itself. The rule
holds unrelated automatic issues in the window too. That is accepted as an initial safety throttle; the 60 min is a
module constant, to be tuned from real false holds.

**Stable earlier candidates.**

- A candidate must precede the new issue by (created_at, number), strictly. It is never the card itself.
- A candidate's ledger is not archived, and its issue was created within 60 min before this one.
- Intake processes a repository's issues in (created_at, number) order; today it sorts by number.
- `held.json` persists the candidates found at hold time. The first is named the likely original.
- The list is never re-evaluated. A hold is a human decision, not a lock: two held cards never keep each other
  blocked, and releasing one never releases another.
- When every candidate's ledger reads `archived`, the next tick adds one comment to the held card: "the earlier
  run(s) are retired; still not dispatched". That is the only effect: the card stays held until the human acts.
- Known consequence of "earlier only": an earlier-created issue never waits on a later one. If labels arrive out of
  order across ticks, a later-created issue dispatched first does not hold an earlier one.

**What the human sees.** The card is blocked `needs_input`: "Possible duplicate; not dispatched. repo#495 arrived
within 60 min after repo#493 (card X, likely original), repo#494 (card Y). To launch it: `hermes muster recover
<card>`. If it is a duplicate, close the issue on GitHub."

**Notifications (bounded).**

- One initial notice: the block event, which is one ping plus one Hermes agent wake.
- One reminder at 24 h if the hold is unresolved. It is a single card with idempotency key
  `muster-hold-reminder:<card>`, subscribed and blocked once. It is archived when the hold resolves.
- Nothing more. The held card's blocked status on the board is the standing "unresolved" record.
- Candidates get one ledger comment naming the new report. Comments do not ping (Hermes notifies on state events
  only).

**Durable resolution.** `held.json` carries `resolution`: absent (held), `released` or `closed`, each with its time.
Every write to `held.json`, whether from the tick or from recover, is made under the card's `launch_lock`. The tick
takes that lock non-blocking and skips the card this tick when it is busy, so release and reconciliation never
interleave.

*Release.*

- `recover <card>` takes the lock and re-checks the approval: the issue is open and labeled, and its newest
  approving event is the held one.
- It then writes `released`, archives the reminder card if there is one, unblocks the card, and launches.
- A `released` hold gets no further hold step: no block, no reminder, no comment. Reconciliation only re-archives a
  reminder that a crash left behind.
- A crash after `released` with no `launch.json` leaves `recover` re-runnable: it accepts a released hold, whether
  blocked or ready.
- A tick that finds a released card `ready` launches it. This is the same path as today's resume after a crash
  between create and launch.
- A launch failure blocks the card, as any launch failure does.

*Closed.*

- An issue closed for any `state_reason` makes the tick write `closed`.
- The tick then comments once ("not dispatched: issue closed (<state_reason>)"), checking for its marker first.
- It archives the held card and its reminder.
- Each step checks state first, so a crash resumes where it stopped.

**A reopen alone never authorizes.** Hermes's idempotency key returns only a non-archived card. Today, a reopened
issue whose newest approving label event is unchanged would get a fresh card. So intake skips an issue whose
(repo, issue, approving event id) has a record resolved `closed` (a hold) or `retired` (cleanup, below). It says
"approval event <id> already resolved; a new label event is needed". Only a new `labeled` event of the approving
label authorizes a new card. That event must come from the approver, or from the auto-approver bot under its
existing rule.

**Idempotent setup.** `held.json` is written atomically first. It records each finished step: `subscribed`,
`commented: [ledgers]`, `blocked`, `reminded`. Every tick runs `reconcile_hold(card)` for each `held.json` whose card
is not archived. Each step checks state before it acts:

- **Subscribe:** `notify-list` already shows the subscription.
- **Comment:** the candidate ledger's comments already contain the marker `hold <card>`. The flag is written after
  the comment.
- **Block:** only if the card is `ready` and the hold is unresolved.
- **Remind:** only if the hold is unresolved, and the idempotency key prevents a second card.

A crash between any two steps therefore repeats nothing. A card whose create returned but has neither `held.json`
nor `launch.json` is still `ready`. The tick recomputes the rule for it: held if earlier candidates exist,
otherwise launched as today.

## 2. Retire closed runs (cleanup)

**Gate.** A worktree may also be removed when its ledger reads `archived` and no PR from its branch is open. Every
existing guard still applies:

- identity: the card file's repo, path and workspace;
- clean;
- pushed: dirty or unpushed work goes to the existing "Unsaved work" card;
- 30 min quiet: no agent running, and no process under any pane shell other than helpers. A pending
  PermissionRequest hook is such a process.

**No active owner.** This is checked in the check and again in the recheck immediately before removal:

- no `open`, `answered` or `executing` decision request for the ledger;
- for ad-hoc runs, no pending outbox entry;
- the launch lock, taken non-blocking for the removal itself.

**Fail closed.** Unknown evidence keeps the worktree, and the cleanup log says why. Unknown means any of these:

- The PR lookup is not a complete success. It must be a paginated `gh api` call that returns a JSON list. An error,
  a timeout or unparseable output all count as unknown.
- A decision file in `decisions/` cannot be read. The cleanup path reads strictly, where the gateway path logs and
  skips such a file.
- The outbox cannot be read.
- Process or pane inspection fails.
- The archived status cannot be read: only an explicit `archived` counts, never "card missing" (the learning from
  #28).
- Ownership cannot be established: an unreadable card file, or a workspace or path mismatch.

For an archived ledger, cleanup also posts one notice card per blocker state.

**External closure.** This applies when a run's issue is closed with `state_reason` `not_planned` or `duplicate`
and no PR from its branch is open. Both values are verified in the REST API; GraphQL's `DUPLICATE` is `duplicate`
in REST. On a complete lookup:

- Cleanup marks the record `retired` (launch.json).
- It comments neutrally on the ledger with the actual reason: "retired: repo#n closed (not_planned); PRs #a, #b
  closed unmerged; no open PR; branch kept".
- It archives the ledger.

Each step checks state first. A `completed` closure, or any open PR, retires nothing. #495's run would be retired;
#494's run (PR #497 open) would not.

**Stranded runs.** A quiet run whose PRs are all closed unmerged, while its issue is not closed `not_planned` or
`duplicate`, gets one notice through the existing `escalate` path, once per state. That is the #493 case: "PR #496
closed unmerged; archive ledger <card> to retire this workspace, or keep it."

## Cut, and why

- **Automatic correlation:** muster cannot read the data it would need, and a wrong merge loses an incident.
- **Telegram release buttons:** these need a new decision kind. `recover` and closing the issue already exist.
- **Holding human-labelled issues:** the human judged each one when labelling it.
- **Writes to the canonical GitHub issue, and a new ledger state:** `archived` plus a comment is Hermes's native
  terminal state.

## Safety boundaries

- No merge, push, branch delete, PID kill, or `--force` removal.
- The canonical run is untouched while its PR is open.
- Nothing is marked done, merged or deployed.

## Test plan (mocked)

- **Replay #493/#494/#495 in one tick:** one launch and two holds with earlier-only candidates. A repeat tick adds
  nothing. In an out-of-order replay (the issue list returned unsorted, and recovery of a card with no hold
  record), self and later issues are never candidates.
- **Crash injection after each hold step** (held.json, subscribe, each comment, block): a resumed tick never
  launches, never comments twice and never blocks twice.
- **Release:**
  - Recover launches.
  - Release racing reconciliation (lock held): the tick skips, then never blocks, reminds or comments.
  - A crash after `released`: recover and tick both resume to one launch.
  - A pending reminder is archived on release.
- **Close:**
  - The candidate retired: one comment.
  - A closed held issue: archived.
  - A reopen with the same approving event: skipped. A new labeled event: a new card.
- **Reminder:** exactly one at 24 h, and none after resolution.
- **Window:** the same title outside the window launches. A human-labelled issue inside the window launches.
- **Cleanup removes** an archived ledger with a closed PR, clean and quiet.
- **Cleanup keeps it, giving a reason,** when any of these holds:
  - the checkout is dirty (with a warning card);
  - a PR is open;
  - a decision is open, answered or executing;
  - the launch lock is held;
  - identity does not match, or the workspace id was reused;
  - the PR lookup errors or returns non-JSON;
  - a decision file is unreadable;
  - the outbox is unreadable;
  - process inspection errors.
- **Cleanup closure cases:**
  - Issue closed `not_planned` or `duplicate` with no open PR: the record is marked retired, the ledger is commented
    and archived, and a reopen is not relaunched.
  - #494's shape is untouched.
  - #493's shape gets one notice.
  - An archive failure is retried.
  - A workspace that is already gone is a no-op.

## Decision needed

Approve v3.
