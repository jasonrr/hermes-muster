# Run progress — implementation plan (issue #46)

Design: `docs/plans/2026-10-17-run-progress-design.md` (approved v2, Proposal v2 11cdee175c5f).
Run tests in the project venv: `. .venv/bin/activate` (docs/learnings.md, PR #1). Lint: `ruff check muster tests`
if ruff is installed (it is how the repo is kept), else skip and say so.

Conventions to keep: small modules with a docstring that states the contract (see `muster/conversation.py`);
hooks never fail the agent (`events.hook`'s `except Exception` + log); files via `core.save_json`; gateway file IO
via `asyncio.to_thread`; `shared.now` (patchable) for wall-clock time in the gateway.

---

## Task 1 — `muster/progress.py`: evidence files and the state function

Files: `muster/progress.py` (new), `muster/core.py` (`seen_at`), `tests/test_progress.py` (new).

Nearest pattern: `muster/conversation.py` (one small data module, `config.data_dir() / "runs" / ledger / …`,
`core.save_json`), `core.seen` (prompt-seen.jsonl reader).

Contents:
- `QUIET = 15 * 60`, `REFRESH = 5 * 60`, `RETRY = 60`.
- `activity(ledger)` → `config.data_dir() / "runs" / ledger / "activity"`; `path(ledger)` → `…/progress.json`;
  `load(ledger)` → dict (`{}` when missing or unreadable).
- `touch(ledger, kind, text=None)`: mkdir, `write_text(text)` when text is given else `Path.touch()`; any
  `OSError` is logged with `core.log("progress", …)` and swallowed. Kinds: `launched`, `prompt`, `tool`, `stop`,
  `end`, `review`.
- `hooked(ledger, event, payload)`: `prompt` + `hook_event_name == "UserPromptSubmit"` → `prompt`;
  `prompt` + `PostToolUse`/`PostToolUseFailure` → `tool`; `stop` → `stop`; `session-end` → `end`; else nothing.
- `launched(ledger, directory, sha)`: `touch(ledger, "launched", json.dumps({"dir": str(directory), "sha256": sha}))`.
- `core.seen_at(directory, sha)` → the matching record's `at` (int) or None; `core.seen` becomes
  `seen_at(...) is not None` (every caller keeps a bool).
- `tracked()`: ledgers with an `activity/launched` file whose `progress.json` is not `final`.
- `state(ledger, now, waits, ref, archived)` → `(key, text, final)`, pure apart from reading the activity mtimes,
  the `launched` JSON, `load(ledger)["received"]` and `core.seen_at`. It stores `received` once found
  (`core.save_json`, merge into progress.json). `waits` = created_at of the ledger's open question/permission
  requests. `ref` = conversation reference or None. `archived` = True/False/None. Rules exactly as the design's
  list 1–7, keys: `archived`, `finished`, `ended`, `waiting:<n>`, `review:<review mtime>`, `turn`, `launched`,
  `received`, `working`, `quiet`. `finished` only when `ref.get("resolved_at")` is set (cleanup saw the merge); a
  human's topic close is not finished. `archived` when `archived is True` or
  ref state in `retiring/deleted`. Times: `time.strftime("%H:%M", time.localtime(t))`; minutes:
  `int((now - t) // 60)`.
- `due(saved, key, text, now)`: `saved.get("key") != key`, or (`text != saved.get("text")` and
  `now - saved.get("edited_at", 0) >= REFRESH`).

Test first (`tests/test_progress.py`, write and watch fail): one test per state — launched-unconfirmed (launched
file, no prompt-seen record); confirmed receipt with no tool ("no tool activity observed yet"); a later non-brief
prompt does not move the received time and a non-brief prompt alone does not confirm; working (tool 3 min old);
long-running active (launched 2 h ago, tool 1 min ago → working); quiet (tool 20 min old → "no new activity
observed", never "stalled"); received with no tool for 20 min → quiet; waiting (one open wait) with a newer tool
mtime stays waiting; review newer than prompt; send-back prompt after review → working; turn (stop newest);
ended; finished (ref with resolved_at) final; archived final; `due` (same key within 5 min no, after 5 min with new
text yes, same text never, new key always). Set mtimes with `os.utime`.

Verify: `python -m pytest -q tests/test_progress.py tests/test_core.py`

Exists because: the design's evidence files and state rules need one tested home that both the hooks (writers) and
the gateway (reader) import without a cycle (progress imports only config and core).

## Task 2 — hook and completion writers

Files: `muster/events.py`, `muster/runs.py`, `muster/core.py` (`ensure`), `muster/decisions.py` (`rereview`),
tests in `tests/test_events.py`, `tests/test_runs.py`, `tests/test_launch.py`, `tests/test_decisions.py`.

Nearest pattern: `core.prompt_seen` call sites in `events.hook` / `runs.hook` (best-effort evidence write inside
the hook, `contextlib.suppress`).

Changes:
- `events.hook`: drop the early `if event == "stop": return 0`. After `git_dir, link = found`, call
  `progress.hooked(link["card"], event, payload)`; then `if event == "stop": return 0` (unchanged behaviour
  otherwise). `stop` and `session-end` now read stdin like other hook events (they are Claude hooks, not
  AGENT_RUN). `touch` swallows its own errors.
- `runs.hook`: after the `claude.ignore` check, `progress.hooked(card, event, payload)`.
- `events.move`, `done` branch, first completion: after `expect(card, "done", event)` →
  `progress.touch(card, "review", detail)`.
- `runs.complete`: after the complete/readback (and only then) → `progress.touch(card, "review", pr["url"])`.
- `decisions.rereview`: just before `return card["id"], None` → `progress.touch(ledger, "review", pr["url"])`.
- `core.ensure`: after `state = send_prompt(...)` and `save(rec)` → `progress.launched(rec["owner"], directory,
  rec["sha256"])` (lazy import inside the function: progress imports core).

Test first: issue hook — UserPromptSubmit payload touches `prompt` only; PostToolUse touches `tool`; Stop touches
`stop` and still returns 0 with no kanban call; SessionEnd touches `end` (and `/clear` touches nothing);
`hook done` touches `review` with the PR url only once the ledger reads done (a refused done touches nothing).
Ad-hoc — `fire(...)` for the same events; a Stop delivery with a verified PR (`test_a_verified_pr_completes…`
setup) touches `review`; `hook done` still exits 1 and touches nothing. Launch — a delivered brief writes
`launched` with the launch dir and sha (`test_launch.py` happy path). Rereview — a verified revision touches
`review`. Replaying the same hook only moves the mtime (file count unchanged).

Verify: `python -m pytest -q tests/test_events.py tests/test_runs.py tests/test_launch.py tests/test_decisions.py`

Exists because: the state function has no evidence without writers, and ad-hoc runs never run `hook done`.

## Task 3 — gateway pass and Telegram `show`

Files: `muster/gateway.py`, `muster/channels/telegram.py`, `muster/channels/__init__.py` (contract docstring),
`tests/test_gateway.py`.

Nearest pattern: `gateway.scan` / `settle_edit` (per-item try, log, continue); `telegram.topic_step`'s
`T.adapter.send(chat, text, metadata={"thread_id": …})` and `telegram.edit`.

Changes:
- `gateway.scan`: first, in its own try, `await progress_pass()`, which reads `decisions.open_requests()` itself;
  then `CHANNEL.steps()` and the rest unchanged. First, so a resolved run's final edit lands while its topic is still
  `closing` (open), before the channel step closes it.
- `progress_pass()`: waits per ledger from the open requests with kind in (question, permission) and status `open`; for each
  `progress.tracked()` ledger, in its own try: skip while `now < saved.retry_at`; ref = `conversation.load`;
  archived = ref-derived, else for a ledger without a ref `events.status` at most every `shared.CLOSED_EVERY`
  (own dict `S.progress_checked`); `(key, text, final) = progress.state(...)`; skip unless `progress.due`; skip
  when `saved.get("lost_thread") == thread now` (a lost post is never repeated); `shown = await CHANNEL.show(ledger,
  text, saved)`; on an exception save `lost_thread` and log; on None save `retry_at = now + RETRY`; else save
  `{**shown, "key", "text", "edited_at": now, "final": final}`.
- Telegram `show(ledger, text, saved)`: thread = `conversation.target(ledger)["thread_id"]`; ref state for this
  platform in (pending, creating, retiring, deleted) → None. If `saved.mid` and `saved.thread == thread` → `edit`
  → `saved` subset on success, None on failure. Else, only in (no ref / other platform / open / fallback): send
  `"muster · <ref name or ledger>\n" + text` with the thread metadata → `{"mid", "thread"}` on success, None on a
  failure result; exceptions propagate (lost).
- Contract docstring: add `async show(ledger, text, saved) -> dict | None`.

Test first (Telegram fake): a launched run with confirmed receipt gets exactly one message; a second scan sends
and edits nothing; a tool touch within 5 min → no edit, after 5 min (patch `shared.now`) → one edit; a state
change (open question request) → immediate edit, and the question message comes after the status message in
`notes`/`sent` order; answer (request done) → edit back to activity; restart (fresh `gateway.S`, same files) → no
new post; finished (ref resolved_at) → one final edit, then a later tool touch changes nothing; a refresh leaves
every activity mtime unchanged; a send that fails → nothing saved but `retry_at`, retried after 60 s; a send that
raises → never posted again; a remade topic (ref thread changes) → one new post there; an old run without
`launched` → nothing.

Verify: `python -m pytest -q tests/test_gateway.py`

Exists because: the human sees nothing without the message, and only the gateway holds the bot.

## Task 4 — Slack `show`: the thread parent carries the line

Files: `muster/channels/slack.py`, `tests/test_slack.py`.

Nearest pattern: `slack.parent` / `parent_line` / `create` (chat_update of the parent, `GONE` handling).

Changes:
- `parent_line(ref, status, text=None)`: for `running`, append `"\n" + (text or progress.load(ref["ledger"]).get("text"))`
  when present, so the lifecycle's own parent writes (reopen) keep the line.
- `show(ledger, text, saved)`: ref mine and state `open` with a thread → `chat_update(ts=thread,
  text=slack_text(parent_line(ref, "running", text)))` → `{"mid": thread, "thread": thread}`; a GONE error or other
  failure → None (logged). Ref mine in (pending, creating, retiring, deleted) → None; in (closing, closed) →
  `saved` (the lifecycle marks the parent done; nothing to add). No ref / other platform / fallback → post or edit
  a channel message (`chat_postMessage` / `chat_update(ts=saved.mid)`) like Telegram.

Test first (`threads` fixture): the parent is updated with the progress line, no new post in the thread; a reopen's
`parent()` keeps the line; closing → no progress edit, `final` saved; no-thread run → one channel post then edits.

Verify: `python -m pytest -q tests/test_slack.py`

Exists because: the human asked to reuse the existing run-status message, and on Slack that is the parent.

## Task 5 — docs

Files: `README.md` (new "Run progress" section after "Run topics": states, evidence, cadence, gaps, never acts),
`docs/learnings.md` (one `pattern → consequence` bullet per surprise met while building).

Verify: `python -m pytest -q && hermes plugins validate .`

Exists because: the issue asks to document verified vs inferred evidence and the bounded detection gaps.

---

## Amendments after the plan attack (these override the tasks above where they differ)

T1
- No `received` cache: `state` calls `core.seen_at(dir, sha)` each time (one small jsonl read). progress.json is
  only the message record; every write merges (`{**load(ledger), ...}`).
- Texts, exact: `🚀 Launched HH:MM · brief receipt not confirmed yet.`; `Brief received HH:MM · ` + tool line;
  tool line = `no tool activity observed yet` | `no tool activity observed yet (N min)` (quiet) |
  `last tool activity N min ago (HH:MM, as of HH:MM)` | `no new activity observed for N min (last tool HH:MM). Long
  tools can be quiet; muster takes no action.`; `✋ Waiting for you since HH:MM (N open) · ` + tool line (`1 open`
  too: no plural logic); `🔍 Ready for review: <url> (since HH:MM)` (`Ready for review (since HH:MM)` when the file
  is empty); `💬 Turn ended at HH:MM: the agent waits at its prompt (no question open).`; `⛔ Session ended at
  HH:MM.`; `✅ Finished: pull request merged.`; `Archived.`
- Keys: `waiting:<n>:<oldest created_at>`; `review:<mtime>` stays (writers no longer re-touch on replay, below).
- `touch(kind)` for every kind but `launched` writes only when `activity/` already exists, so a late hook after
  cleanup removed `runs/<ledger>` recreates nothing, and runs from before the upgrade stay untracked.
- `hooked` never raises (its own `try/except Exception` → log), whatever the payload.
- Tests compute expected HH:MM with the same `time.strftime("%H:%M", time.localtime(t))`; mtimes are set with a
  test helper `at(ledger, kind, t)` (`touch` + `os.utime(t, t)`) relative to the injected `now`.

T2
- `events.hook`: `hooked` runs right after `git_dir, link = found` (after `claude.ignore`); then `stop` returns 0.
  `test_a_stop_without_a_card_does_nothing_at_all` is updated: a Stop now runs the context `git rev-parse` (as every
  PostToolUse already does) and still makes no kanban call. New test payloads carry `hook_event_name`.
- `runs.complete`: touch `review` after `close_wait`, only when `pr["state"] != "MERGED"` (a merged PR is not
  "ready for review"; the run reads "Turn ended" until it is resolved or archived — a documented gap).
- `decisions.rereview`: touch only inside `if card["status"] == "ready":` (the call that made the review card), so a
  replay or a later Stop does not move "since".
- `core.ensure`: `progress.launched` on both the early `DELIVERED` return and after `send_prompt` (closes the crash
  window and lets a recovered run be tracked).
- Replay test: the issue `board` fixture, assert `sorted(os.listdir(activity))` unchanged after a second identical
  hook.

T3
- `chat` = `core.notify_target()["chat_id"]` (as `telegram.outcome`). All sync IO in `asyncio.to_thread`; `now` =
  `shared.now()`.
- Archived for a ledger without a conversation ref: reuse `shared.archived(ledger)`, throttled by `shared.checked`
  at `shared.CLOSED_EVERY` (channel steps never see such a ledger, so no interplay).
- Tracked set: `progress.tracked()` globs `runs/*/activity/launched`; the gateway keeps `S.final` (ledgers saved
  final) and skips them without reading their file.
- Post guard: when there is no `mid` for the current thread, save `{"posting": {"thread": thread}}` before calling
  `show`. `show` returns None only when it sent nothing (not ready, adapter missing, a failure result) → clear
  `posting`. A dict → save `mid`, `thread`, clear `posting`. An exception, or a `posting` for this same thread found
  on a later pass (a kill mid-send) → lost: never posted again for that thread, logged once. Pre-send problems
  inside `show` (no adapter) return None, not raise.
- `retry_at = now + RETRY` only after a failed send or edit, never for not-ready.
- Final with nothing to show (no `mid`, or the ref is retiring/deleted, or Slack's lifecycle owns the parent) →
  save `final` anyway; `S.final` gets it.
- Telegram `closing`/`closed` with no mid → None and not-ready (no post into a closing topic); if the state is
  final, saved final as above.
- A Telegram `success=False` may hide a delivered message (Hermes source not checked here); `present` has the same
  exposure today. Documented, not solved.
- Fake: `tests/fake_hermes.py` `Adapter.send` gains `fail_notes` (next n sends return success False) and
  `raise_notes` (next n raise); order between a status post and a question is asserted by message id (one counter).
- `tests/test_channels.py` `Durable` gets `async def show(...): return None` (contract completeness).

T4
- `chat_update(channel=ref["chat_id"], ts=thread, ...)` for the parent; `chat()` for a channel message.
- `parent_line` appends the saved line only while `not progress.load(ledger).get("final")`.
- GONE parent → final saved (nothing left to edit); other failures → retry_at.
- README states: on Slack threads the final line is the lifecycle's own `✅ done` / `archived`.

T5: lint = `/Users/jasonrosoff/Code/radicalcandorai/bot_service/.venv/bin/ruff check --select F muster tests`
(the project ships no ruff config); say so in the PR.
