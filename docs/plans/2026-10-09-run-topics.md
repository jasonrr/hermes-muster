# Plan: one Telegram topic per run (issue #24)

Design: `docs/plans/2026-10-09-run-topics-design.md` (Proposal v3, approved 2026-10-09). The human will check live,
before merge, whether Telegram delivers the bot's own `forum_topic_created` service message.

Verify everything with `. .venv/bin/activate && python -m pytest -q` (docs/learnings.md: system python3 has no
pytest) and `hermes plugins validate .`.

## Shared facts
- Hermes surfaces used: `kanban notify-subscribe/notify-unsubscribe --thread-id`
  (hermes_cli/kanban_parser.py:86-90). The `metadata={"thread_id": t}` argument on `adapter.send_clarify` /
  `send_exec_approval` / `send`. `adapter.create_handoff_thread(chat, name) -> str|None` and
  `adapter.get_chat_info(chat) -> {"type": "forum"|...}`. From PTB `app.bot` (already held as `S.bot`):
  `get_chat_member(chat, bot.id)` (`.status`, `.can_manage_topics`), `edit_forum_topic(chat, message_thread_id,
  name=)`, `close_forum_topic(chat, message_thread_id)`, `reopen_forum_topic(chat, message_thread_id)`,
  `send_message(..., message_thread_id=)`.
- The reference is `config.data_dir()/runs/<ledger>/conversation.json`:
  `{platform, chat_id, user_id, thread_id, name, state, why, attempts, since, previous, cards, noticed}`.
  `state` is one of `pending | creating | open | closing | closed | fallback`. `thread_id` is a str or None
  (a future Slack `thread_ts`).
- Probe classification of the exception text, lower-cased: `"not_modified"` or `"not modified"` → alive;
  `"thread not found"`, `"topic_id_invalid"` or `"topic_deleted"` → deleted; anything else → unknown. Unknown
  changes nothing.

## Task 1: the setting
- Files: `muster/config.py`, `plugin.yaml`, `tests/test_config.py`.
- Test first: with `notify_topics=True`, `config.require()` raises ConfigError when `notify_chat_id` is empty,
  when `notify_user_id` is empty, or when `notify_platform != "telegram"`. A non-bool value is refused too.
  The default is False.
- Code: add `"notify_topics": False` to DEFAULTS. Validate it in `require()`, in the style of the
  `project_number` block (config.py:48-53). Add it to the plugin.yaml config_schema next to `notify_chat_type`.
- Verify: `python -m pytest -q tests/test_config.py`
- Exists because: #24 requires topics to be opt-in, and a topic needs a forum group, which a DM cannot be.

## Task 2: the reference store
- Files: new `muster/conversation.py`, new `tests/test_conversation.py`.
- Pattern: `decisions.locked` / `update` (decisions.py:61-96), with flock on `<dir>/conversation.lock`, and
  `core.save_json` for atomic writes.
