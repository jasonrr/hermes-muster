# Stale "is reviewing" replies: plan

Design (approved, Proposal v1 9b3abe36c1bb on t_18fb4d80): when a review starts, Muster wakes the coordinator with Hermes's
native `wake` delivery mode. That mode sends no ping. The run's status message already says
`🔍 <coordinator> is reviewing …` while the review runs (`progress.state`). The ledger keeps `notify+wake` until
Muster completes it with an open pull request, so a blocked ledger still pings.

Root cause (reproduced: `test_design_approve` fails at B with `✔ Demo [in:d1]: design v1 review 🔍 Ella is reviewing
design v1…` left in the thread). Hermes's notifier (`gateway/kanban_watchers_notifier.py` `_send_pings`, mode
`notify+wake`) posts a ping when any of these three cards completes:
`events.review` (design card), `decisions.rereview` (revised PR card), and the ledger in `adhoc.complete` /
`events.move("done")`. `wake` skips the ping and keeps the wake and its summary handoff (`_send_pings`:
`if not self.send_passive: continue`; `build_wake_text`). `notify-subscribe` is last-write-wins on
`--delivery-mode` for the same (card, platform, chat, thread) row (`hermes_cli/kanban_db_notify.py`).

Pattern for every subscribe: `core.subscribe(card, ledger, mode=...)` (`muster/events.py` `review`, the
existing `mode` parameter). Tests follow each module's own fake board (`tests/test_events.py` `board`, `tests/test_runs.py`
`board`).

## Task 1: review cards wake without a ping

Files: `muster/events.py` (`review`), `muster/decisions.py` (`rereview`), `tests/test_events.py`, `tests/test_runs.py`.

- Test first: in `test_each_proposal_version_wakes_the_coordinator_once_in_the_run_topic`, assert the design card's
  `--delivery-mode` is `wake`. In `test_a_revised_pull_request_after_a_send_back_makes_a_ready_for_re_review_card`,
  assert `revised["mode"] == "wake"`. Watch both fail.
- Change: pass `mode="wake"` in both `core.subscribe` calls. Update the `review` docstring: the status message
  carries "is reviewing", so there is no ping.
- Verify: `python3 -m pytest -q tests/test_events.py tests/test_runs.py`
- Exists because: those two cards produce the design and revised-PR pings.

## Task 2: the ledger stops pinging when it completes with an open PR

Files: `muster/adhoc.py` (`complete`), `muster/events.py` (`move`, `done`), `tests/test_runs.py`, `tests/test_events.py`.

- Test first:
  - `test_runs`: a verified open PR completes the ledger, and a `notify-subscribe <ledger> … --delivery-mode wake`
    call comes before `complete`. A merged PR makes no such call: its "Merged:" line is not a review notice and stays a ping.
  - `test_events`: `done` on an issue run calls `notify-subscribe t_abc123 … wake` before `complete`.
    Watch both fail.
- Change: add `core.switch_mode(card, old, new)`. It reads `notify-list`, and re-subscribes each row in mode `old`
  naming only its platform, chat, thread and `--delivery-mode new`, so every row changes in place, wherever it was
  subscribed. Right before the open-PR `complete`, call `switch_mode(card, "notify+wake", "wake")`. Right before
  each ledger `block` (`adhoc.deliver` and `events.move`, session-end), call `switch_mode(card, "wake", "notify+wake")`.
  So a completion that switched the ledger and then failed still pings on a later block.
  A failure raises before the move, and the outbox retries the event, as it does for any failed move.
- Verify: `python3 -m pytest -q tests/test_runs.py tests/test_events.py`
- Exists because: the ledger's completion is the "PR opened · is reviewing" ping. Its `notify+wake` is still
  needed until then, for a launch failure or a session-end block (learnings: a `wake` card sends no ping at all).

## Task 3: acks count wake subscriptions

Files: `muster/runs.py` (`acked`), `tests/test_runs.py`.

- Test first: `acked` is True for a `wake` row with `last_event_id >= want` and `last_ping_event_id == 0`. It stays
  False for a `notify+wake` row in that state, and False for a `wake` row whose `last_event_id < want`. Without
  this, a wake ledger or review card is never acked: the "not acked after 15 min" warning fires, and
  `runs.closed` waits for it. Monkeypatch `core.kanban` and `runs.last_event`. Watch it fail.
- Change: include `wake` rows; require a ping cursor only for `notify+wake`.
- Verify: `python3 -m pytest -q tests/test_runs.py`
- Exists because: Task 2 sets the ledger's mode to `wake`, and `adhoc.deliver` acks the ledger and the re-review card.

## Task 4: docs and the full check

Files: `docs/channels.md` (Slack section: no longer red), `docs/reference.md` (the PR-opened ping line, the
"Cards" `notify+wake` line), `docs/learnings.md` (one bullet).

- Verify:
  - `python3 -m pytest -q` and `ruff check .`.
  - Acceptance (`tests/acceptance/README.md` env, PYTHONPATH/PYTHONHOME cleared, `-p no:cacheprovider`):
    `MUSTER_ACCEPTANCE=1 $PY -m pytest -p no:cacheprovider tests/acceptance -q -rA`. Every lifecycle test must
    pass with its subtests executed.
  - `$PY tests/acceptance/gate.py`: expect every test to pass. A CONFIG MISMATCH on
    `display.platforms.slack.cleanup_progress` is expected. Report the key names.
- Exists because: the docs describe the old ping, and the brief requires these runs.

Plan review (sonnet, both passes) findings and outcomes:
- p1: `core.subscribe` at the current target misses a launch-time row at another target → `switch_mode` over every
  `notify-list` row. Fixed.
- p1: a ledger left in `wake` by a completion that then failed would block silently → restore before every ledger
  block. Fixed.
- p1: the existing counts of `notify-subscribe` at `test_events.py` 1013/1049 → those ledgers are already done, so no
  switch happens. They pass unchanged. Not a defect.
- p2: the fakes keep one global mode → `modes` per card in both `board` fakes. Fixed.
- p2: Telegram `move_cards` re-subscribes cards `notify+wake`. If it runs between the switch and the notifier's claim of
  the completion, the ping returns: today's behavior, no loss. Parked.
- p2: a ledger `blocked` event not yet pinged when the switch lands loses its ping (the wake still runs). That needs a
  block, then a resumed builder, then `done`, all within one notifier tick, and `done` supersedes the block. Parked.
- p2: `tests/fake_hermes.Subs` appends instead of upserting → no path that uses it calls `switch_mode`. Parked.

