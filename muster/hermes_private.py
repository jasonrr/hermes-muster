"""hermes_private: every private Hermes name muster relies on, and nothing else (see AGENTS.md).

Each function names the Hermes source it depends on and the public API that would replace it. Upstream asks:
see jasonrr/hermes-muster#20. tests/test_hermes_private.py fails loudly when
a shape here changes.
"""


def await_approval(session_key, notify, command, request_id):
    """Queue a Hermes approval and block until it is answered, withdrawn or `approvals.timeout` passes.

    Relies on tools/approval_gateway_wait.py `_await_gateway_decision` (Hermes's MCP elicitation calls it the
    same way, tools/approval_prompt.py). Wanted upstream: a public `request_approval(session_key, command, ...)`
    for plugins. Returns Hermes's {"resolved", "choice", "reason", "cancelled"?, "notify_failed"?}.
    """
    from tools import approval_gateway_wait

    return approval_gateway_wait._await_gateway_decision(
        session_key, notify, {"command": command, "request_id": request_id,
                              "pattern_key": "muster_permission", "pattern_keys": ["muster_permission"]},
        surface="muster")


def approval_session(adapter, callback_data):
    """The session key of the Hermes approval card an `ea:<choice>:<id>` tap is for, or None.

    muster's cards must take only the notify user's tap, while Hermes's own cards keep Hermes's rules, so the
    guard must tell them apart. Relies on plugins/platforms/telegram/adapter.py `_approval_state` (approval id ->
    session key). Wanted upstream: a session-key or plugin-namespace filter for approval callbacks.
    """
    try:
        return (getattr(adapter, "_approval_state", None) or {}).get(int(str(callback_data).split(":", 2)[2]))
    except (ValueError, IndexError):
        return None


async def send_labelled_clarify(adapter, chat_id, text, labels, clarify_id, session_key, metadata=None):
    """Hermes's clarify prompt with each option's label on its button, and no numbered legend in the text.

    Hermes's `send_clarify` (plugins/platforms/telegram/adapter.py:4376) shows numbers only on its buttons and lists
    the options under the text. This is a labelled copy that goes through the same send shell (`_send_prompt`,
    adapter.py:4265) and makes the same `_clarify_state` record (adapter.py:4391). It also uses the same callback
    data (`cl:<id>:<idx>`, `cl:<id>:other`) and the same Other label. So Hermes's tap handler, its choice mapping
    (choices[idx] as registered), the expired notice and Other's text capture all apply unchanged. Wanted upstream:
    `send_clarify(..., button_labels=[...])`.
    """
    import html

    from agent.i18n import t
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode

    def build():
        rows = [[InlineKeyboardButton(label, callback_data=f"cl:{clarify_id}:{i}")] for i, label in enumerate(labels)]
        rows.append([InlineKeyboardButton(t("platform.telegram.prompt.other"), callback_data=f"cl:{clarify_id}:other")])
        return (f"❓ {html.escape(text)}", InlineKeyboardMarkup(rows),
                lambda msg: adapter._clarify_state.__setitem__(clarify_id, session_key))

    # metadata (a run topic's thread_id) is routed exactly as send_clarify routes it (adapter.py:4394)
    return await adapter._send_prompt("send_clarify", chat_id, metadata, build, parse_mode=ParseMode.HTML,
                                      thread_id=adapter._metadata_thread_id(metadata))
