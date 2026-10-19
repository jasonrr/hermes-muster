> **Historical record (2026-10-16).** Not operating instructions: later work may have changed what this describes. Current behavior is in the [README](../../README.md).

# Plan: Ella reviews each proposal version; the human acts with one tap (issue #36)

Design: Proposal v2 5c0779c477b3 on ledger t_c5218f7e, approved 2026-10-16. Summary:
1. Each delivered proposal version creates a DESIGN REVIEW card (key `design:<ledger>:v<n>:<sha>`), subscribed in
   the run topic with Hermes's native delivery mode `wake`, then completed: the coordinator wakes, no ping.
2. The coordinator records `hermes muster recommend-design <ledger> --proposal v<n>:<sha> --choice
   approve|revise|judgment --review F [--feedback F]`: a `design` request with Approve / Send back / Do nothing.
3. The human's tap answers the ledger's open approval `question` request whose pinned proposal equals the
   reviewed one, through that request's own open -> answered transition; the pane's bridge delivers it.

Read `docs/learnings.md` first (#5 outbox: save first, every move reads state; "Hermes idempotency returns only
a non-archived card"). Verify: `.venv/bin/python -m pytest -q` (venv: `uv venv .venv && uv pip install --python
.venv/bin/python pytest`). Lint: `python3 -m py_compile <changed files>`. Commit each task with explicit paths, no
trailer.

## Task 1: the design review wake (`core.subscribe` mode; `events.post_proposal`)

**Files:** `muster/core.py`, `muster/events.py`, `muster/runs.py`, `tests/test_events.py`, `tests/test_runs.py`.

**Exists because:** a proposal comment is no notifier event, so the coordinator never wakes for designs (#36).

**Pattern:** `decisions.rereview` (idempotent card, `core.subscribe(card, ledger)`, `complete`, `expect`).

**Test first** (`tests/test_events.py`, `board` fixture; the fake's `create` hands out `t_wait<n>` per key):
- propose "plan one" -> a create keyed `design:t_abc123:v1:<sha>`, its body names ledger t_abc123 and
  `Proposal v1 <sha>` and ends with the text; `notify-subscribe ... --delivery-mode wake`; `complete` with summary
  `Proposal v1 <sha> ready for design review; review it with the muster:escalation skill`; card done.
- `propose` the same text again, and a redelivered proposal entry -> no second `complete`.
- the comment already posted but the design card absent (comment pre-seeded in `board["comments"]`) -> a
  redelivery creates and completes the card.
- revised text -> a second card keyed v2; v1's card untouched.
- `tests/test_runs.py`: the ad-hoc propose test also sees the design card completed (deliver path).
- Existing tests that name `t_wait1` for the approval wait card now see `t_wait2` (the design card takes the
  first key): update those ids only.

**Code:**
- `core.subscribe(card, ledger=None, mode="notify+wake")`: pass `mode` to `--delivery-mode` and to the read-back.
- `events.review(ledger, proposal, title)`: the card as in the summary; title
  `f"{title or ledger}: {heading}, ready for design review"`; body
  `f"Design review. ledger {ledger} | {heading}\n{core.PROVENANCE}" + attached(ledger, proposal)`.
  When the created card reads `ready`: subscribe (mode `wake`), complete, `expect(... "done")`. Any other status:
  already reviewed, nothing.
- `post_proposal(ledger, proposal, title=None)` calls `review` after the comment reads back (also when the comment
  was already there). Callers: `events.move` passes `link.get("title")`; `runs.deliver` passes `run.get("title")`
  and still returns None (no ack: Hermes retries a wake-only delivery itself).

**Verify:** `.venv/bin/python -m pytest -q tests/test_events.py tests/test_runs.py`.

## Task 2: `recommend-design` (the `design` request)

**Files:** `muster/decisions.py`, `muster/events.py` (a `versions(card)` helper, used by `propose` too),
`muster/cli.py`, `tests/test_decisions.py`, `tests/test_cli.py`.

**Exists because:** the human must act on the review, bound to the reviewed version (design v2 §2).

**Pattern:** `decisions.recommend` (validation, the dedupe on an open request, `create`, `stale_others`).

**Test first** (`tests/test_decisions.py`; write proposal files directly into `events.proposals(ledger)`;
monkeypatch `decisions.run_of` to return a run dict):
- v2 saved, `--proposal v1:<sha1>` -> refused "Proposal v2 is newer"; a wrong sha -> refused; `revise` without
  `--feedback` -> refused; a malformed `--proposal` -> refused; an unknown version -> refused. Exit 1, no request.
- valid `revise` -> one open `design` request: `proposal == {"version": 2, "sha": sha2}`, `actions ==
  ["send-back", "approve", "nothing"]`, labels `["Send back (recommended)", "Approve", "Do nothing"]`, question
  text starting `Design review of Proposal v2 <sha2>`; `judgment` -> actions `["approve", "send-back", "nothing"]`,
  no "(recommended)".
- the same pin again while open -> prints the existing id, no second request; a request for an older pin still
  open -> staled by the new one.
- `tests/test_cli.py`: the parser takes the command and dispatches it.

**Code:** `DESIGN_LABELS = {"approve": "Approve", "send-back": "Send back", "nothing": "Do nothing"}`,
`DESIGN_CHOICE = {"approve": "approve", "revise": "send-back", "judgment": None}`; `recommend_design(args)` as in
the tests, `read_text` for the files, `run_of(ledger)` for `run` (the gateway's title and topic need it).
CLI: `recommend-design` with `ledger`, `--proposal`, `--choice {approve,revise,judgment}`, `--review`,
`--feedback`; SUBCOMMANDS maps it to `("decisions", "recommend_design")`.

**Verify:** `.venv/bin/python -m pytest -q tests/test_decisions.py tests/test_cli.py`.

## Task 3: executing the human's tap

**Files:** `muster/decisions.py`, `muster/gateway.py`, `tests/test_decisions.py`, `tests/test_gateway.py`.

**Exists because:** only the human's tap may answer the approval question (design v2 §3).

**Pattern:** `decisions._execute` / `_feedback_from_build` (status guard, `executing` with `intent`, `fail`);
`_recover` for boot recovery.

**Test first:**
- a `question` request (`proposal` = the pin, one question) open; design request answered `{"action":
  "approve"}`; `execute` -> question `answered` with `{<its text>: "Approve: the human approved Proposal v2
  <sha>."}`, design `done`.
- `send-back` -> the question's answer starts `Revise Proposal v2 <sha> before building.` and holds the feedback;
  a typed `{"text": "also X"}` -> feedback plus `Additional instructions from the human:\nalso X`; `replace: Y`
  -> only Y; `send-back` with no feedback -> design `failed`, question still open.
- the open question pins v1 while the design pins v2, or no question open -> design `failed` naming the version,
  question untouched; a question with two questions -> `failed`.
- `nothing` -> design `done`, question still open.
- recovery: design `executing` whose question already carries the reply -> `done`, no second transition; one
  whose question is still open -> answered once.
- `tests/test_gateway.py`: an answered `design` request runs `decisions.execute` (as a `build` one does), and boot
  recovery covers `design`.

**Code:** `_execute` branches on `design` first to `_design(req, boot)` (reply text from the answer; `executing`
with `intent={"reply": ...}`; then `_answer_approval`). `_answer_approval(req)`: among `for_ledger(ledger,
"question")` with `proposal == pin`, one already answered with this reply -> design done; else the open one with
exactly one question -> `transition(open -> answered, answer={text: reply})` -> design done; anything else -> fail
with the reason. `_recover`: `design` -> `_answer_approval`. Gateway: add `"design"` to the boot-recovery kinds
and to `on_answered`.

**Verify:** `.venv/bin/python -m pytest -q tests/test_decisions.py tests/test_gateway.py`.

## Task 4: the escalation skill and README

**Files:** `skills/escalation/SKILL.md`, `README.md`.

**Exists because:** the coordinator learns what a design review card means only from the skill.

**Steps:** the card list gains DESIGN REVIEW; a step "Completed design review card": read the card (its body has
the pinned version's full text), review it (minimum engineering, behavior first, blockers vs optional hardening,
real trade-offs, human approval kept), write the review, run `recommend-design` with the heading's `v<n>:<sha>`,
then `[SILENT]`. A refusal "Proposal v<m> is newer" means a newer card follows: reply `[SILENT]`. README: the
decisions section gains "Designs". No test (prose); check with `grep -n recommend-design skills README.md`.

**Verify:** full suite: `.venv/bin/python -m pytest -q`.

## Fixes from the plan attack (2026-10-16)

- A1 (Task 1) A failed wake must not block arming or the queue. The wake is its own outbox event, `design`,
  queued by `propose` right after `proposal`; `propose` waits only for `proposal`. In `runs.drain` a failed
  `design` entry, like `stop`, does not stop the queue (later questions still land); the flush retries it.
  `events.move` and `runs.deliver` route `design` to `events.review`. `post_proposal` stays comment-only.
- A2 (Task 1) An archived design card plus a replay makes a second wake (Hermes idempotency skips archived
  cards). Accepted: it needs a replay after the card was archived; listed in the PR as p3.
- A3 (Task 2) An open `design` request is advisory: nothing waits on it. `cleanup.owner_free` skips `design`
  requests in `open`, so one the human never tapped does not hold the worktree; `gateway.stale_requests` ends it
  when the run is archived.
- A4 (Task 3) Tap before the agent asked: when no `question` request for the pin has existed yet, the design
  request fails with "the agent has not asked for approval of Proposal v<n> <sha> yet; tap again on the new
  message" and a fresh copy is created (as `_recover` re-offers a build), so the tap is not lost. A question for
  the pin that already ended, or one about another version: fail, no copy.
- A5 formats: `--proposal` is `v<n>:<12 hex>` (`re.fullmatch(r"v(\d+):([0-9a-f]{12})")`), else refused
  "--proposal must be v<n>:<sha> from the card's heading". `versions(card)` returns sorted ints. The question text
  is `f"Design review of {heading}\n\n{review}"` (render cuts to the Telegram cap). `--feedback` is accepted with
  any choice: it is what Send back sends. A typed reply is always a revision (amend). Replies:
  `APPROVE = "Approve: the human approved {heading}."`,
  `REVISE = "Revise {heading} before building. Revisions from the human:\n{feedback}"`.
  `replay` entries carry `link`, so `move` passes `link.get("title")`.
