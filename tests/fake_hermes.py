"""The slice of Hermes the gateway touches: tools.clarify_gateway (real semantics), telegram.ext, an adapter.

clarify_gateway mirrors hermes-agent/tools/clarify_gateway.py: register / wait_for_response (blocks, pops the
entry) / resolve_gateway_clarify (False once resolved or unknown) / get_pending_for_session / mark_awaiting_text.
"""

import json
import sys
import threading
import types
from types import SimpleNamespace


class Entry:
    def __init__(self, clarify_id, session_key, question, choices, multi):
        self.clarify_id, self.session_key, self.question = clarify_id, session_key, question
        self.choices, self.multi_select = list(choices) if choices else None, multi
        self.event, self.response, self.awaiting_text = threading.Event(), None, not choices


def clarify_module():
    mod = types.ModuleType("tools.clarify_gateway")
    mod._entries, mod._index, mod._lock = {}, {}, threading.RLock()

    def register(clarify_id, session_key, question, choices, multi_select=False):
        entry = Entry(clarify_id, session_key, question, choices, bool(multi_select) and bool(choices))
        with mod._lock:
            mod._entries[clarify_id] = entry
            mod._index.setdefault(session_key, []).append(clarify_id)
        return entry

    def wait_for_response(clarify_id, timeout):
        with mod._lock:
            entry = mod._entries.get(clarify_id)
        if entry is None:
            return None
        while not entry.event.wait(timeout=0.05):
            pass
        with mod._lock:
            mod._entries.pop(clarify_id, None)
            ids = mod._index.get(entry.session_key) or []
            if clarify_id in ids:
                ids.remove(clarify_id)
                if not ids:
                    mod._index.pop(entry.session_key, None)
        return entry.response

    def resolve_gateway_clarify(clarify_id, response):
        with mod._lock:
            entry = mod._entries.get(clarify_id)
            if entry is None or entry.event.is_set():
                return False
            entry.response = str(response)
            entry.event.set()
            return True

    def get_pending_for_session(session_key, *, include_choice_prompts=False):
        with mod._lock:
            for cid in mod._index.get(session_key) or []:
                entry = mod._entries.get(cid)
                if entry is not None and (include_choice_prompts or entry.awaiting_text):
                    return entry
        return None

    def mark_awaiting_text(clarify_id):
        with mod._lock:
            entry = mod._entries.get(clarify_id)
            if entry is not None:
                entry.awaiting_text = True
            return entry is not None

    def clear_session(session_key):
        with mod._lock:
            cancelled = 0
            for entry in (mod._entries.pop(cid, None) for cid in list(mod._index.pop(session_key, []) or [])):
                if entry is None or entry.event.is_set():
                    continue
                entry.response = mod.CANCELLED
                entry.event.set()
                cancelled += 1
            return cancelled

    def label(text, choices):
        return next((str(c).strip() for c in choices if str(c).strip().casefold() == text.strip().casefold()), None)

    def pick(token, choices):
        if token.isdigit():
            i = int(token) - 1
            return str(choices[i]).strip() if 0 <= i < len(choices) else None
        return label(token, choices)

    def coerce(entry, text):
        """(value, None) or (None, "invalid_selection" | "prose"), as the real _coerce_text_response_detailed."""
        text = text.strip()
        if not entry.choices:
            return text, None
        tokens = [t.strip() for t in text.split(",") if t.strip()] if "," in text else text.split()
        numeric = all(t.isdigit() for t in tokens) and bool(tokens)
        if entry.multi_select:
            picked = [pick(t, entry.choices) for t in (tokens if "," in text or numeric else [text])]
            got = None if not picked or None in picked else list(dict.fromkeys(picked))
            coerced = json.dumps(got) if got else None
            shaped = numeric or "," in text
        else:
            shaped = text.lstrip("-").isdigit()
            i = int(text) - 1 if shaped else -1
            coerced = str(entry.choices[i]).strip() if 0 <= i < len(entry.choices) else label(text, entry.choices)
        if coerced is not None:
            return coerced, None
        if entry.awaiting_text:
            return text, None
        return None, "invalid_selection" if shaped else "prose"

    def attempt_text_response_for_session(session_key, response):
        entry = get_pending_for_session(session_key, include_choice_prompts=True)
        if entry is None:
            return mod.TEXT_NO_PENDING
        value, reason = coerce(entry, response)
        if value is None:
            return mod.TEXT_REJECTED_SELECTION if reason == "invalid_selection" else mod.TEXT_REJECTED_PROSE
        return mod.TEXT_RESOLVED if resolve_gateway_clarify(entry.clarify_id, value) else mod.TEXT_NO_PENDING

    def resolve_text_response_for_session(session_key, response):
        return attempt_text_response_for_session(session_key, response) == mod.TEXT_RESOLVED

    mod.TEXT_RESOLVED, mod.TEXT_REJECTED_PROSE = "resolved", "rejected_prose"
    mod.TEXT_REJECTED_SELECTION, mod.TEXT_NO_PENDING = "rejected_selection", "no_pending"
    mod.CANCELLED = "\x00cancelled"
    mod.register, mod.wait_for_response = register, wait_for_response
    mod.resolve_gateway_clarify, mod.get_pending_for_session = resolve_gateway_clarify, get_pending_for_session
    mod.mark_awaiting_text, mod.clear_session = mark_awaiting_text, clear_session
    mod.attempt_text_response_for_session = attempt_text_response_for_session
    mod.resolve_text_response_for_session = resolve_text_response_for_session
    return mod


