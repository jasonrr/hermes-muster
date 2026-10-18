"""telegram: muster's Telegram channel, moved out of muster.gateway.

A question, build or feedback choice goes out as a Hermes clarify prompt, a permission prompt as Hermes's own
approval card. Hermes owns the buttons, the typed-text parsing and the "expired" notice on a dead prompt. muster adds
only what Hermes lacks (upstream asks: jasonrr/hermes-muster#20):
- a narrower authorization: only an approver, in the notify chat, may answer a muster prompt (a guard in front of
  Hermes's handlers, PTB group -1); Hermes's allowlist (who may talk to its agent) is not enough to approve;
- a reply routed to the message it answers: Hermes routes typed text by chat session, oldest prompt first;
- a ForceReply after "Other": in a group with privacy mode Telegram delivers only replies to the bot;
- one forum topic per run (`notify_topics`, muster.conversation): created with Hermes's create_handoff_thread, a
  lost create reconciled from the bot's own forum_topic_created service message, a deleted topic (a prompt that
  landed outside it) recreated with the run's subscriptions and open prompts moved to it, closed when the run's work
  is resolved (cleanup) or by the human, reopened only by the human or for a request newer than the close, and
  deleted (never recreated) once its ledger card reads archived. Nothing probes a topic: Telegram has no read of a
  topic's state, and every call that could stand in for one changes it. Hermes's kanban notifier already sends pings
  and runs wakes in a subscription's thread, so the coordinator follows the topic without any code here.
Hermes keeps its prompts in memory, so each boot presents open requests again (`durable_prompts` False). Private
Hermes names are reached only through muster.hermes_private. Nothing here runs at import or register time: Hermes
calls `telegram_factory` when the Telegram adapter connects.
"""

import asyncio
import contextlib
import json
import re
import threading
from types import SimpleNamespace

from .. import config, conversation, core, decisions, hermes_private, progress
from . import shared
from .shared import ensure_config, log, session, soon

name = "telegram"
durable_prompts = False
CAP = 3500  # characters per message (Telegram allows 4096)
LABEL_MAX = 32  # characters on a button; Telegram's phone client cuts longer ones
TOPIC_RETRY = 10  # s before another try at checking the group, after a check that could not run
TOPIC_ATTEMPTS = 3
NOTICE_WAIT = 20  # s to wait for Telegram's topic-created notice after a create that returned no thread
MIGRATE_MAX = 60  # s, the longest wait between retries of a card subscription that did not move


class State:
    def __init__(self):
        self.adapter = self.bot = None
        self.shown = {}  # id -> number of clarify prompts presented in this process
        self.approvals = set()  # permission request ids whose Hermes approval wait runs in this process
        self.messages = {}  # message id -> (request id, question index), including superseded ones
        self.ledgers = {}  # request id -> ledger card: whose conversation (topic) the request belongs to
        self.repairing = set()  # ledgers whose cards and prompts are moving to a new topic


T = State()


def validate(settings):
    """A forum topic lives in a Telegram group, never in the DM fallback."""
    if settings["notify_topics"] is not True:
        return []
    entries = core.approver_entries(settings["notify_user_id"])
    return ([] if str(settings["notify_chat_id"]).strip() else ["notify_chat_id"]) + (
        [] if entries and all(entries) else ["notify_user_id"])


def home():
    return core.telegram_home()


def register(ctx):
    if not hasattr(ctx, "register_telegram_handler"):
        return False
    ctx.register_telegram_handler(telegram_factory)
    return True


def telegram_factory(app, adapter):
    """Called by the Telegram adapter's connect(), again with a new app when it rebuilds."""
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    config.load(shared.CTX)
    shared.configured = True
    core.prepare_env()
    app.add_handler(CallbackQueryHandler(guard, pattern=r"^cl:mu"), -1)
    app.add_handler(CallbackQueryHandler(approval_guard, pattern=r"^ea:"), -1)
    app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CREATED, topic_created), -1)
    app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CLOSED | filters.StatusUpdate.FORUM_TOPIC_REOPENED,
                                   topic_toggled), -1)
    T.adapter = adapter
    T.bot = getattr(app, "bot", None)  # Telegram's own bot, for the ForceReply after Other
    shared.start_scan()


def authorized(user, chat):
    """Only an approver, in the notify chat. A group with no notify_user_id authorizes nobody."""
    ensure_config()
    return str(user) in core.approvers() and str(chat) == core.notify_target()["chat_id"]


async def refuse_unless_authorized(query):
    """Group -1, ahead of Hermes's handler (group 0): refuse anyone but an approver in the notify chat."""
    from telegram.ext import ApplicationHandlerStop

    try:
        if authorized(query.from_user.id, query.message.chat.id):
            return
    except Exception as caught:  # noqa: BLE001 - fail closed
        log(f"guard error, refused: {caught}")
    log(f"refused a tap from user {getattr(query.from_user, 'id', '?')} in chat "
        f"{getattr(getattr(query.message, 'chat', None), 'id', '?')}")
    await query.answer("Not authorized")
    raise ApplicationHandlerStop


