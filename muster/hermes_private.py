"""hermes_private: every private Hermes name muster relies on, and nothing else (see AGENTS.md).

Each function names the Hermes source it depends on and the public API that would replace it. Upstream asks:
see jasonrr/hermes-muster#20. tests/test_hermes_private.py fails loudly when
a shape here changes.
"""

from types import SimpleNamespace


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

    Hermes's `send_clarify` (plugins/platforms/telegram/adapter.py:4273) shows numbers only on its buttons and lists
    the options under the text. This is a labelled copy that goes through the same send shell (`_send_prompt`,
    adapter.py:4162) and makes the same `_clarify_state` record (adapter.py:4288). It also uses the same callback
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

    # metadata (a run topic's thread_id) is routed exactly as send_clarify routes it (adapter.py:4290)
    return await adapter._send_prompt("send_clarify", chat_id, metadata, build, parse_mode=ParseMode.HTML,
                                      thread_id=adapter._metadata_thread_id(metadata))


async def send_document(adapter, chat_id, path, name, caption, metadata=None):
    """A local file as a Telegram document in the run's topic, with a failure reported as a failure.

    Hermes's public `send_document` (plugins/platforms/telegram/adapter.py:5363) answers a failed upload by posting a
    "couldn't send file" notice and returning that notice's result, success included, so muster could not tell a
    delivered file from a failed one, and would claim documents the human never got. This goes through the same send
    shell (`_send_local_file`, adapter.py:5341: existence check, open, topic routing and retry in `_send_media`) with
    an `on_error` that returns the failure instead of the notice. Wanted upstream: `send_document` reporting a failed
    upload as `success=False` (the notice may still post).
    """
    async def failed(caught):
        return SimpleNamespace(success=False, message_id=None, error=str(caught) or type(caught).__name__)

    return await adapter._send_local_file(
        "File", path, chat_id, None, metadata, "document",
        lambda f: {"document": f, "filename": name, "caption": caption[:1024]}, failed)
