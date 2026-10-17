# Decision channels: the transport-neutral core and the adapter contract (issue #17, 2026-10-08)

Telegram is the first decision surface. Slack is the second (design `docs/plans/2026-10-17-channels-design.md`).

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
  4. **Edit** the message to the request's `outcome` when it ends, so the human knows what became of the answer.
     A hook-delivered answer reads "Sent to Claude" / "Allow sent to Claude" / "Deny sent to Claude"
     (jasonrr/hermes-muster#21). That label means the PermissionRequest hook printed the answer, not that
     Claude applied it: the pane can answer, or Claude can exit, around that write. Confirming would take a
     PostToolUse acknowledgment, which was cut in #17 and cannot see a deny anyway. An allow is permission,
     never a command that succeeded. A ✓ is kept for outcomes muster verified itself (the feedback "Sent ✓",
     checked against the prompt Claude received).

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
  approval card); so does text after "Other" when exactly one muster prompt waits in the conversation (run topic,
  or the main chat) the text was typed in. It returns
  `{"action": "skip"}` only for those. After "Other", muster sends a ForceReply (group privacy mode).
- **Restart.** Hermes keeps prompts in memory. The scan presents open requests again each boot; a tap on an
  older message gets Hermes's own "expired" notice and is edited to "Superseded". Every message id ever sent stays bound.

## Run topics (issue #24)

- The conversation a run's messages go to is `muster.conversation`: one reference per ledger with `platform`,
  `chat_id`, `thread_id`, the requester and a state. Every card subscription passes its `thread_id` to Hermes
  (`notify-subscribe --thread-id`), and every gateway send passes it as `metadata["thread_id"]`. Taps and replies
  are checked against it.
- Telegram's `thread_id` is the forum topic's `message_thread_id`. The gateway owns its lifecycle: create,
  reconcile, probe, recreate, close and reopen.

## Slack

Superseded by `docs/plans/2026-10-17-channels-design.md` (approved 2026-10-17), built under issue #43. The contract
above assumed plugin action handlers run before Hermes's own on Slack, as the Hermes docs say. The Slack adapter's
source registers its `hermes_clarify_*` and `hermes_approve_*` listeners first, and Bolt runs only the first match,
so a plugin cannot guard Hermes's own Slack prompts. muster's Slack channel (`muster/channels/slack.py`) therefore
posts its own Block Kit messages with `muster_*` action ids and authorizes every tap itself. It needs no
`notify_slack_*` settings: the generic `notify_*` keys cover Slack, and `notify_user_id` holds the approvers.