def approval_modules():
    """tools.approval + tools.approval_gateway_wait as Hermes has them: a per-session queue of entries, each
    with an event; the wait notifies, blocks until resolved, withdrawn or TIMEOUT (approvals.timeout)."""
    approval = types.ModuleType("tools.approval")
    wait = types.ModuleType("tools.approval_gateway_wait")
    approval._lock, approval._gateway_queues = threading.Lock(), {}
    wait.TIMEOUT = 5

    def resolve_gateway_approval(session_key, choice, resolve_all=False, reason=None, request_id=None):
        with approval._lock:
            queue = approval._gateway_queues.get(session_key) or []
            if not queue:
                return 0
            entry = queue.pop(0)
            entry.result, entry.reason = choice, reason
            entry.event.set()
            return 1

    def withdraw_gateway_approval(session_key, request_id, cause):
        with approval._lock:
            queue = approval._gateway_queues.get(session_key) or []
            entry = next((e for e in queue if e.data.get("request_id") == request_id), None)
            if entry is None:
                return False
            queue.remove(entry)
            entry.cancelled = cause
            entry.event.set()
            return True

    def _await_gateway_decision(session_key, notify_cb, approval_data, *, surface="gateway"):
        entry = SimpleNamespace(event=threading.Event(), data=dict(approval_data), result=None, reason=None,
                                cancelled=None)
        with approval._lock:
            approval._gateway_queues.setdefault(session_key, []).append(entry)
        try:
            notify_cb(dict(entry.data))
        except Exception:
            withdraw_gateway_approval(session_key, entry.data.get("request_id"), "notify_failed")
            return {"resolved": False, "choice": None, "notify_failed": True}
        fired = entry.event.wait(wait.TIMEOUT)
        with approval._lock:
            queue = approval._gateway_queues.get(session_key) or []
            if entry in queue:
                queue.remove(entry)
        extra = {"cancelled": entry.cancelled} if entry.cancelled else {}
        return {"resolved": fired and entry.result is not None, "choice": entry.result, "reason": entry.reason, **extra}

    approval.resolve_gateway_approval = resolve_gateway_approval
    approval.withdraw_gateway_approval = withdraw_gateway_approval
    wait._await_gateway_decision = _await_gateway_decision
    return approval, wait


class ApplicationHandlerStop(Exception):
    pass


class CallbackQueryHandler:
    def __init__(self, callback, pattern=None, block=True):
        self.callback, self.pattern, self.block = callback, pattern, block


