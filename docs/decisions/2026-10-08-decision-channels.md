# Decision channels: the transport-neutral core and the adapter contract (issue #17, 2026-10-08)

Telegram is the first decision surface. Slack is a requirement for the architecture, not built.

## The core (any channel)

- `muster/decisions.py` owns the decision request. That covers its file, its status
  (`open → answered → executing → done | failed | stale`), its audit trail, and every effect: the
  execution of merge, send-back or do-nothing, and recovery after a crash.
- `muster/bridge.py` is the pane side. It turns a Claude dialog into a request and hands the answer back
  to Claude. It never knows which channel answered.
- A channel adapter reuses Hermes's own prompt surfaces; it adds only what Hermes lacks
  (jasonrr/hermes-muster#20).
  1. **Present** each `open` request with Hermes's prompts (clarify for questions and build choices, the
     approval card for permissions). Save the channel's message ids on `presented.messages` with
     `decisions.update`.
  2. **Authorize** every tap and reply against the configured human and conversation, before any state
     changes, on top of Hermes's own allowlist (being allowed to talk to the Hermes agent is not permission
     to answer muster). Refuse everyone else, and log it.
  3. **Map** a tap or a reply back to the request and the question, by message id, never by guessing from
     unrelated chat. Then move the request `open → answered` with `decisions.transition` (its refusal is
     the double-click guard), and call `gateway.on_answered`.
  4. **Edit** the message to the request's `outcome` when it ends, so the human knows the answer arrived.

## Telegram (built: `muster/gateway.py`)

- **Present.** Questions: Hermes's `adapter.send_clarify` and `tools.clarify_gateway`; the clarify id is
  `mu<request id>q<n>`, the session key `muster:<id>:<n>` (one per question, so Hermes's typed-text
  resolution targets that question). Permissions: `adapter.send_exec_approval` with Hermes's approval wait
  (`hermes_private.await_approval`), session key `muster:<id>`.
- **Authorize.** PTB `CallbackQueryHandler`s in group -1, ahead of Hermes's: `^cl:mu` (muster clarify
  prompts) and `^ea:` (approval cards, applied only to a card whose session is `muster:...`, found through
  `hermes_private.approval_session`). Each compares the tapper's user and chat with `core.notify_target()`,
  and on a mismatch answers "Not authorized" and raises `ApplicationHandlerStop`.
- **Map replies.** The `pre_gateway_dispatch` hook. A reply to a stored message id resolves that prompt
  through Hermes (`mark_awaiting_text` + `resolve_text_response_for_session`, or a deny with reason on an
  approval card); so does text after "Other" when exactly one muster prompt waits. It returns
  `{"action": "skip"}` only for those. After "Other", muster sends a ForceReply (group privacy mode).
- **Restart.** Hermes keeps prompts in memory. The scan presents open requests again each boot; a tap on an
  older message gets Hermes's own "expired" notice and is edited to "Superseded". Every message id ever sent stays bound.

## Slack (contract only)

- **Present.** The Hermes Slack adapter already implements `send_clarify(chat_id, question, choices,
  clarify_id, session_key)` with Block Kit buttons. The scan's `present` works unchanged once it calls the
  Slack adapter. Capture that adapter with `ctx.register_platform_handler("slack", factory)`, the
  equivalent of `register_telegram_handler`.
- **Authorize.** Register `ctx.register_slack_action_handler(<regex matching muster ids>, callback)` ahead
  of the core one, comparing `body["user"]["id"]` and the channel with new `notify_slack_user_id` and
  `notify_slack_channel` settings. Check first whether Hermes runs plugin action handlers before its own.
- **Map replies.** `pre_gateway_dispatch` already sees Slack messages. A thread reply carries the parent
  `ts` as its reply id, so key `presented.messages` by `ts`. `on_dispatch` currently returns early for
  any platform but telegram. Generalize that check to "the configured decision platform".
- **Config.** `notify_platform: slack` selects the adapter. Wait cards then skip their subscription only when Hermes's runtime
  status shows the Slack platform connected (`decisions.gateway_up`), as with Telegram.
- **Not needed.** No change to `decisions.py`, `bridge.py`, `recommend`, execution, recovery or
  re-review.
