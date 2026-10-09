# Design: one Telegram topic per run (issue #24)

## Problem
Every run shares the notify chat. Its questions, permission cards, wait/review pings, the coordinator's review and
the build buttons interleave with every other run's. #24 asks for one forum topic per run, holding the whole
conversation, closed (not deleted) when the run is finished, reopened when it resumes, with a channel-neutral
reference that a later Slack `thread_ts` can fill.

## What Hermes already does (verified in the installed source, d2bac63)
- `hermes kanban notify-subscribe --thread-id` exists. The kanban notifier sends the ping with
  `metadata.thread_id`, and a `notify+wake` turn runs with a `SessionSource` carrying that thread. So the
  coordinator's wake, its commentary, and `[SILENT]` already land in the topic once the card's subscription
  names it (gateway/kanban_watchers_notifier.py ~699-734). **No coordinator code changes.**
- `send_clarify`, `send_exec_approval` and `send` take `metadata={"thread_id": ...}` (telegram adapter
  T:4376, base.py:2895, T:3728). `edit_message` edits by id, so it needs no thread.
- `adapter.create_handoff_thread(chat_id, name)` (public, T:2631) calls Telegram createForumTopic and returns
  the thread id or None. It swallows every error.
- No adapter method closes or reopens a topic. Hermes's own PTB `app.bot` (muster already holds it for the
  ForceReply) has `close_forum_topic` / `reopen_forum_topic` / `get_chat_member`.
- Hermes isolates sessions per forum topic. Inbound `event.source.thread_id` is set in a topic, and General
  is None. A deleted topic: Hermes retries the send without a thread, so the message lands in General.

## Chosen approach
1. **Opt-in setting** `notify_topics: false` (the default). When on, it needs `notify_platform: telegram` and a
   group `notify_chat_id`, and `config.require()` refuses anything else. Runs launched before the switch have no
   reference and stay in the main chat. Nothing migrates.
2. **The reference.** `<data dir>/runs/<ledger>/conversation.json` holds `{platform, chat_id, thread_id,
   requester_user_id, name, state, why, attempts}`. `state` is one of `pending | open | closing | closed |
   fallback`. `thread_id` is Telegram's `message_thread_id` now and Slack's `thread_ts` later. One function,
   `core.conversation(ledger)`, reads it and returns the main-chat target when no file exists.
3. **Creation (gateway only; the tick has no bot).** At launch, after the card is made and before
   `subscribe`, the tick writes `state: pending` and waits up to 45 s for the gateway to fill the reference.
   - **Prerequisites first.** The gateway scan (every 2 s) checks that `get_chat_info` reports a forum and that
     `get_chat_member(bot).can_manage_topics` is true. A failure gives `fallback` with an actionable reason
     ("enable Topics on the group", "give the bot Manage Topics").
   - **Then create.** It saves `creating` with `attempt` and `since` before it calls `create_handoff_thread`,
     and saves `open` + `thread_id` on success.
   - **Lost response, reconciled.** Telegram posts a `forum_topic_created` service message into every new topic.
     Bots receive service messages even in privacy mode, Hermes asks for all update types, and Hermes itself
     reads these messages to learn topic names (T:7118). muster adds one `MessageHandler(FORUM_TOPIC_CREATED)` in
     group -1 through the same `register_telegram_handler` hook it already uses. It never stops propagation.
     A service message counts as muster's topic only when all three hold: it is in the notify chat, it was sent
     by the bot itself, and its name equals a `creating` reference's name. That adopts the thread id:
     `open`, no second create.
   - **Retry only after silence.** If the create returned None and no matching service message arrived within
     10 s, the gateway re-checks the prerequisites and tries again, up to 3 attempts, then `fallback`.
   - **A late duplicate is closed.** A service message for an earlier attempt that arrives after the reference
     is already `open` on another thread gets one message ("Duplicate topic, not used; this run is in <topic
     name>") and is closed. So at most one topic is ever live for a run.
   - **Subscribe and fallback.** The tick then subscribes the ledger to whatever the reference says. If the
     gateway is down or the wait times out, the tick writes `fallback` itself, under the reference's lock, so a
     late adoption cannot overwrite it; that late topic is then closed as a duplicate. Every fallback is said
     once, in the main chat, as a ledger comment plus a ping: "Topic not created: <why>; this run stays in the
     main chat".
4. **Propagation.** `core.subscribe(card, ledger)` passes `--thread-id` from the reference for every card of
   the run: the ledger, wait cards, review cards and cleanup warnings. That alone moves pings and coordinator
   wakes into the topic. In the gateway, `present`, `present_approval`, the Other ForceReply and the restart
   re-present all send with the ledger's thread. Build and send-back requests are already keyed by ledger,
   so they follow too.
5. **Authorization stays fail-closed and gets narrower.**
   - Taps and replies keep the user+chat check and the binding by message id.
   - The one route that guessed, "free text after Other while exactly one prompt waits", now counts only the
     prompts in the topic the text arrived in. Two concurrent runs can then each have an Other pending without
     both being refused or cross-routed.
   - A tap or reply must also come from the request's current topic. A muster message found anywhere else
     (Hermes rerouted it to General because the topic was deleted) is never answered there. The tap gets
     "Moved: answer in the run's topic", and it triggers the repair in step 7.
   - Callback expiry, replay guards and `decisions.transition` stay unchanged.
6. **Close and reopen.**
   - When cleanup removes a run's worktree (merged, clean, quiet: muster's definition of finished), it sets
     the reference to `closing`. The gateway posts "Run finished; worktree removed. History kept.", calls
     `close_forum_topic`, and sets `closed`.
   - When the gateway is about to present a request for a `closed` run (a resumed run, a late question or
     review), it calls `reopen_forum_topic` first and sets `open`. The topic is never deleted.
   - Kanban pings to a closed topic still arrive, because a bot with `can_manage_topics` may post in a closed
     topic.