async def in_topic(rid, thread):
    """The message (or reply) is in the request's conversation: its run's topic, or the main chat (General) for a
    run without one. A request muster no longer has passes: Hermes answers it with its own "expired"."""
    ledger = T.ledgers.get(rid)
    try:
        if ledger is None:
            ledger = T.ledgers[rid] = (await asyncio.to_thread(decisions.load, rid))["ledger"]
        ref = await asyncio.to_thread(conversation.load, ledger)
    except FileNotFoundError:
        return True
    except (OSError, ValueError, KeyError) as caught:  # unreadable: fail closed
        log(f"request {rid}: conversation unreadable, refused: {caught}")
        return False
    if ref and ref.get("previous") and (ref.get("state") in ("pending", "creating") or not prompts_moved(ref)):
        return False  # its topic is being remade: nowhere answers until its prompts move there
    expected = (await asyncio.to_thread(conversation.target, ledger))["thread_id"]
    got = None if thread in (None, "", "1", 1) else str(thread)  # Telegram's General topic is thread 1
    return got == (str(expected) if expected else None)


async def refuse_unless_in_topic(query, rid):
    """A muster message found outside its run's topic (Hermes resent it to General when the topic was gone) is
    never answered there: present the request again, in its topic (repairing a deleted one first)."""
    from telegram.ext import ApplicationHandlerStop

    thread = getattr(query.message, "message_thread_id", None)
    if await in_topic(rid, thread):
        return
    log(f"request {rid}: tap outside its run's topic refused; presenting it again")
    release(rid)
    with contextlib.suppress(OSError, ValueError, KeyError):
        req = await asyncio.to_thread(decisions.load, rid)
        presented = req.get("presented") or {}
        await asyncio.to_thread(decisions.update, rid, presented={k: v for k, v in presented.items() if k != "boot"})
        expected = (await asyncio.to_thread(conversation.target, req["ledger"]))["thread_id"]
        newest = {str(ids[-1]) for ids in (presented.get("messages") or {}).values() if ids}
        if expected and thread in (None, "", "1", 1) and str(getattr(query.message, "message_id", "")) in newest:
            await gone(req["ledger"], str(expected))  # its newest prompt fell back to General: the topic is gone
    await query.answer("Moved: answer in the run's topic")
    raise ApplicationHandlerStop


async def guard(update, context):
    """A tap on a muster clarify prompt: authorize it, and after Other send the reply prompt."""
    query = update.callback_query
    await refuse_unless_authorized(query)
    found = re.match(r"cl:mu([0-9a-f]+)q", str(query.data))
    if found:
        await refuse_unless_in_topic(query, found.group(1))
    if str(query.data).endswith(":other"):
        await ask_for_text(query)


async def approval_guard(update, context):
    """A tap on a Hermes approval card: a muster card (session muster:...) takes only an approver's tap;
    Hermes's own cards pass untouched."""
    query = update.callback_query
    key = str(hermes_private.approval_session(T.adapter, query.data) or "")
    if key.startswith("muster:"):
        await refuse_unless_authorized(query)
        await refuse_unless_in_topic(query, key.split(":")[1])


