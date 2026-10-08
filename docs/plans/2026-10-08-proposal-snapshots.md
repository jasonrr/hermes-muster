# Plan: proposal snapshots on cards (issue #14)

Approved design (2026-10-08, with three changes after reading #17): the agent saves its plan/design
with `hermes muster hook propose <file>` before asking for approval. Each distinct text is a numbered
version `Proposal v<n> <sha256[:12]>`, kept in `<data dir>/runs/<ledger>/proposals/v<n>.md` and
posted as one ledger comment through the existing outbox. The next AskUserQuestion wait pins that
version in its outbox entry; its wait card body carries the version id, the full text and every
question/option the agent asked. The block reason (the ping) stays short.

Patterns followed: the outbox (`runs.enqueue` / `runs.drain`, `events.replay`, `runs.deliver`), read-
state-first moves (docs/learnings.md "issue-run outbox"), `done` as the one hook that prints and can
exit 1 (events.hook docstring), `--` before agent-written positionals (learnings "hardening").

## Task 1: contract — a comment is in `show --json` whole

- Files: tests/test_kanban_contract.py.
- Test first: comment a multi-line body; `show --json`'s `comments[-1].body` equals it.
- Verify: `.venv/bin/python -m pytest -q tests/test_kanban_contract.py`
- Exists because: replay skips a version whose header comment is already on the ledger, and the
  reviewer reads the snapshot from `show --json`; both rely on this unmocked behaviour.

## Task 2: `propose` for issue runs, and the comment delivery

- Files: muster/events.py, muster/cli.py, tests/test_events.py.
- Tests first (fake board gains `comment` and `comments` in show): v1 posts one ledger comment
  headed `Proposal v1 <sha>`; the same text again posts nothing and stays v1; a new text is v2 and
  v1's comment stays; a comment failing twice makes propose exit 1 with the error and leaves the
  entry queued; the flush then posts exactly one comment; a replay after a kill between comment and
  record posts no second comment; an empty, missing or >64 KB file is refused before anything is
  saved.
- Code: `events.propose(card, file, **issue)`; `move`/`deliver` handle event `proposal` by
  `post_proposal` (skip when a comment starts with the header). cli: `propose` in hook choices,
  exits 1 on failure like `done`.
- Verify: `.venv/bin/python -m pytest -q tests/test_events.py`
- Exists because: the issue's explicit workflow boundary, durable retry-safe write, no duplicates.

## Task 3: the wait card carries the pinned version and the whole ask

- Files: muster/claude.py, muster/events.py, tests/test_events.py, tests/test_claude.py.
- Tests first: after propose, an AskUserQuestion PreToolUse opens a wait card whose body has the
  provenance line, `Proposal v1 <sha>`, the full text and every option label/description; its block
  reason names the version and stays short; the next ask (no new propose) carries no proposal; a
  permission prompt never consumes a pending proposal; a wait queued behind an undelivered proposal
  is not opened until the proposal lands (no premature notification).
- Code: `claude.ask(payload)` returns the AskUserQuestion questions or None; the hook pins
  `{version, sha}` into the notification entry (`events.pin`); `open_wait` builds the body.
- Verify: `.venv/bin/python -m pytest -q tests/test_events.py tests/test_claude.py`
- Exists because: "fully evaluated from `show <wait-card> --json`", precise version linkage.

## Task 4: ad-hoc runs

- Files: muster/runs.py, tests/test_runs.py.
- Tests first: `hook --card <card> propose <file>` posts the ledger comment; the next ask's wait card
  carries it, as for issue runs.
- Code: `runs.hook` routes `propose` before reading stdin; `runs.deliver` handles `proposal`;
  the notification entry gets the same pin and ask.
- Verify: `.venv/bin/python -m pytest -q tests/test_runs.py`
- Exists because: "the same holds for an ad-hoc run".

## Task 5: brief, footer, README

- Files: muster/core.py (brief), muster/runs.py (footer), README.md, tests/test_core.py.
- Test first: the brief and the footer name `muster hook propose` (footer with `--card <card>`).
- Verify: `.venv/bin/python -m pytest -q && .venv/bin/ruff check .`
- Exists because: agents only use the boundary if told; README must say how reviewers retrieve it.

Cut: secret scrubbing (no reliable detector; the brief says no secrets), scrollback scraping (the
issue forbids it), new kanban fields or a snapshot card, anything from #17's decision workflow.

## Decisions from the plan attack (fresh sub-agent, 2026-10-08)

- One owner: `events.propose(card, file, **issue)` serves both run kinds; `runs.hook` calls it before
  reading stdin, and `events.hook` routes `propose` before its stdin read too (a tty stdin would hang).
- The text is read from the file (relative to the hook's cwd), refused (stderr, exit 1) when missing,
  empty, not UTF-8 or over 48 KB (option previews on the card cut at 2 KB: Linux caps one argv string at 128 KB), and passed through `core.SECRET` (the GitHub token redactor) before
  it is hashed or stored. cli.main exits 1 for `propose` failures, as for `done`.
- Versioning compares with the latest version only (v1, v2, then v1's text again is v3: a revert is a
  new revision). Allocation, the `v<n>.md` write and the enqueue run under the run's `lock` file, released
  before the drain (flock is per open file: drain re-taking it in-process would deadlock).
- Every propose enqueues, even an unchanged text: delivery skips a header already on the ledger, so a kill
  between the file write and the enqueue is healed by running propose again.
- `proposals/armed` holds the version the next ask carries; propose writes it, the AskUserQuestion hook
  consumes it at enqueue and pins `{"version", "sha"}` in the entry with `"ask"` (the questions). A
  permission prompt neither reads nor consumes it.
- The `proposal` entry: detail = header, field `proposal`. `runs.enqueue`'s run.json guard keys on
  `git_dir` (issue runs), not on extra fields being present.
- Delivery: `move` handles `proposal` first; `runs.deliver` after its archived check and before the
  `ledger == "done"` branch, returning None (a comment is no notifier event). Comment argv:
  `comment -- <ledger> <header + text>`.
- propose output: `proposal: Proposal v<n> <sha> on ledger <card>`, exit 0; not delivered (queued) or the
  ledger archived: the reason on stderr, exit 1.
- `open_wait(..., ask=None, proposal=None)`; block reason:
  `<question>\nProposal v<n> <sha>: full text on this card and ledger <card>.\nReply in Herdr pane <p>.`
- Accepted: an ask whose wait card is already open drops its pin (that card was opened for the earlier
  wait); a persistently failing comment holds every later ping of that run until it lands (only hermes
  being down does that, and then no ping can go out anyway).
