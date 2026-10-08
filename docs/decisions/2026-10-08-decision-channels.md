# Decision channels: the transport-neutral core and the adapter contract (issue #17, 2026-10-08)

Telegram is the first decision surface. Slack is a requirement for the architecture, not built.

## The core (any channel)

- `muster/decisions.py` owns the decision request. That covers its file, its status
  (`open → answered → executing → done | failed | stale`), its audit trail, and every effect: the
  execution of merge, send-back or do-nothing, and recovery after a crash.
- `muster/bridge.py` is the pane side. It turns a Claude dialog into a request and hands the answer back
  to Claude. It never knows which channel answered.
- A channel adapter does four things. Everything else is the core's.
  1. **Present** each `open` request: one prompt per question, its options as native choices, plus free
     text. Save the channel's message ids on `presented.messages` with `decisions.update`.
  2. **Authorize** every tap and reply against the configured human and conversation, before any state
     changes. Refuse everyone else, and log it.
  3. **Map** a tap or a reply back to the request and the question, by message id and option index, never
     by guessing from unrelated chat. Then move the request `open → answered` with `decisions.transition`
     (its refusal is the double-click guard), and call `gateway.on_answered`.
  4. **Edit** the message to the request's `outcome` when it ends.

## Telegram (built: `muster/gateway.py`)

- **Present.** Hermes's `adapter.send_clarify` and `tools.clarify_gateway`. The clarify id is
  `mu<request id>q<n>`, and the session key is `muster:<id>`.
- **Authorize.** A PTB `CallbackQueryHandler(pattern="^cl:mu")` in group -1. It compares the clicker's user
  id and chat id with `core.notify_target()`. On a mismatch it answers "Not authorized" and raises
  `ApplicationHandlerStop`. Hermes's core clarify handler (group 0) then does the rest.
- **Map replies.** The `pre_gateway_dispatch` hook. It handles a reply to a stored message id, or text
  after "Other" when exactly one muster prompt waits. It returns `{"action": "skip"}` only for those.
- **Restart.** Clarify state is in memory. The scan re-presents open requests and marks older messages
  "Superseded". Every message id ever sent stays bound.

## Slack (contract only)

- **Present.** The Hermes Slack adapter already implements `send_clarify(chat_id, question, choices,
  clarify_id, session_key)` with Block Kit buttons. The scan's `present` works unchanged once it calls the
  Slack adapter. Capture that adapter with `ctx.register_platform_handler("slack", factory)`, the
  equivalent of `register_telegram_handler`.
- **Authorize.** Register `ctx.register_slack_action_handler(<regex matching muster clarify ids>,
  callback)` ahead of the core one. It compares `body["user"]["id"]` and the channel with new
  `notify_slack_user_id` and `notify_slack_channel` settings, and refuses otherwise. Check first whether
  Hermes runs plugin action handlers before its own clarify handler; if it does not, the guard must
  resolve the clarify itself.
- **Map replies.** `pre_gateway_dispatch` already sees Slack messages. A thread reply carries the parent
  `ts` as its reply id, so key `presented.messages` by `ts`. `on_dispatch` currently returns early for
  any platform but telegram. Generalize that check to "the configured decision platform".
- **Config.** `notify_platform: slack` selects the adapter. Wait cards then use `wake` only when the Slack
  side is running (the `.gateway` heartbeat), as with Telegram.
- **Not needed.** No change to `decisions.py`, `bridge.py`, `recommend`, execution, recovery or
  re-review.