7. **Deleted topic: detected, recreated, rerouted.** Hermes silently resends to General when a thread is gone,
   so muster checks the topic itself. The probe is `bot.edit_forum_topic(chat, thread, name=<same name>)`.
   Telegram answers `TOPIC_NOT_MODIFIED` (or success) for a live topic and a thread-not-found / `TOPIC_ID_INVALID`
   error for a deleted one. Any other error is "unknown", and unknown changes nothing.
   - **When it probes:** right before every muster prompt is sent; every 30 s for each `open` reference with an
     open request or a live card; and when a stray message in General was tapped (step 5).
   - **On "deleted":**
     1. It creates a new topic. This is the same reconciled create as step 3, with `previous: [old thread ids]`
        kept on the reference.
     2. It re-subscribes every card recorded on the reference. `core.subscribe` appends each card id to
        `cards`; the gateway subscribes the new thread and unsubscribes the old one with `hermes kanban
        notify-subscribe/notify-unsubscribe`.
     3. It re-presents the run's open requests in the new topic. Their old messages are edited to "Moved to the
        new topic", and old buttons are refused by the topic check.
     4. It posts "The previous topic was deleted; this run continues here."
   - **Result:** the next agent prompt goes straight to the new topic. The next coordinator wake does too,
     because Hermes's notifier reads the repaired subscription. The one window is a kanban event within 30 s
     of the deletion, before a probe runs. Hermes's own fallback then delivers that ping to General, so it is
     visible, not lost.
8. **Restart/retry/recover.** The reference lives in the data dir, so it survives gateway restarts, `recover`
   and revisions. A re-present after a restart reuses the thread. The existing "Superseded" edits retire
   stale controls.

## Cut, and why
- **Telegram calls from the tick or the CLI** (direct Bot API with the token). That would be a second
  Telegram transport, which #24 forbids.
- **Slack delivery.** Only the data model is built (#24 scope). The decisions doc gains a line about it.
- **A topic per analysis workspace.** It has no ledger and no prompts.
- **Migrating existing runs.** #24 says opt-in and no silent split. The cost: a run launched before the switch
  stays in the main chat until it ends.

## Safety boundaries
- No change to who may answer: the notify user in the notify chat, plus the narrower topic check above.
- No topic is ever deleted. No bot permission or group setting is changed by muster or by me. Enabling topics
  and granting `can_manage_topics` are the human's steps, listed in the README.
- Close and reopen use Hermes's PTB bot (public PTB API), not a private Hermes name. The one new reliance,
  `create_handoff_thread`, is public. Nothing new goes in `hermes_private.py`.

## Trade-offs
- Reconciling a lost create depends on the service message. If Telegram drops it too, the third attempt can
  make an empty topic, and when its service message turns up late it is closed as a duplicate.
- The launch waits up to 45 s (three attempts) for the gateway, once per run. A tick with several new
  approvals launches them one after another.
- One `editForumTopic` probe per muster prompt, plus one every 30 s per active run.

## Test plan (fakes extended: create_handoff_thread, close/reopen, get_chat_member, thread on sends)
- Two concurrent runs: each ledger, wait card and review card is subscribed with its own `--thread-id`. Each
  request is presented in its own topic. Other-text in topic A resolves only A's prompt, and a tap from topic B
  on A's message is refused.
- A design approval question, a free-text answer, a permission card, a recommendation (build request), a
  send-back (feedback request) and a re-review card all carry run A's thread.
- A restart re-presents with the same thread. The reference is not recreated, and `attempts` stops at 3.
- Lost response: the fake `create_handoff_thread` makes the topic but returns None, and the service message
  arrives. The reference adopts that thread, a second create is never called, and a late duplicate service
  message for an older attempt is closed with its note.
- Deleted topic, then an agent prompt: the probe says deleted, so a new topic is created and the prompt is sent
  with the new thread. The card subscriptions move to the new thread (old one unsubscribed), and a tap on the
  old message is refused with "Moved".
- Deleted topic, then a coordinator wake: the 30 s probe repairs it. A completed ledger's subscription (read
  back with `notify-list`) names the new thread, so Hermes's notifier wake source routes there. The fake
  notifier delivers by subscription.
- Prerequisites: not a forum, or no `can_manage_topics`, gives `fallback`, a main-chat subscription and the
  notice. Three None creates give `fallback`. The gateway down at launch gives an immediate `fallback`. A late
  create after the tick's fallback does not overwrite it.
- Cleanup removal gives `closing`, then the finished message and close. A new request gives a reopen and then
  the present.
- `notify_topics` off: byte-identical behaviour (no `--thread-id`, no metadata), and the existing suite stays
  green. `config.require` rejects topics without a group chat or on another platform.
- `pytest -q` and `hermes plugins validate .`. No live Telegram run: enabling topics on the group is the
  human's call (#24 scope note).

## Decision needed
Approve this design, in particular: (a) the gateway creates the topic while the launch waits up to 45 s, with
a stated fallback to the main chat, and a lost create response is reconciled by the bot's own
`forum_topic_created` service message; (b) a run counts as finished when cleanup removes its worktree, and that
is when the topic closes; (c) a deleted topic is detected by an `editForumTopic` probe (before every prompt and
every 30 s), then recreated, with subscriptions and open prompts moved to the new topic.
