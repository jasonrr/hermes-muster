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
