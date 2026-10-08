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


def user_authorized(gateway, source):
    """Hermes's own authorization for an inbound message (allowlists, pairing).

    pre_gateway_dispatch runs before Hermes authorizes (hermes_cli/plugins.py), so a hook that consumes a
    message must check itself. Relies on gateway/authz_mixin.py `GatewayRunner._is_user_authorized`. Wanted
    upstream: authorization before pre_gateway_dispatch, or a public `is_authorized(source)`. Fails closed.
    """
    check = getattr(gateway, "_is_user_authorized", None)
    return bool(check and check(source))