async def ask_for_text(query):
    """After Other: a ForceReply naming the human, bound to the same question. In a group Telegram delivers a
    message to the bot only when it replies to the bot (privacy mode), and the Other button opens no reply box."""
    from telegram import ForceReply

    found = re.fullmatch(r"cl:mu([0-9a-f]+)q(\d+):other", str(query.data))
    if not found or T.bot is None:
        return
    rid, n = found.group(1), int(found.group(2))
    try:
        msg = await T.bot.send_message(
            chat_id=query.message.chat.id, parse_mode="HTML",
            message_thread_id=getattr(query.message, "message_thread_id", None),
            text=f"{query.from_user.mention_html()}: reply to this message with your answer.",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Your answer"))
        mid = str(msg.message_id)
        T.messages[mid] = (rid, n)
        await asyncio.to_thread(add_reply, rid, n, mid)
    except Exception as caught:  # noqa: BLE001 - the question still takes a reply to its own message
        log(f"request {rid}: reply prompt: {caught}")


def add_reply(rid, n, mid):
    """Keep a reply prompt's id with the request, so a reply to it still binds after a restart."""
    req = decisions.load(rid)
    presented = req.get("presented") or {}
    replies = presented.get("replies") or {}
    replies.setdefault(str(n), []).append(mid)
    decisions.update(rid, presented={**presented, "replies": replies})


# -- in-process state -----------------------------------------------------------------------------

def track(req):
    """Bind a request's saved message ids (prompts and reply prompts) in this process, so a reply binds after a
    restart. Another platform's ids are not Telegram messages."""
    T.ledgers[req["id"]] = req["ledger"]
    presented = req.get("presented") or {}
    if not mine(presented):
        return
    for n, ids in [*(presented.get("messages") or {}).items(), *(presented.get("replies") or {}).items()]:
        for mid in ids:
            T.messages[str(mid)] = (req["id"], int(n))


def release_ended(live):
    """A request that left `open`: let its waiter threads exit."""
    for rid in [*T.shown, *T.approvals]:
        if live.get(rid) != "open":
            release(rid)


def release(rid):
    """Cancel a request's Hermes prompts in this process; their waiter threads then exit."""
    from tools import approval, clarify_gateway

    for n in range(T.shown.pop(rid, 0)):
        clarify_gateway.clear_session(session(rid, n))
    if rid in T.approvals:
        T.approvals.discard(rid)
        approval.withdraw_gateway_approval(f"muster:{rid}", rid, "muster: the request ended")


async def edit(chat, mid, text):
    try:
        res = await T.adapter.edit_message(chat, mid, text)
    except Exception as caught:  # noqa: BLE001
        log(f"edit {mid}: {caught}")
        return False
    if not res.success:
        log(f"edit {mid}: {getattr(res, 'error', 'failed')}")
    return bool(res.success)


async def outcome(req):
    """Edit a finished request's newest messages (and reply prompts) to its outcome. True when every edit took."""
    presented = req.get("presented") or {}
    if not mine(presented):
        return True  # another platform's messages: not ours to edit
    chat = core.notify_target()["chat_id"]
    newest = [ids[-1] for ids in (presented.get("messages") or {}).values() if ids]
    newest += [mid for ids in (presented.get("replies") or {}).values() for mid in ids]
    return all([await edit(chat, mid, req.get("outcome") or req["status"]) for mid in newest])


# -- presenting -----------------------------------------------------------------------------------

def render(req, n):
    """Plain text: Hermes's clarify prompt HTML-escapes it, so muster's `**` markers would show (shared.plain)."""
    q = req["questions"][n]
    detail = ["Full text: the message above."] if (req.get("detail") or {}).get("platform") == name else []
    return shared.plain(shared.render(req, n, cap=CAP, label_max=LABEL_MAX, tail=[
        "Several: tap Other and type the numbers, e.g. 1,3" if q.get("multi")
        else "Your own words: tap Other, or reply to this message.", *detail]))


def buttons(req, n):
    return shared.buttons(req, n, label_max=LABEL_MAX, number_multi=True)


async def present(req, meta, questions):
    """Each question given as a Hermes clarify prompt, with a waiter thread for its answer."""
    rid, loop = req["id"], asyncio.get_running_loop()
    messages, failure = {}, None
    from tools import clarify_gateway

    chat = core.notify_target()["chat_id"]
    text = shared.detail(req)
    if text and (req.get("detail") or {}).get("platform") != name:  # once, above the card; Hermes formats and splits it
        res = await T.adapter.send(chat, text, metadata=meta)
        if not res.success:
            return {"messages": messages, "failure": res.error or "detail send failed"}
        req["detail"] = {"platform": name, "id": str(res.message_id)}
        await asyncio.to_thread(decisions.update, rid, detail=req["detail"])
    for n in questions:
        q = req["questions"][n]
        cid, choices = f"mu{rid}q{n}", req["choices"][n]
        clarify_gateway.register(cid, session(rid, n), q["text"], choices, bool(q.get("multi")))
        T.shown[rid] = max(T.shown.get(rid, 0), n + 1)
        threading.Thread(target=waiter, args=(rid, n, cid, loop), daemon=True).start()
        try:
            res = await hermes_private.send_labelled_clarify(T.adapter, chat, render(req, n), buttons(req, n),
                                                             cid, session(rid, n), metadata=meta)
        except Exception as caught:  # noqa: BLE001
            res = SimpleNamespace(success=False, error=str(caught))
        if not res.success:
            failure = res.error or "send failed"
            break
        messages.setdefault(str(n), []).append(str(res.message_id))
        T.messages[str(res.message_id)] = (rid, n)
    return {"messages": messages, "failure": failure}


async def present_approval(req, meta):
    """A permission prompt as Hermes's own approval card (formatted command; Allow once and Deny only, see
    bridge.approval_card), queued and waited on by Hermes's approval wait, so its buttons, edit and
    approvals.timeout apply unchanged. A reply to the card denies with that text."""
    rid, card = req["id"], req["card"]
    chat, loop = core.notify_target()["chat_id"], asyncio.get_running_loop()
    sent = loop.create_future()
    why = (f"Permission needed · {shared.title(req)}: {card['why']}. Allow once lets the builder run this one "
           "command, once; Muster can't confirm it ran. To deny with a message to the builder, reply to this message.")

    def notify(_data):  # runs in the wait thread, after Hermes queued the request
        future = asyncio.run_coroutine_threadsafe(T.adapter.send_exec_approval(
            chat, card["command"], f"muster:{rid}", why, metadata=meta, allow_permanent=False, allow_session=False),
            loop)
        try:
            res = future.result(60)
        except Exception as caught:  # noqa: BLE001
            res = SimpleNamespace(success=False, error=str(caught), message_id=None)
        loop.call_soon_threadsafe(lambda: sent.done() or sent.set_result(res))
        if not res.success:
            raise RuntimeError(res.error or "send failed")  # Hermes then returns notify_failed

    T.approvals.add(rid)
    threading.Thread(target=approval_waiter, args=(rid, card["command"], notify, loop), daemon=True).start()
    res = await sent
    if not res.success:
        return {"messages": {}, "failure": res.error or "send failed"}
    T.messages[str(res.message_id)] = (rid, 0)
    return {"messages": {"0": [str(res.message_id)]}, "failure": None}


def approval_waiter(rid, command, notify, loop):
    """Block in Hermes's approval wait; hand the human's choice (or Hermes's timeout, a deny) to the loop."""
    try:
        decision = hermes_private.await_approval(f"muster:{rid}", notify, command, rid)
    except Exception as caught:  # noqa: BLE001
        log(f"approval {rid}: {caught}")
        return
    if decision.get("notify_failed") or decision.get("cancelled"):
        return  # not sent (present retries), or withdrawn by release()
    choice = decision.get("choice")
    if choice == "once":
        answer = {"decision": "allow"}
    elif choice is not None:  # deny, or a tier the card does not offer: fail closed
        reason = (decision.get("reason") or "").strip()
        answer = {"decision": "deny", **({"message": reason} if reason else {})}
    else:  # Hermes's approvals.timeout passed with no answer: fail closed
        answer = {"decision": "deny", "timeout": True,
                  "message": "No answer from the human in time, so muster denied this. Ask again if you still need it."}
    T.approvals.discard(rid)
    soon(loop, shared.permission(rid, answer))


def waiter(rid, n, cid, loop):
    """One daemon thread per question: block until Hermes resolves the clarify, then hand the value to the loop."""
    from tools import clarify_gateway

    try:
        value = clarify_gateway.wait_for_response(cid, 0)
    except Exception as caught:  # noqa: BLE001
        log(f"waiter {cid}: {caught}")
        return
    if value is None or value.startswith("\x00"):  # cancelled by release()
        return
    soon(loop, shared.answer(rid, n, value))


# -- typed replies --------------------------------------------------------------------------------

async def reply(event):
    """A typed reply to a muster prompt, routed to it; None leaves the message to Hermes."""
    src = event.source
    if not T.messages or not authorized(src.user_id, src.chat_id):  # this hook runs before Hermes authorizes
        return None
    from tools import approval, clarify_gateway

    text, reply_to = event.text, event.reply_to_message_id
    thread = getattr(src, "thread_id", None)
    bound = T.messages.get(str(reply_to)) if reply_to is not None else None
    if bound:
        rid, n = bound
        if not await in_topic(rid, thread):
            return None  # a reply from outside the run's conversation is not an answer
        if rid in T.approvals:
            ok = approval.resolve_gateway_approval(f"muster:{rid}", "deny", reason=text) > 0
        else:
            entry = clarify_gateway.get_pending_for_session(session(rid, n), include_choice_prompts=True)
            ok = (entry is not None and clarify_gateway.mark_awaiting_text(entry.clarify_id)
                  and clarify_gateway.resolve_text_response_for_session(session(rid, n), text))
        if ok:
            await edit(src.chat_id, str(reply_to), f"Received ✓: {text[:200]}")
            return {"action": "skip"}
        return None  # not (or no longer) waiting in this process: leave it to Hermes
    # after "Other", the next message in that conversation is the answer whatever it replies to (as Hermes's own
    # clarify): only prompts of the topic it was typed in count, so two runs' prompts never compete
    waiting = [key for rid, count in list(T.shown.items()) if await in_topic(rid, thread)
               for key in (session(rid, n) for n in range(count))
               if clarify_gateway.get_pending_for_session(key) is not None]
    if len(waiting) == 1 and clarify_gateway.resolve_text_response_for_session(waiting[0], text):
        return {"action": "skip"}
    return None


# -- the run's status message (issue #46) ---------------------------------------------------------

async def show(ledger, line, saved):
    """Post the run's status message once (its topic, else the main chat), then edit it. Returns {mid, thread}
    once Telegram took it, None when it cannot be shown yet, False when Telegram refused it (tried later)."""
    if T.adapter is None:
        return None
    ref = await asyncio.to_thread(conversation.load, ledger)
    state = ref.get("state") if mine(ref) else None
    if state in ("pending", "creating", "retiring", "deleted"):
        return None
    thread = (await asyncio.to_thread(conversation.target, ledger))["thread_id"]
    chat = core.notify_target()["chat_id"]
    text = f"{await asyncio.to_thread(progress.name, ledger, ref)}\n{line}"
    if saved.get("mid") and saved.get("thread") == thread:
        return {"mid": saved["mid"], "thread": thread} if await edit(chat, saved["mid"], text) else False
    if state in ("closing", "closed"):
        return None  # no first post into a topic being closed
    res = await T.adapter.send(chat, text, metadata={"thread_id": thread} if thread else None)
    if not res.success:
        log(f"{ledger}: status message: {getattr(res, 'error', 'failed')}")
        return False
    return {"mid": str(res.message_id), "thread": thread}


# -- run topics (issue #24) -----------------------------------------------------------------------

def dest_of(ref):
    """Where the run is now: its thread, or "main" (the main chat)."""
    return ref.get("thread_id") or "main"


def prompts_moved(ref):
    return ref.get("moved") == dest_of(ref)


def outcome_of(error):
    """A Telegram topic call's error: "alive" (nothing to change), "deleted" (the thread is gone), else "unknown"."""
    text = str(error).lower()
    if "not_modified" in text or "not modified" in text:
        return "alive"
    if any(k in text for k in ("thread not found", "topic_id_invalid", "topic_deleted")):
        return "deleted"
    return "unknown"


def mine(record):
    """A conversation ref or a request's `presented` of this platform; one without a platform predates channels."""
    return bool(record) and record.get("platform", "telegram") == name


async def destination(req):
    """(ready, send metadata) for a request of this run: its topic. Not ready while the topic is being made, moved
    or closed. A closed topic is reopened only for a request made after it closed (new work); an older one (a replay,
    a restart, a saved approval) waits without reopening it: the pane still takes its answer, and the human may
    reopen the topic. Another platform's conversation: the main chat, and that conversation is left alone."""
    ledger = req["ledger"]
    ref = await asyncio.to_thread(conversation.load, ledger)
    if not mine(ref):
        return True, None
    state = (ref or {}).get("state")
    if state in ("retiring", "deleted"):
        await asyncio.to_thread(shared.stale_requests, ledger)
        return False, None
    if ref and (ledger in T.repairing or (ref.get("previous") and not prompts_moved(ref))):
        return False, None  # a new destination its prompts have not moved to yet
    if state in (None, "fallback"):
        return True, None  # the main chat
    if state not in ("open", "closed"):
        return False, None  # being made, or being closed
    if state == "closed":
        if req.get("created_at", 0) <= (ref.get("closed_at") or float("inf")):
            return False, None  # older than the close: never reopens it
        archive = await shared.archived(ledger)  # a fresh read: nothing reopens an archived run's topic
        if archive is not False:
            if archive:
                await shared.retire(ledger)
            return False, None
        if not await reopen(ref, req["id"]):
            return False, None
    return True, {"thread_id": ref["thread_id"]}


async def reopen(ref, rid):
    """Reopen a closed topic for request rid, new since the close. False when it did not reopen."""
    ledger, thread = ref["ledger"], ref["thread_id"]
    try:
        await T.bot.reopen_forum_topic(ref["chat_id"], int(thread))
    except Exception as caught:  # noqa: BLE001
        found = outcome_of(caught)
        if found == "deleted":
            await gone(ledger, thread)
            return False
        if found == "unknown":
            log(f"{ledger}: reopening topic {thread}: {caught}")
            return False
    _, ok = await asyncio.to_thread(conversation.swap, ledger, ("closed",), state="open", closed_at=None,
                                    closed_by=None)
    if ok:
        log(f"{ledger}: topic {thread} reopened for request {rid}, made after it closed")
    return ok


async def gone(ledger, thread):
    """The run's topic `thread` was deleted: the next scans make a new one and move its cards and prompts there."""
    ref = await asyncio.to_thread(conversation.load, ledger)
    if not ref or str(ref.get("thread_id")) != str(thread):
        return  # already left
    _, ok = await asyncio.to_thread(conversation.swap, ledger, ("open", "closed"), state="creating", thread_id=None,
                                    previous=[*ref.get("previous", []), thread], attempts=0, since=0, asked=False,
                                    moved=None, repaired=None, migrate_at=0, migrate_delay=0, closed_at=None,
                                    closed_by=None)
    if ok:
        log(f"{ledger}: topic {thread} is gone; making a new one")


async def steps():
    """Once per scan, before the requests: each run's topic one step further."""
    if T.bot is None or T.adapter is None:
        return
    for ref in await asyncio.to_thread(conversation.active):
        if not mine(ref):
            continue  # another platform's conversation and subscriptions stay as they are
        try:
            await topic_step(ref)
        except Exception as caught:  # noqa: BLE001 - one run's topic must not stop the others
            log(f"topic {ref.get('ledger')}: {caught}")


async def topic_step(ref):
    ledger, state, chat = ref["ledger"], ref.get("state"), ref.get("chat_id")
    if state == "deleted":
        await shared.unsubscribe(ref)  # until every card's subscription is gone
        return
    every = shared.CLOSED_EVERY if state == "closed" else shared.CHECK_EVERY
    if (state != "retiring" and shared.clock() - shared.checked.get(ledger, float("-inf")) >= every
            and await shared.archived(ledger)):
        await shared.retire(ledger)
        ref = await asyncio.to_thread(conversation.load, ledger)
        state = ref.get("state")
    if state == "retiring":
        await delete(ref)
    elif state in ("pending", "creating"):
        await create(ref)
    elif state == "fallback":
        if not ref.get("noticed"):
            await T.adapter.send(chat, f"Topic not created for {ref.get('name')}: {ref.get('why')}. "
                                       f"This run stays in the main chat.")
            await asyncio.to_thread(conversation.update, ledger, noticed=True)
        if ref.get("previous") and ref.get("repaired") != "main":
            await repair(ref)  # a recreate that failed: the run moves to the main chat
    elif state == "closing" and shared.now() >= ref.get("retry_at", 0):
        if not ref.get("said"):  # once
            try:
                await T.adapter.send(chat, "Finished. This topic is now closed.",
                                     metadata={"thread_id": ref["thread_id"]})
            except Exception as caught:  # noqa: BLE001
                log(f"{ledger}: finish note: {caught}")
            await asyncio.to_thread(conversation.update, ledger, said=True)
        try:
            await T.bot.close_forum_topic(chat, int(ref["thread_id"]))
        except Exception as caught:  # noqa: BLE001 - closed already (not modified) or deleted: nothing to close
            if outcome_of(caught) == "unknown":
                await shared.backoff(ref, f"closing topic {ref['thread_id']}: {caught}")
                return
        # closed only once Telegram confirmed it: local state alone is no proof
        await asyncio.to_thread(conversation.swap, ledger, ("closing",), state="closed", why="", retry_at=0,
                                retry_delay=0, closed_at=shared.now(), closed_by="muster")
    elif state == "closed" and not ref.get("closed_at"):  # closed before closed_at was kept: nothing saved reopens it
        await asyncio.to_thread(conversation.swap, ledger, ("closed",), closed_at=shared.now())
    elif state == "open" and ref.get("previous") and ref.get("repaired") != ref["thread_id"]:
        await repair(ref)  # until every card's subscription reads back here


async def delete(ref):
    """The run is archived: its open prompts end and its topic is deleted, so nothing can bring it back. A delete
    Telegram did not confirm closes the topic meanwhile and is tried again."""
    ledger, chat, thread = ref["ledger"], ref.get("chat_id"), ref.get("thread_id")
    if shared.now() < ref.get("retry_at", 0):
        return
    await asyncio.to_thread(shared.stale_requests, ledger)
    if thread:
        try:
            await T.bot.delete_forum_topic(chat, int(thread))
        except Exception as caught:  # noqa: BLE001
            if outcome_of(caught) != "deleted":  # "thread not found": the human deleted it already
                with contextlib.suppress(Exception):
                    await T.bot.close_forum_topic(chat, int(thread))
                await shared.backoff(ref, f"deleting topic {thread}: {caught}")
                return
    ref, ok = await asyncio.to_thread(conversation.swap, ledger, ("retiring",), state="deleted",
                                      deleted_at=shared.now(), why="", retry_at=0, retry_delay=0, unsubscribed=False)
    if ok:
        log(f"{ledger}: topic {thread} deleted")
        await shared.unsubscribe(ref)  # pings for an archived run must not land in General


async def create(ref):
    """At most one createForumTopic call per destination. Its result can be lost: Hermes's create_handoff_thread
    turns every error into None, including a timeout after Telegram made the topic. So a None is never retried
    (that could make a second topic). The topic is adopted from Telegram's topic-created notice when one arrives
    (topic_created), and after NOTICE_WAIT without one the run uses the main chat; a notice that turns up later
    closes that topic as unused. Only a prerequisite check that could not run is tried again (3 times)."""
    ledger, attempts = ref["ledger"], ref.get("attempts", 0)
    if ref.get("asked"):
        if shared.now() - ref.get("since", 0) >= NOTICE_WAIT:
            await asyncio.to_thread(
                conversation.swap, ledger, ("creating",), state="fallback", noticed=False,
                why="Telegram neither confirmed the topic nor sent its notice; not retried, so no second topic is made")
        return
    if ref["state"] == "creating" and shared.now() - ref.get("since", 0) < TOPIC_RETRY:
        return
    if attempts >= TOPIC_ATTEMPTS:
        await asyncio.to_thread(conversation.swap, ledger, ("pending", "creating"), state="fallback", noticed=False,
                                why=f"could not check the group after {TOPIC_ATTEMPTS} attempts")
        return
    gone = await shared.archived(ledger)  # a fresh read before every attempt: no topic is made for an archived run
    if gone is not False:
        if gone:
            await shared.retire(ledger)
        return
    ref, ok = await asyncio.to_thread(conversation.swap, ledger, ("pending", "creating"), state="creating",
                                      attempts=attempts + 1, since=shared.now())
    if not ok:
        return
    blocked = await prerequisites(ref["chat_id"])
    if blocked:
        definite, why = blocked
        if definite:
            await asyncio.to_thread(conversation.swap, ledger, ("creating",), state="fallback", why=why, noticed=False)
        log(f"{ledger}: topic attempt {attempts + 1}: {why}")
        return
    # Saved before the call: from here on the result may be unknown (a lost reply, or a restart mid-call).
    _, ok = await asyncio.to_thread(conversation.swap, ledger, ("creating",), asked=True, since=shared.now())
    if not ok:
        return
    thread = await T.adapter.create_handoff_thread(ref["chat_id"], ref["name"])
    if thread:
        await settle(ledger, str(thread))
    else:
        log(f"{ledger}: the topic create returned no thread; waiting {NOTICE_WAIT} s for Telegram's notice, no retry")


async def prerequisites(chat):
    """None when the bot may make topics in the chat; else (definite, why). An error checking is not definite."""
    try:
        info = await T.bot.get_chat(chat)
        if not getattr(info, "is_forum", False):
            return True, "Topics are not enabled in the group (group settings > Topics)"
        member = await T.bot.get_chat_member(chat, T.bot.id)
    except Exception as caught:  # noqa: BLE001
        return False, f"could not check the group: {caught}"
    status = str(getattr(member.status, "value", member.status))
    if status == "creator" or (status == "administrator" and getattr(member, "can_manage_topics", False)):
        return None
    return True, "the bot cannot manage topics (make it an admin with the Manage Topics right)"


async def settle(ledger, thread):
    """A topic Telegram made for this run: adopt it, unless the run already has its topic or gave up waiting."""
    old = await asyncio.to_thread(conversation.load, ledger)
    if old and thread in old.get("previous", []):
        return  # a topic the run left: never adopted again
    ref, ok = await asyncio.to_thread(conversation.swap, ledger, ("pending", "creating"), state="open",
                                      thread_id=thread)
    if ok:
        log(f"{ledger}: topic {thread} open")
    elif ref and ref.get("thread_id") != thread:
        await close_duplicate(ref, thread)


async def close_duplicate(ref, thread):
    if ref.get("state") in ("retiring", "deleted"):  # a late topic of an archived run: deleted, not kept
        log(f"{ref['ledger']}: topic {thread} of an archived run deleted")
        try:
            await T.bot.delete_forum_topic(ref["chat_id"], int(thread))
        except Exception as caught:  # noqa: BLE001
            log(f"{ref['ledger']}: deleting topic {thread}: {caught}")
        return
    where = f'the topic "{ref.get("name")}"' if ref.get("state") in conversation.THREADED else "the main chat"
    log(f"{ref['ledger']}: duplicate topic {thread} closed")
    try:
        await T.adapter.send(ref["chat_id"], f"Duplicate topic, not used; this run is in {where}.",
                             metadata={"thread_id": thread})
        await T.bot.close_forum_topic(ref["chat_id"], int(thread))
    except Exception as caught:  # noqa: BLE001
        log(f"{ref['ledger']}: closing duplicate topic {thread}: {caught}")


async def topic_created(update, context):
    """Telegram's forum_topic_created notice of a topic the bot itself made in the notify chat. Its name is unique
    among live runs (conversation.unique), so it names exactly one run: the thread of that run's create whose reply was
    lost is adopted; any other topic for the run is closed as unused. A name that matches no run, or several, is
    left alone."""
    try:
        msg = update.effective_message
        made = getattr(msg, "forum_topic_created", None)
        if not made or T.bot is None or getattr(msg.from_user, "id", None) != T.bot.id:
            return
        ensure_config()
        if str(msg.chat.id) != core.notify_target()["chat_id"]:
            return
        thread = str(msg.message_thread_id)
        log(f"topic notice: thread {thread} {made.name!r}")  # the live evidence that Telegram sends these to the bot
        named = [r for r in await asyncio.to_thread(conversation.every) if mine(r) and r.get("name") == made.name]
        if len(named) != 1:
            if named:
                log(f"topic notice: {len(named)} runs are named {made.name!r}; ambiguous, ignored")
            return
        ref = named[0]
        if thread in ref.get("previous", []):
            return  # a late notice of a topic the run already left (deleted): never adopted, never closed
        if ref.get("state") == "creating" and ref.get("asked"):
            await settle(ref["ledger"], thread)
        elif ref.get("thread_id") != thread and thread not in ref.get("previous", []):
            await close_duplicate(ref, thread)
    except Exception as caught:  # noqa: BLE001 - never break Hermes's own update handling
        log(f"topic notice: {caught}")


async def topic_toggled(update, context):
    """Telegram's forum_topic_closed or forum_topic_reopened notice of a run's topic, from a human (the bot's own
    close and reopen are recorded where it makes them). A human close is kept: no restart, scan or saved prompt
    reopens the topic, and the run's work goes on. A human reopen is the explicit resume: its prompts show again."""
    try:
        msg = update.effective_message
        if T.bot is None or getattr(msg.from_user, "id", None) == T.bot.id:
            return
        ensure_config()
        if str(msg.chat.id) != core.notify_target()["chat_id"]:
            return
        thread = str(msg.message_thread_id)
        mine = [r for r in await asyncio.to_thread(conversation.every)
                if str(r.get("thread_id")) == thread and r.get("state") in conversation.THREADED]
        if len(mine) != 1:
            return
        ledger = mine[0]["ledger"]
        if getattr(msg, "forum_topic_closed", None) is not None:
            _, ok = await asyncio.to_thread(conversation.swap, ledger, ("open", "closing"), state="closed",
                                            closed_at=shared.now(), closed_by="human", resumed=False, why="",
                                            retry_at=0, retry_delay=0)
            if ok:
                log(f"{ledger}: topic {thread} closed by the human; kept closed")
        else:
            # also from closing: a close whose reply was lost may have landed; its retry must not undo this reopen
            _, ok = await asyncio.to_thread(conversation.swap, ledger, ("closed", "closing"), state="open",
                                            closed_at=None, closed_by=None, resumed=True, why="", retry_at=0,
                                            retry_delay=0)  # kept open, even once resolved
            if ok:
                log(f"{ledger}: topic {thread} reopened by the human; its prompts show again")
                for req in await asyncio.to_thread(decisions.for_ledger, ledger):
                    if req["status"] == "open":  # present again in this boot, now that the topic takes them
                        release(req["id"])
                        presented = req.get("presented") or {}
                        await asyncio.to_thread(decisions.update, req["id"],
                                                presented={k: v for k, v in presented.items() if k != "boot"})
    except Exception as caught:  # noqa: BLE001 - never break Hermes's own update handling
        log(f"topic close/reopen notice: {caught}")


async def repair(ref):
    """The run's topic was recreated (or given up on). First, once: its open requests are presented again there and
    the move is said. Then each card's subscription is moved, and a card whose move does not read back is kept in
    `migrating` and tried again (backing off to MIGRATE_MAX). `repaired` is written only when none is left."""
    ledger, dest = ref["ledger"], dest_of(ref)
    if ledger in T.repairing or shared.now() < ref.get("migrate_at", 0):
        return
    T.repairing.add(ledger)
    try:
        if ref.get("moved") != dest:
            for req in await asyncio.to_thread(decisions.for_ledger, ledger):
                if req["status"] == "open":
                    release(req["id"])
                    presented = req.get("presented") or {}
                    await asyncio.to_thread(decisions.update, req["id"],
                                            presented={k: v for k, v in presented.items() if k != "boot"})
            if ref.get("thread_id"):
                await T.adapter.send(ref["chat_id"], "The previous topic was deleted; this run continues here.",
                                     metadata={"thread_id": ref["thread_id"]})
            ref = await asyncio.to_thread(conversation.update, ledger, moved=dest, migrating=list(ref.get("cards", [])),
                                          migrate_delay=0, migrate_at=0)
        left = await asyncio.to_thread(move_cards, ref)
        if left:
            delay = min(max(ref.get("migrate_delay", 0) * 2, shared.SCAN_EVERY), MIGRATE_MAX)
            await asyncio.to_thread(conversation.update, ledger, migrating=left, migrate_delay=delay,
                                    migrate_at=shared.now() + delay)
            log(f"{ledger}: {len(left)} card subscription(s) not moved to {dest} yet, retry in {delay} s: {left}")
        else:
            await asyncio.to_thread(conversation.update, ledger, migrating=[], repaired=dest)
            log(f"{ledger}: moved to {dest}")
    finally:
        T.repairing.discard(ledger)


def move_cards(ref):
    """Move each card in `migrating` to where the run is now. Returns the cards not verified there: a card is done
    only when notify-list shows it subscribed at the destination (core.subscribe reads that back) and nowhere it
    was before, so Hermes's notifier pings it, and wakes the coordinator, in the new place only."""
    ledger, platform, chat = ref["ledger"], config.settings["notify_platform"], ref["chat_id"]
    olds = [t for t in ref.get("previous", []) if t] + ([None] if ref.get("thread_id") else [])
    left = []
    for card in ref.get("migrating", []):
        try:
            core.subscribe(card, ledger)
            for old in olds:
                with contextlib.suppress(core.CommandError):  # "no such subscription": checked below
                    core.kanban("notify-unsubscribe", card, "--platform", platform, "--chat-id", chat,
                                *(["--thread-id", old] if old else []))
            stale, here = {str(old or "") for old in olds}, str(ref.get("thread_id") or "")
            rows = [str(r.get("thread_id") or "") for r in json.loads(core.kanban("notify-list", card, "--json"))
                    if str(r.get("chat_id")) == str(chat)]
            if here not in rows:
                raise core.CommandError(f"not subscribed at {here or 'the main chat'}")
            if stale & set(rows):
                raise core.CommandError("still subscribed where the run was")
        except Exception as caught:  # noqa: BLE001 - one card must not hold up the rest
            if "no such task" in str(caught):  # deleted from the board: nothing left to move
                log(f"{ledger}: {card} is gone from the board; not moved")
                continue
            log(f"{ledger}: moving {card}'s subscription: {caught}")
            left.append(card)
    return left
