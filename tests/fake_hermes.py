"""The slice of Hermes the gateway touches: tools.clarify_gateway (real semantics), telegram.ext, an adapter.

clarify_gateway mirrors hermes-agent/tools/clarify_gateway.py: register / wait_for_response (blocks, pops the
entry) / resolve_gateway_clarify (False once resolved or unknown) / get_pending_for_session / mark_awaiting_text.
"""

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

    mod.register, mod.wait_for_response = register, wait_for_response
    mod.resolve_gateway_clarify, mod.get_pending_for_session = resolve_gateway_clarify, get_pending_for_session
    mod.mark_awaiting_text = mark_awaiting_text
    return mod


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
    tools = types.ModuleType("tools")
    tools.clarify_gateway = clarify
    ext = types.ModuleType("telegram.ext")
    ext.CallbackQueryHandler, ext.ApplicationHandlerStop = CallbackQueryHandler, ApplicationHandlerStop
    telegram = types.ModuleType("telegram")
    telegram.ext, telegram.ForceReply = ext, ForceReply
    for name, mod in (("tools", tools), ("tools.clarify_gateway", clarify), ("telegram", telegram),
                      ("telegram.ext", ext)):
        monkeypatch.setitem(sys.modules, name, mod)
    return clarify
