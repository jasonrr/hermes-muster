"""The private Hermes names muster relies on (muster/hermes_private.py), checked against the real source.

If Hermes renames or reshapes one of them, these fail loudly. Skipped when no Hermes checkout is present.
"""

import ast
import inspect
from pathlib import Path

import pytest

from muster import hermes_private

HERMES = Path("/Users/jasonrosoff/.hermes/hermes-agent")
pytestmark = pytest.mark.skipif(not HERMES.is_dir(), reason=f"no Hermes checkout at {HERMES}")


def params(path, name):
    """Parameter names and defaults of function `name` in a source file, without importing it."""
    tree = ast.parse((HERMES / path).read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    a = fn.args
    return [x.arg for x in [*a.posonlyargs, *a.args]], [x.arg for x in a.kwonlyargs]


def signature(module, path, name):
    """(positional, keyword-only) names from the imported function, else from its source."""
    import importlib
    import sys
    sys.path.insert(0, str(HERMES))
    try:
        fn = getattr(importlib.import_module(module), name)
        sig = inspect.signature(fn)
        return ([p.name for p in sig.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)],
                [p.name for p in sig.parameters.values() if p.kind == p.KEYWORD_ONLY])
    except Exception:  # noqa: BLE001 - Hermes's own dependencies may be missing here
        return params(path, name)
    finally:
        sys.path.remove(str(HERMES))


def test_await_gateway_decision_still_takes_what_await_approval_passes():
    positional, keyword = signature("tools.approval_gateway_wait", "tools/approval_gateway_wait.py",
                                    "_await_gateway_decision")
    assert positional[:3] == ["session_key", "notify_cb", "approval_data"] and "surface" in keyword + positional


def test_the_approval_functions_the_gateway_calls_exist_with_their_arguments():
    positional, _ = params("tools/approval.py", "resolve_gateway_approval")
    assert positional[:2] == ["session_key", "choice"]
    assert "reason" in positional or "reason" in params("tools/approval.py", "resolve_gateway_approval")[1]
    positional, _ = params("tools/approval.py", "withdraw_gateway_approval")
    assert positional[:3] == ["session_key", "request_id", "cause"]


def test_the_clarify_functions_the_gateway_calls_exist_with_their_arguments():
    path = "tools/clarify_gateway.py"
    assert params(path, "register")[0][:5] == ["clarify_id", "session_key", "question", "choices", "multi_select"]
    assert params(path, "wait_for_response")[0] == ["clarify_id", "timeout"]
    assert params(path, "get_pending_for_session") == (["session_key"], ["include_choice_prompts"])
    for name, expect in (("resolve_text_response_for_session", ["session_key", "response"]),
                         ("mark_awaiting_text", ["clarify_id"]), ("clear_session", ["session_key"]),
                         ("resolve_gateway_clarify", ["clarify_id", "response"])):
        assert params(path, name)[0] == expect, name
    source = (HERMES / path).read_text()
    assert 'CANCELLED = "\\x00cancelled"' in source  # the gateway's waiter ignores values starting with \x00


def test_is_user_authorized_still_takes_a_source():
    positional, _ = signature("gateway.authz_mixin", "gateway/authz_mixin.py", "_is_user_authorized")
    assert positional == ["self", "source"]


def test_the_adapter_methods_the_gateway_calls_exist():
    tree = ast.parse((HERMES / "gateway/platforms/base.py").read_text())  # the adapter contract
    found = {n.name: [a.arg for a in n.args.args + n.args.kwonlyargs]
             for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"chat_id", "command", "session_key", "allow_permanent", "allow_session"} <= set(found["send_exec_approval"])
    assert {"chat_id", "question", "choices", "clarify_id", "session_key"} <= set(found["send_clarify"])
    assert {"chat_id", "message_id", "content"} <= set(found["edit_message"])


def test_user_authorized_fails_closed():
    class No:
        _is_user_authorized = staticmethod(lambda source: False)

    assert hermes_private.user_authorized(object(), "s") is False
    assert hermes_private.user_authorized(No(), "s") is False
