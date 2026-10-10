"""The slice of Hermes the gateway touches: tools.clarify_gateway (real semantics), telegram.ext, an adapter.

clarify_gateway mirrors hermes-agent/tools/clarify_gateway.py: register / wait_for_response (blocks, pops the
entry) / resolve_gateway_clarify (False once resolved or unknown) / get_pending_for_session / mark_awaiting_text.
"""

import json
import sys
import threading
import types
from types import SimpleNamespace

from muster import core


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


class MessageHandler:
    def __init__(self, filters, callback, block=True):
        self.filters, self.callback, self.pattern = filters, callback, None


filters = types.SimpleNamespace(StatusUpdate=types.SimpleNamespace(FORUM_TOPIC_CREATED="forum_topic_created"))


class Application:
    def __init__(self):
        self.handlers = []  # (handler, group)
        self.bot = Bot()

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


class Adapter:
    """The Telegram adapter's prompt sends and edit_message as muster uses them: failure is a result, not a raise."""

    def __init__(self):
        self.sent, self.edits = [], []
        self.fail_sends = 0  # the next n sends fail
        self.fail_edits = 0
        self._n = 100
        self._approval_state = {}  # approval id -> session key, as the Telegram adapter keeps it
        self._clarify_state = {}  # clarify id -> session key, likewise
        self.bot = None  # the Bot whose forum the topics live in (Application.bot)
        self.creates = []  # scripted create_handoff_thread outcomes: "ok", "lost" (made, no reply) or "fail"
        self.notes = []  # send(): (chat, text, thread)

    @staticmethod
    def _metadata_thread_id(metadata):
        thread = (metadata or {}).get("thread_id")
        return str(thread) if thread else None

    def landing(self, metadata):
        """The thread a send lands in: Hermes resends to General (None) when the thread is gone."""
        thread = (metadata or {}).get("thread_id")
        return None if thread and self.bot and int(thread) in self.bot.deleted else thread

    async def create_handoff_thread(self, parent_chat_id, name):
        """As the Telegram adapter: Telegram's createForumTopic, every error swallowed into None."""
        outcome = self.creates.pop(0) if self.creates else "ok"
        if outcome == "fail":
            return None
        thread = self.bot.make_topic(parent_chat_id, name)
        return None if outcome == "lost" else str(thread)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.notes.append((chat_id, content, self.landing(metadata)))
        self._n += 1
        return SimpleNamespace(success=True, message_id=str(self._n), error=None)

    async def send_exec_approval(self, chat_id, command, session_key, description=None, metadata=None,
                                 allow_permanent=True, allow_session=True, smart_denied=False):
        if self.fail_sends:
            self.fail_sends -= 1
            return SimpleNamespace(success=False, message_id=None, error="boom")
        self._n += 1
        self._approval_state[self._n] = session_key
        self.sent.append({"chat": chat_id, "command": command, "text": description, "session": session_key,
                          "mid": str(self._n), "permanent": allow_permanent, "session_button": allow_session,
                          "thread": self.landing(metadata)})
        return SimpleNamespace(success=True, message_id=str(self._n), error=None)

    async def _send_prompt(self, what, chat_id, metadata, build, *, parse_mode=None, thread_id=None, reply_to_mode=None):
        """Hermes's control-prompt shell: build() -> (text, keyboard, on_sent), routed send, a failure as a result."""
        if self.fail_sends:
            self.fail_sends -= 1
            return SimpleNamespace(success=False, message_id=None, error="boom")
        text, keyboard, on_sent = build()
        self._n += 1
        on_sent(SimpleNamespace(message_id=self._n))
        buttons = [(b.text, b.callback_data) for row in keyboard.inline_keyboard for b in row]
        cid = buttons[0][1].split(":")[1]
        self.sent.append({"chat": chat_id, "text": text, "buttons": buttons, "cid": cid, "parse_mode": parse_mode,
                          "session": self._clarify_state[cid], "mid": str(self._n),
                          "thread": self.landing({"thread_id": thread_id} if thread_id else metadata)})
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
        self.id = 555  # the bot's own user id
        self.forum, self.member = True, SimpleNamespace(status="administrator", can_manage_topics=True)
        self.topics, self.closed, self.deleted = {}, set(), set()  # thread -> name
        self.calls = []  # (method, chat, thread)
        self._thread = 40

    def make_topic(self, chat_id, name):
        self._thread += 1
        self.topics[self._thread] = name
        return self._thread

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, message_thread_id=None):
        self._n += 1
        self.sent.append({"chat": chat_id, "text": text, "markup": reply_markup, "mid": self._n,
                          "thread": message_thread_id})
        return SimpleNamespace(message_id=self._n)

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, is_forum=self.forum)

    async def get_chat_member(self, chat_id, user_id):
        assert user_id == self.id
        return self.member

    def _topic(self, method, chat_id, thread):
        self.calls.append((method, chat_id, thread))
        if thread in self.deleted or thread not in self.topics:
            raise Exception("Message thread not found")  # PTB's BadRequest text

    async def reopen_forum_topic(self, chat_id, message_thread_id):
        self._topic("reopen", chat_id, message_thread_id)
        if message_thread_id not in self.closed:
            raise Exception("Topic_not_modified")
        self.closed.discard(message_thread_id)
        return True

    async def close_forum_topic(self, chat_id, message_thread_id):
        self._topic("close", chat_id, message_thread_id)
        self.closed.add(message_thread_id)
        return True

    async def delete_forum_topic(self, chat_id, message_thread_id):
        self._topic("delete", chat_id, message_thread_id)
        del self.topics[message_thread_id]
        return True


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None):
        self.text, self.callback_data = text, callback_data


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


