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


def test_the_adapter_methods_the_gateway_calls_exist():
    tree = ast.parse((HERMES / "gateway/platforms/base.py").read_text())  # the adapter contract
    found = {n.name: [a.arg for a in n.args.args + n.args.kwonlyargs]
             for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"chat_id", "command", "session_key", "allow_permanent", "allow_session"} <= set(found["send_exec_approval"])
    assert {"chat_id", "message_id", "content"} <= set(found["edit_message"])


def test_the_telegram_adapter_still_keeps_approval_state():
    files = list((HERMES / "plugins/platforms/telegram").glob("*.py")) + list((HERMES / "gateway/platforms").glob("telegram*.py"))
    init = [n for f in files for c in ast.walk(ast.parse(f.read_text())) if isinstance(c, ast.ClassDef)
            for n in c.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
            and "_approval_state" in (ast.get_source_segment(f.read_text(), n) or "")]
    assert init, "no Telegram adapter __init__ mentions _approval_state: hermes_private.approval_session is stale"


def test_approval_session_reads_the_adapters_state_and_returns_none_on_bad_data():
    class Adapter:
        _approval_state = {7: "muster:abc"}

    assert hermes_private.approval_session(Adapter(), "ea:once:7") == "muster:abc"
    assert hermes_private.approval_session(Adapter(), "ea:once:8") is None
    assert hermes_private.approval_session(Adapter(), "ea:junk") is None
    assert hermes_private.approval_session(Adapter(), "ea:once:x") is None
    assert hermes_private.approval_session(object(), "ea:once:7") is None


def test_send_labelled_clarify_still_matches_hermess_send_clarify():
    path = "plugins/platforms/telegram/adapter.py"
    source = (HERMES / path).read_text()
    assert params(path, "_send_prompt") == (["self", "what", "chat_id", "metadata", "build"],
                                            ["parse_mode", "thread_id", "reply_to_mode"])
    clarify = ast.get_source_segment(source, next(
        n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "send_clarify"))
    for shape in ('callback_data=f"cl:{clarify_id}:{idx}"', 'callback_data=f"cl:{clarify_id}:other"',
                  't("platform.telegram.prompt.other")', "self._clarify_state.__setitem__(clarify_id, session_key)",
                  "self._send_prompt(", "parse_mode=ParseMode.HTML", "_html.escape(question)"):
        assert shape in clarify, f"send_clarify changed ({shape}): hermes_private.send_labelled_clarify is stale"
    assert "self._clarify_state: Dict[str, str] = {}" in source