class Application:
    def __init__(self):
        self.handlers = []  # (handler, group)
        self.bot = Bot()

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


class Adapter:
    """send_clarify / edit_message as the Telegram adapter has them: failure is a result, not a raise."""

    def __init__(self):
        self.sent, self.edits = [], []
        self.fail_sends = 0  # the next n sends fail
        self.fail_edits = 0
        self._n = 100
        self._approval_state = {}  # approval id -> session key, as the Telegram adapter keeps it

    async def send_exec_approval(self, chat_id, command, session_key, description=None, metadata=None,
                                 allow_permanent=True, allow_session=True, smart_denied=False):
        if self.fail_sends:
            self.fail_sends -= 1
            return SimpleNamespace(success=False, message_id=None, error="boom")
        self._n += 1
        self._approval_state[self._n] = session_key
        self.sent.append({"chat": chat_id, "command": command, "text": description, "session": session_key,
                          "mid": str(self._n), "permanent": allow_permanent, "session_button": allow_session})
        return SimpleNamespace(success=True, message_id=str(self._n), error=None)

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        if self.fail_sends:
            self.fail_sends -= 1
            return SimpleNamespace(success=False, message_id=None, error="boom")
        self._n += 1
        self.sent.append({"chat": chat_id, "text": question, "choices": choices, "cid": clarify_id,
                          "session": session_key, "mid": str(self._n)})
        return SimpleNamespace(success=True, message_id=str(self._n), error=None)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        if self.fail_edits:
            self.fail_edits -= 1
            return SimpleNamespace(success=False, message_id=None, error="gone")
        self.edits.append((chat_id, message_id, content))
        return SimpleNamespace(success=True, message_id=message_id, error=None)


class ForceReply:
    def __init__(self, selective=None, input_field_placeholder=None):
        self.selective, self.input_field_placeholder = selective, input_field_placeholder


class Bot:
    """PTB's bot as the Telegram app carries it: send_message returns the Message."""

    def __init__(self):
        self.sent, self._n = [], 900

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self._n += 1
        self.sent.append({"chat": chat_id, "text": text, "markup": reply_markup, "mid": self._n})
        return SimpleNamespace(message_id=self._n)


class Query:
    def __init__(self, user, chat, data="cl:mu0q0:0"):
        self.from_user = SimpleNamespace(id=user, mention_html=lambda: f'<a href="tg://user?id={user}">J</a>')
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat))
        self.data = data
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text)


def update(user, chat, data="cl:mu0q0:0"):
    return SimpleNamespace(callback_query=Query(user, chat, data))


def event(text, user, chat, reply=None, platform="telegram"):
    return SimpleNamespace(text=text, reply_to_message_id=reply,
                           source=SimpleNamespace(platform=SimpleNamespace(value=platform),
                                                  user_id=user, chat_id=chat))


def install(monkeypatch):
    """Put the fakes in sys.modules; returns the fake clarify module."""
    clarify = clarify_module()
    approval, wait = approval_modules()
    tools = types.ModuleType("tools")
    tools.clarify_gateway, tools.approval, tools.approval_gateway_wait = clarify, approval, wait
    ext = types.ModuleType("telegram.ext")
    ext.CallbackQueryHandler, ext.ApplicationHandlerStop = CallbackQueryHandler, ApplicationHandlerStop
    telegram = types.ModuleType("telegram")
    telegram.ext, telegram.ForceReply = ext, ForceReply
    for name, mod in (("tools", tools), ("tools.clarify_gateway", clarify), ("tools.approval", approval),
                      ("tools.approval_gateway_wait", wait), ("telegram", telegram),
                      ("telegram.ext", ext)):
        monkeypatch.setitem(sys.modules, name, mod)
    return clarify


class Gateway:
    """The Hermes gateway runner as far as muster asks: `_is_user_authorized` (allowlists), true for user 4242."""

    def _is_user_authorized(self, source):
        return str(source.user_id) == "4242"


def spawn_task(coro, name=None):
    import asyncio
    return asyncio.get_running_loop().create_task(coro)