- Functions:
  - `path(ledger)`.
  - `load(ledger)`: the dict, or None.
  - `update(ledger, **fields)`: under the lock, merges fields and returns the result. It creates the file when
    missing.
  - `swap(ledger, from_states, **fields)`: under the lock, applies the fields only when the state is in
    from_states. Returns `(ref, ok)`. This is the guard against a late gateway write racing the tick's
    fallback.
  - `target(ledger)`: `core.notify_target()`, plus `thread_id` (None unless the ref is in `open|closing|closed`
    with a thread). With `chat_type="forum"` when it has a thread.
  - `add_card(ledger, card)`: appends to `cards` once.
  - `name(repo, title, issue=None, branch=None)`: `"<name>#<n> <title>"` or `"<name> <branch>: <title>"`, cut to
    128 characters (Telegram's limit).
  - `request(ledger, name, wait=45)`, the tick side:
    1. Return None when `notify_topics` is off.
    2. If `decisions.gateway_up()` is false, write `fallback` with why "the Hermes gateway is not running" and
       return.
    3. Otherwise write `pending` (with `platform`, `chat_id`, `user_id`, `name`, `attempts: 0`, `previous: []`,
       `cards: []`).
    4. Poll every 0.5 s until the state is `open` or `fallback`.
    5. On timeout, `swap(ledger, ("pending", "creating"), state="fallback", why="no topic within 45 s")`.
    6. Return the ref.
  - `finish(ledger)`: `swap(ledger, ("open",), state="closing")`.
  - `active()`: every ref under `runs/*/conversation.json` whose state is not `closed`. An unreadable file is
    logged and skipped, as in `decisions.read_all`.
- Test first:
  - `request` with the gateway down gives `fallback` without waiting.
  - `request` where a thread fills the ref to `open` returns the thread.
  - On a timeout, `request` gives `fallback`, and a late `swap(("pending", "creating"), state="open")` is
    refused.
  - `target` returns the main chat when there is no ref, and for `fallback`.
  - `name` is truncated.
  - `add_card` is idempotent.
- Verify: `python -m pytest -q tests/test_conversation.py`
- Exists because: the reference must survive restarts, revisions and recovery (#24), and the CLI and the
  gateway are separate processes.

## Task 3: subscriptions follow the reference
- Files: `muster/core.py`, `muster/runs.py`, `muster/events.py`, `muster/decisions.py`, `muster/cleanup.py`, and
  their tests (`tests/test_core.py`, `test_runs.py`, `test_events.py`, `test_decisions.py`, `test_cleanup.py`).
- `core.subscribe(card, ledger=None)`:
  - The target is `conversation.target(ledger)` when a ledger is given, else `notify_target()`.
  - Add `--thread-id <t>` when there is a thread, and include `thread_id` in the read-back `want`.
  - After the read-back succeeds, call `conversation.add_card(ledger, card)` when the ref exists.
  - With topics off there is no ref, so the argv is byte-identical: assert that in a test.
- Callers:
  - `core.launch` (core.py:942): before `subscribe(card)`, set `at["step"] = "topic"` and call
    `conversation.request(card, conversation.name(repo, issue["title"], issue=number))`. When it returns a
    `fallback` ref, add a kanban comment on the card: `topic: not created: <why>; this run stays in the main
    chat` (suppress CommandError, as with the project comment). Then `subscribe(card, card)`.
  - `runs.launch_run` (runs.py:474): the same, with `name(repo, title, branch=branch)`.
  - `events.open_wait` (events.py:170): `core.subscribe(card, link["card"])`.
  - `decisions.rereview` (decisions.py:188): `core.subscribe(card["id"], ledger)`.
  - `cleanup.escalate`: `core.subscribe(card, ledger)`, where `escalate(link, reason, signature, ledger)`
    takes the target's ledger (Task 6 adds `ledger` to targets).
- Test first:
  - A ledger with an `open` ref gets `--thread-id` on its subscription and its wait card's subscription.
  - The review card in `rereview` carries the thread.
  - With topics off, nothing changes.
  - A `fallback` ref gives a main-chat subscription and the comment.
  - Two ledgers with two refs get different `--thread-id`s.
- Verify: `python -m pytest -q tests/test_core.py tests/test_runs.py tests/test_events.py tests/test_decisions.py tests/test_cleanup.py`
- Exists because: Hermes's kanban notifier puts the ping, and the `notify+wake` coordinator turn, in the
  subscription's thread (gateway/kanban_watchers_notifier.py ~699-734). One flag moves all coordinator commentary.

## Task 4: the gateway's topic lifecycle
- Files: `muster/gateway.py`, `tests/fake_hermes.py`, `tests/test_gateway.py`.
- Fakes:
  - `Adapter.create_handoff_thread(chat, name)`: a scripted list of results, where an entry may be "lost"
    (it makes the topic but returns None).
  - `Adapter.get_chat_info`, which returns `{"type": self.chat_type}`.
  - `Adapter.send(chat, content, reply_to=None, metadata=None)`.
  - Record `metadata` on `send_clarify` / `send_exec_approval`.
  - `Bot.id`, plus `get_chat_member`, `edit_forum_topic` (raises the scripted error for deleted threads, else
    `BadRequest("Topic_not_modified")`), `close_forum_topic`, `reopen_forum_topic`, and `send_message(...,
    message_thread_id=None)`.
  - The fake topic store: `topics: {thread: name}`, plus `deleted: set`.
  - A `MessageHandler` stub and `filters.StatusUpdate.FORUM_TOPIC_CREATED` in the `telegram.ext` fake.
- `telegram_factory` adds
  `app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CREATED, topic_created), -1)`.
- `async topics()` runs once per scan, before the request loop. For each `conversation.active()` ref, inside
  its own try block:
  - **`pending` or (`creating` and `since` more than 10 s old):**
    1. If `attempts >= 3`, `swap → fallback` ("no topic after 3 attempts").
    2. Otherwise check the prerequisites: `get_chat_info` type `forum`, and the member's status `creator`, or
       `can_manage_topics`. A definite failure gives `swap → fallback` with an actionable reason.
    3. Otherwise `swap(("pending", "creating"), state="creating", attempts+1, since=now)` and await
       `create_handoff_thread`.
    4. A thread gives `swap(("creating",), state="open", thread_id=t)`. If the swap is refused (the tick fell
       back meanwhile), close t as a duplicate.
  - **Repair:** `open` with a non-empty `previous` and no `repaired` marker for this thread → `repair(ref)`.
  - **`fallback` and not `noticed`:**
    1. `adapter.send(main chat, "Topic not created for <name>: <why>. This run stays in the main chat.")`.
    2. `update(noticed=True)`.
    3. If `previous` is non-empty (a repair that fell back), `repair(ref)` too, so the cards move to the main
       chat.
  - **`closing`:**
    1. `adapter.send(chat, "Run finished; worktree removed. History kept.", metadata={"thread_id": t})`.
    2. `bot.close_forum_topic`.
    3. `swap(("closing",), state="closed")`.
  - **`open` and last probe more than 30 s ago:** `probe(ref)`.
- `topic_created(update, context)`:
  1. Read `msg = update.effective_message`. Proceed only when `str(msg.chat.id) == notify chat` and
     `msg.from_user.id == S.bot.id`.
  2. Find the ref with `ref["name"] == msg.forum_topic_created.name` in state `creating` or `pending`, and
     `swap → open` with `thread_id=str(msg.message_thread_id)`.
  3. Otherwise, if a ref with that name is `open`/`fallback` on a different thread, or the thread is in its
     `previous`, it is a duplicate: send "Duplicate topic, not used; this run is in <name>." to that thread and
     close it.
  4. Wrap everything in try/except, log the error, and never raise.
- `probe(ref)` returns alive, deleted or unknown, using the classification above. On deleted:
  `swap(("open", "closed"), state="creating", thread_id=None, previous=previous+[old], attempts=0, since=0)`.
- `repair(ref)`, once per new thread, recorded as `repaired: <thread or "main">`:
  1. Move each card in `ref["cards"]`: subscribe it to `conversation.target`, then unsubscribe it from each
     thread in `previous` (and from the main chat when the new target has a thread). Use
     `asyncio.to_thread(core.kanban, ...)`. A per-card error is logged and does not stop the rest.
  2. For each open request of the ledger (`decisions.for_ledger`, status `open`): `release(rid)`, edit its
     messages to "Moved to the run's new topic", then `decisions.update(rid, presented={"messages": ...})`
     with no boot, so `handle` presents it again.
  3. Post "The previous topic was deleted; this run continues here." in the new thread (only when there is
     one).
- `async destination(ledger)` → `(ready, metadata)`, called by `present` and `present_approval` before sending:
  - No ref, or `fallback` → `(True, None)`.
  - `pending` or `creating` → `(False, None)`, so the request waits for the next scan.
  - `closed` → `reopen_forum_topic`, `swap → open`, `(True, thread)`.
  - `open` → `probe`. Deleted → `(False, None)`; alive or unknown → `(True, {"thread_id": t})`.
- Test first. Every test drives `run(gateway.scan())` and the fakes:
  - A pending ref becomes open with a thread.
  - Not a forum → fallback, and the notice is sent once.
  - No `can_manage_topics` → fallback.
  - **Lost response:** the create returns None, the fake delivers the service message, and the ref adopts it.
    `create_handoff_thread` is called once. A later service message with the same name on another thread
    gets the duplicate note and is closed.
  - **Lost with no service message:** three attempts at least 10 s apart (patch `time.time`), then fallback.
  - The tick's fallback beats a late create: the topic is closed as a duplicate.
  - `closing` → the finished message is in the thread, the topic is closed and the state is `closed`.
  - A request on a closed ref → reopen, then present with the thread.
  - **Deleted, then an agent prompt:** the open request's present probes "deleted", so a new topic is created
    on the next scans. The cards are re-subscribed (new thread) and unsubscribed (old). The prompt is sent
    with the new thread's metadata, and the "continues here" note goes out.
  - **Deleted, then a coordinator wake:** with no open request, the 30 s probe (patch `time.monotonic`) finds
    the topic deleted, and the scans repair it. The ledger card's subscription calls name the new thread.
    A fake notifier `deliver(card)` in the test, which picks the latest subscription per card, lands in the
    new thread.
  - A restart (a new `gateway.State()`) with an `open` ref re-presents with the same thread, with no create.
- Verify: `python -m pytest -q tests/test_gateway.py`
- Exists because: only the gateway holds the bot, and #24 requires tested recovery for missing permissions,
  deleted topics and ambiguous creation.

## Task 5: sends carry the thread; answers are scoped to the topic
- Files: `muster/gateway.py`, `tests/test_gateway.py`.
- `present` and `present_approval` get `ready, meta = await destination(req["ledger"])`. When not ready, return
  without sending (no retry backoff). Pass `metadata=meta` to `send_clarify(...)`. Pass `send_exec_approval`
  by keyword: `metadata=meta, allow_permanent=False, allow_session=False`.
- `S.ledgers[rid] = req["ledger"]` at present, and in `scan` for every open request.
- `ask_for_text`: `send_message(..., message_thread_id=query.message.message_thread_id)`.
- `in_topic(ledger, thread)` is True when the ref has no thread (main-chat run), or when
  `str(thread) == ref.thread_id`.
- `guard`: after `refuse_unless_authorized`, take the rid from `cl:mu<rid>q`. If the topic does not match the
  query's `message.message_thread_id` (via `getattr`), call `query.answer("Moved: answer in the run's topic")`,
  reset that ledger's probe clock (so the next scan probes), and raise `ApplicationHandlerStop`.
  `approval_guard`: the same check, with the rid from the `muster:<rid>` session.
- `_dispatch`:
  - For a bound reply, if the ledger's topic does not match `getattr(event.source, "thread_id", None)`, return
    None (Hermes handles it).
  - The "Other" path counts only the waiting sessions whose ledger is `in_topic` for the event's thread.
- Test first:
  - Two concurrent runs, A and B, have open refs on threads 11 and 22. Their questions are sent with
    `metadata.thread_id` 11 and 22. Other-text in thread 22 resolves only B's prompt while both wait (today
    `len(waiting) == 1` refuses both). A tap from thread 22 on A's message is refused with "Moved". A
    reply in thread 11 to A's message resolves A.
  - All of these are presented with A's thread: a permission card, a build request (`recommend`), a feedback
    request (send-back), and a re-review card's subscription.
  - With topics off, every existing test passes unchanged, and `metadata` is None.
- Verify: `python -m pytest -q tests/test_gateway.py tests/test_bridge.py tests/test_decisions.py`
- Exists because: #24 requires that two concurrent dispatches never cross-route prompts, answers or commentary,
  and the "exactly one waiting" rule is the one route that guessed.

## Task 6: close on finish
- Files: `muster/cleanup.py`, `tests/test_cleanup.py`.
- `coding_target(..., ledger=None)` stores `"ledger"`. Issue targets pass `ledger` from the card file: in
  `targets()`, read the card file's `card` key where it already reads the file (cleanup.py:517), rather than
  from `intake_link` lazily. Run records pass `record.parent.name` when not owned. In `step()`, after a
  successful remove (`core.run(argv)` returned), call `conversation.finish(target["ledger"])` when a ledger is
  set (suppress OSError, and log it). `escalate` gets `target.get("ledger")`.
- Test first:
  - The removal of an issue worktree whose ledger has an `open` ref → `closing`.
  - The removal of a run → `closing`.
  - A dry run → unchanged.
  - An analysis workspace → no call.
- Verify: `python -m pytest -q tests/test_cleanup.py`
- Exists because: #24 says the topic closes only when the run is actually finished, and muster's finished is
  the cleanup removal.

## Task 7: docs
- Files: `README.md` (a "Topics" bullet under Decisions in the channel, plus a config row), the decision-channels
  doc (Slack: the reference's `thread_id` = `thread_ts`), and `docs/learnings.md` (one bullet per surprise).
- Contents:
  - Opt-in.
  - The prerequisites: Topics enabled on the group, and the bot as admin with Manage Topics.
  - The fallback to the main chat with its notice.
  - The 45 s launch wait.
  - Close on cleanup, and reopen.
  - Deleted-topic recovery and its 30 s window.
  - The lost-response reconciliation, and that it is unverified live.
  - Restart the gateway after an upgrade.
- Verify: `python -m pytest -q && hermes plugins validate .`
- Exists because: the human enables topics and grants the permission, so they need the steps.

## Amendments after the plan review (these supersede the tasks above where they conflict)
1. `closing`: swap to `closed` first, then send the finished message and close. A close error is logged, never
   retried. Thread-not-found / NOT_MODIFIED count as done. `destination()`'s reopen is the same: on an error, log
   it, set `open` and continue (the probe decides about deletion).
2. Adoption race: on a refused swap after a create returned T, reload the ref. Close T only when
   `ref.thread_id != T`. Duplicates are matched by name only among refs in `creating`/`pending` (adopt) or by
   "this ledger's ref is open on another thread" for the ref whose create attempts are recorded. The
   service-message handler checks only refs with `attempts > 0`.
3. `target()` keeps `notify_target()["chat_type"]` ("group": Hermes's inbound topic sessions are `group`,
   adapter.py:7183) and only adds `thread_id`.
4. Update `tests/test_cleanup.py:218`'s subscribe fake to accept `(card, ledger=None)`.
5. `telegram_factory` imports `MessageHandler, filters` and adds the handler. The fake `telegram.ext` gains both.
   Update the handler-list assertion at test_gateway.py:107.
6. `request()` returns an existing ref unchanged. It never raises: any error gives a `fallback` attempt plus a
   log line, and ultimately returns None. `subscribe` always runs after it. `wait` comes from a module constant
   `conversation.WAIT` (45) that tests patch.
7. Same as 6. The 45 s wait inside the launch lock is accepted (design trade-off), and the README states it.
8. Prerequisites: call `S.bot.get_chat(chat)`. `is_forum` False → definite fallback. An exception → unknown, which
   counts as an attempt. `get_chat_member`: status `creator`, or `administrator` with `can_manage_topics` → ok;
   other statuses → definite fallback; an exception → unknown.
9. Probe = `S.bot.reopen_forum_topic(chat, t)`: success or "not_modified" → alive (a topic closed by hand while
   muster says open is reopened); thread-not-found / topic_id_invalid / topic_deleted → deleted. No renames.
10. Cleanup ledger: resolve it in `step()` after a successful remove. For an issue target, use
    `target["link"].get("card", {}).get("card")` (link["card"] is the card-file dict). For a run, the record's
    parent name, passed as `coding_target(..., ledger=)`. `finish()` moves `open` or `closed`-after-reopen:
    `swap(("open",), "closing")`. A topic reopened after the worktree is gone stays open (accepted, documented).
11. `destination()` is called at the very top of `present` / `present_approval`, before any register, thread or
    `S.approvals.add`.
12. `in_topic(ledger, thread)`: a ref with no thread matches only an event thread of None or "1".
13. `topics()` runs at the top of `scan()`, before `open_requests()`.
14. `active()` skips `closed` refs and noticed `fallback` refs that have no `previous`. `repair()` runs in
    `asyncio.to_thread` (one background call per ref, guarded by `S.repairing`).
15. Tests patch `gateway.now` (time.time) and `gateway.clock` (time.monotonic), module-level aliases used only
    by topic code.
16. `core.subscribe` imports `conversation` lazily. `conversation` imports `config` and `core` at the top, and
    `decisions` lazily.
17. Accepted: the human verifies the service-message delivery live (asked 2026-10-09).
- Guess list: `name()` takes the slug and uses its short name. `add_card` with no ref is a no-op. `update`
  mkdirs. The probe clock is `S.probed[ledger]`. Field `user_id` = the requester (`notify_target().user_id`).
  `guard` with an unknown rid loads it via `decisions.load` (to_thread); a missing request → no topic check
  (Hermes then answers "expired"). The topic check runs before `ask_for_text`. The decision-channels doc's
  "exactly one muster prompt waits" line is updated to "in that topic".