OTHER = "✏️ Other (type answer)"  # Hermes's English platform.telegram.prompt.other


def t(key, **kw):
    return {"platform.telegram.prompt.other": OTHER}.get(key, key)


class Query:
    def __init__(self, user, chat, data="cl:mu0q0:0", thread=None):
        self.from_user = SimpleNamespace(id=user, mention_html=lambda: f'<a href="tg://user?id={user}">J</a>')
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat), message_thread_id=thread)
        self.data = data
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text)


def update(user, chat, data="cl:mu0q0:0", thread=None):
    return SimpleNamespace(callback_query=Query(user, chat, data, thread))


def topic_notice(chat, thread, name, sender=555):
    """Telegram's forum_topic_created service message, as a PTB Update."""
    return SimpleNamespace(effective_message=SimpleNamespace(
        chat=SimpleNamespace(id=chat), from_user=SimpleNamespace(id=sender), message_thread_id=thread,
        forum_topic_created=SimpleNamespace(name=name)))


def event(text, user, chat, reply=None, platform="telegram", thread=None):
    return SimpleNamespace(text=text, reply_to_message_id=reply,
                           source=SimpleNamespace(platform=SimpleNamespace(value=platform),
                                                  user_id=user, chat_id=chat, thread_id=thread))


def install(monkeypatch):
    """Put the fakes in sys.modules; returns the fake clarify module."""
    clarify = clarify_module()
    approval, wait = approval_modules()
    tools = types.ModuleType("tools")
    tools.clarify_gateway, tools.approval, tools.approval_gateway_wait = clarify, approval, wait
    ext = types.ModuleType("telegram.ext")
    ext.CallbackQueryHandler, ext.ApplicationHandlerStop = CallbackQueryHandler, ApplicationHandlerStop
    ext.MessageHandler, ext.filters = MessageHandler, filters
    telegram = types.ModuleType("telegram")
    telegram.ext, telegram.ForceReply = ext, ForceReply
    telegram.InlineKeyboardButton, telegram.InlineKeyboardMarkup = InlineKeyboardButton, InlineKeyboardMarkup
    constants = types.ModuleType("telegram.constants")
    constants.ParseMode = SimpleNamespace(HTML="HTML", MARKDOWN_V2="MarkdownV2")
    agent, i18n = types.ModuleType("agent"), types.ModuleType("agent.i18n")
    agent.i18n, i18n.t = i18n, t
    for name, mod in (("telegram.constants", constants), ("agent", agent), ("agent.i18n", i18n), ("tools", tools), ("tools.clarify_gateway", clarify), ("tools.approval", approval),
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


class Subs:
    """`hermes kanban notify-subscribe / notify-unsubscribe / notify-list` as Hermes keeps them: one row per
    (card, chat, thread); `deliver` is where the kanban notifier would send a card's ping and run its wake."""

    def __init__(self):
        self.rows, self.calls = {}, []
        self.fail = {}  # card -> how many of its next notify-subscribe calls fail
        self.missing = set()  # cards deleted from the board
        self.status = {}  # card -> its board status (default "ready")

    def __call__(self, *argv):
        self.calls.append(argv)
        verb, card = argv[0], argv[1]
        if verb == "notify-subscribe" and card in self.missing:
            raise core.CommandError(f"hermes kanban: exit 1\nno such task: {card}")  # as _cmd_notify_subscribe
        if verb == "notify-subscribe" and self.fail.get(card):
            self.fail[card] -= 1
            raise core.CommandError("hermes kanban: exit 1\ndatabase is locked")
        if verb == "notify-subscribe":
            flag = dict(zip(argv[2::2], argv[3::2]))
            self.rows.setdefault(card, []).append({
                "chat_id": flag["--chat-id"], "thread_id": flag.get("--thread-id", ""), "user_id": flag["--user-id"],
                "chat_type": flag["--chat-type"], "notifier_profile": flag["--notifier-profile"],
                "delivery_mode": flag["--delivery-mode"]})
            return ""
        if verb == "notify-unsubscribe":
            flag = dict(zip(argv[2::2], argv[3::2]))
            rows = self.rows.get(card, [])
            keep = [r for r in rows if (r["chat_id"], r["thread_id"]) != (flag["--chat-id"], flag.get("--thread-id", ""))]
            if len(keep) == len(rows):
                raise core.CommandError("(no such subscription)")
            self.rows[card] = keep
            return ""
        if verb == "notify-list":
            return json.dumps(self.rows.get(card, []))
        if verb == "comment":
            return ""
        if verb == "show":
            if card in self.missing:
                raise core.CommandError(f"hermes kanban: exit 1\nno such task: {card}")  # as `hermes kanban show`
            return json.dumps({"task": {"status": self.status.get(card, "ready")}})
        raise AssertionError(argv)

    def deliver(self, card):
        """The threads Hermes's notifier pings (and runs the coordinator's wake in) for this card's next event."""
        return sorted(r["thread_id"] for r in self.rows.get(card, []))
