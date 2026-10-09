"""gateway: the Hermes-gateway side of a channel decision.

Runs inside the Hermes gateway process. A scan task presents each `open` request (muster.decisions): a
question, build or feedback choice as Hermes clarify prompts, a permission prompt as Hermes's own approval
card. Hermes owns the buttons, the typed-text parsing and the "expired" notice on a dead prompt. muster adds
only what Hermes lacks (upstream asks: jasonrr/hermes-muster#20):
- a narrower authorization: only the notify user, in the notify chat, may answer a muster prompt (a guard in
  front of Hermes's handlers); Hermes's allowlist (who may talk to its agent) is not enough to approve;
- the outcome on the message: it is edited to what happened (Sent to Claude, Deny sent to Claude, Answered in
  the pane...), so the human knows whether muster passed their answer on (not that Claude applied it);
- prompts that survive a gateway restart: Hermes keeps them in memory, so each boot presents open requests again;
- a reply routed to the message it answers: Hermes routes typed text by chat session, oldest prompt first;
- a ForceReply after "Other": in a group with privacy mode Telegram delivers only replies to the bot;
- send retry with backoff: Hermes sends once.
Private Hermes names are reached only through muster.hermes_private. Nothing here runs at import or register
time: Hermes calls `telegram_factory` when the Telegram adapter connects.
"""

import asyncio
import json
import re
import secrets
import threading
import time
from types import SimpleNamespace

from . import config, core, decisions, hermes_private

CTX = None  # the Hermes plugin context; set by register()
SCAN_EVERY = 2  # s between scans
ALIVE_MAX = 30  # s without a hook heartbeat before a question or permission request is stale
CAP = 3500  # characters per message (Telegram allows 4096)
BACKOFF_MAX = 60
BOOT = secrets.token_hex(4)


class State:
    def __init__(self):
        self.adapter = self.loop = self.task = self.bot = None
        self.configured = self.recovered = False
        self.presenting = set()  # request ids being presented right now
        self.retry = {}  # id -> (monotonic time not before, next delay)
        self.shown = {}  # id -> number of clarify prompts presented in this process
        self.approvals = set()  # permission request ids whose Hermes approval wait runs in this process
        self.partial = {}  # id -> {question index: answer}: lost on restart, the request is re-presented
        self.messages = {}  # message id -> (request id, question index), including superseded ones
        self.watch = set()  # ids presented or seen open: edited to their outcome when they end
        self.edit_failed = set()
        self.swept = False


S = State()


def log(line):
    core.log("gateway", line)


def ensure_config():
    if not S.configured:
        config.load(CTX)
        S.configured = True


def session(rid, n):
    """One clarify session per question, so Hermes's typed-text resolution targets exactly that question."""
    return f"muster:{rid}:{n}"


def soon(loop, coro):
    """From a waiter thread: run coro on the gateway loop as a Hermes-supervised task."""
    try:
        loop.call_soon_threadsafe(CTX.spawn_task, coro)
    except RuntimeError:  # the loop is closed: the gateway is shutting down
        coro.close()
        log("loop closed, answer dropped")


def telegram_factory(app, adapter):
    """Called by the Telegram adapter's connect(), again with a new app when it rebuilds."""
    from telegram.ext import CallbackQueryHandler

    config.load(CTX)
    S.configured = True
    core.prepare_env()
    app.add_handler(CallbackQueryHandler(guard, pattern=r"^cl:mu"), -1)
    app.add_handler(CallbackQueryHandler(approval_guard, pattern=r"^ea:"), -1)
    S.adapter, S.loop = adapter, asyncio.get_running_loop()
    S.bot = getattr(app, "bot", None)  # Telegram's own bot, for the ForceReply after Other
    if S.task is None or S.task.done() or S.task.get_loop() is not S.loop:
        S.task = CTX.spawn_task(scan_loop(), name="muster:scan")  # Hermes cancels it on plugin unload


def authorized(user, chat):
    """Only the notify user, in the notify chat. A group with no notify_user_id authorizes nobody."""
    ensure_config()
    s, target = config.settings, core.notify_target()
    return (not (s["notify_chat_id"] and not s["notify_user_id"])
            and str(user) == target["user_id"] and str(chat) == target["chat_id"])


async def refuse_unless_authorized(query):
    """Group -1, ahead of Hermes's handler (group 0): refuse anyone but the notify user in the notify chat."""
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


async def guard(update, context):
    """A tap on a muster clarify prompt: authorize it, and after Other send the reply prompt."""
    query = update.callback_query
    await refuse_unless_authorized(query)
    if str(query.data).endswith(":other"):
        await ask_for_text(query)


async def approval_guard(update, context):
    """A tap on a Hermes approval card: a muster card (session muster:...) takes only the notify user's tap;
    Hermes's own cards pass untouched."""
    query = update.callback_query
    if str(hermes_private.approval_session(S.adapter, query.data) or "").startswith("muster:"):
        await refuse_unless_authorized(query)


async def ask_for_text(query):
    """After Other: a ForceReply naming the human, bound to the same question. In a group Telegram delivers a
    message to the bot only when it replies to the bot (privacy mode), and the Other button opens no reply box."""
    from telegram import ForceReply

    found = re.fullmatch(r"cl:mu([0-9a-f]+)q(\d+):other", str(query.data))
    if not found or S.bot is None:
        return
    rid, n = found.group(1), int(found.group(2))
    try:
        msg = await S.bot.send_message(
            chat_id=query.message.chat.id, parse_mode="HTML",
            text=f"{query.from_user.mention_html()}: reply to this message with your answer.",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Your answer"))
        mid = str(msg.message_id)
        S.messages[mid] = (rid, n)
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


# -- the scan -------------------------------------------------------------------------------------

async def scan_loop():
    while True:
        try:
            await scan()
        except Exception as caught:  # noqa: BLE001
            log(f"scan: {caught}")
        await asyncio.sleep(SCAN_EVERY)


async def scan():
    """One pass: each request in its own try block, then release the prompts of the ones that ended."""
    S.loop = asyncio.get_running_loop()
    reqs = await asyncio.to_thread(decisions.open_requests)
    live = {r["id"]: r["status"] for r in reqs}
    if not S.recovered:  # once per boot: what an earlier boot left half done
        S.recovered = True
        for req in reqs:
            if req["kind"] not in ("build", "feedback"):
                continue
            if req["status"] == "executing" and req.get("executing_boot") != BOOT:
                background(decisions.recover, req)
            elif req["status"] == "answered":  # tapped, but the gateway went down before it started
                background(decisions.execute, req["id"], BOOT)
    for req in reqs:
        presented = req.get("presented") or {}
        for n, ids in [*(presented.get("messages") or {}).items(), *(presented.get("replies") or {}).items()]:
            for mid in ids:
                S.messages[str(mid)] = (req["id"], int(n))
        if presented.get("messages"):
            S.watch.add(req["id"])
        try:
            await handle(req)
        except Exception as caught:  # noqa: BLE001 - one poisoned request must not stop the others
            log(f"request {req.get('id')}: {caught}")
    for rid in list(S.retry):  # a request that ended unsent is no longer a failing send
        if live.get(rid) != "open":
            S.retry.pop(rid)
    for rid in [*S.shown, *S.approvals]:  # a request that left `open`: let its waiter threads exit
        if live.get(rid) != "open":
            release(rid)
    if not S.swept:  # requests that ended while the gateway was down
        S.swept = True
        for req in await asyncio.to_thread(decisions.read_all, True):
            if (req.get("presented") or {}).get("messages") and not req.get("edited"):
                S.watch.add(req["id"])
    for rid in list(S.watch):
        if rid not in live:
            try:
                await settle_edit(rid)
            except Exception as caught:  # noqa: BLE001
                log(f"request {rid}: edit: {caught}")


async def edit(chat, mid, text):
    try:
        res = await S.adapter.edit_message(chat, mid, text)
    except Exception as caught:  # noqa: BLE001
        log(f"edit {mid}: {caught}")
        return False
    if not res.success:
        log(f"edit {mid}: {getattr(res, 'error', 'failed')}")
    return bool(res.success)


async def settle_edit(rid):
    """Edit a finished request's newest messages (and reply prompts) to its outcome; one retry on a later scan."""
    req = await asyncio.to_thread(decisions.load, rid)
    if req.get("edited"):
        S.watch.discard(rid)
        return
    chat = core.notify_target()["chat_id"]
    presented = req.get("presented") or {}
    newest = [ids[-1] for ids in (presented.get("messages") or {}).values() if ids]
    newest += [mid for ids in (presented.get("replies") or {}).values() for mid in ids]
    ok = all([await edit(chat, mid, req.get("outcome") or req["status"]) for mid in newest])
    if ok or rid in S.edit_failed:
        S.watch.discard(rid)
        S.edit_failed.discard(rid)
        await asyncio.to_thread(decisions.update, rid, edited=True)
    else:
        S.edit_failed.add(rid)


def release(rid):
    """Cancel a request's Hermes prompts in this process; their waiter threads then exit."""
    from tools import approval, clarify_gateway

    for n in range(S.shown.pop(rid, 0)):
        clarify_gateway.clear_session(session(rid, n))
    S.partial.pop(rid, None)
    if rid in S.approvals:
        S.approvals.discard(rid)
        approval.withdraw_gateway_approval(f"muster:{rid}", rid, "muster: the request ended")


async def handle(req):
    rid, kind = req["id"], req["kind"]
    if kind in ("question", "permission") and time.time() - req.get("alive", req.get("created_at", 0)) > ALIVE_MAX:
        # the hook stopped writing `alive`: it died without SIGTERM (a SIGTERM closes the request itself)
        await asyncio.to_thread(decisions.transition, rid, ("open", "answered"), "stale",
                                outcome="The agent is no longer waiting")
        release(rid)
        return
    if req["status"] != "open" or rid in S.presenting:
        return
    if (req.get("presented") or {}).get("boot") == BOOT:
        return
    wait = S.retry.get(rid)
    if wait and time.monotonic() < wait[0]:
        return
    await (present_approval(req) if kind == "permission" else present(req))


# -- presenting -----------------------------------------------------------------------------------

def title(req):
    run = req.get("run") or {}
    return f"{run['repo']} #{run['issue']}" if run.get("repo") and run.get("issue") else run.get("branch") or req["ledger"]


def render(req, n):
    q, run = req["questions"][n], req.get("run") or {}
    card = req.get("wait") or req["ledger"]
    head = [title(req)]
    tail = ["Several: tap Other and type the numbers, e.g. 1,3" if q.get("multi")
            else "Your own words: tap Other, or reply to this message."]
    if req.get("proposal"):
        p = req["proposal"]
        tail.append(f"Proposal v{p.get('version')} {str(p.get('sha', ''))[:8]} (full text on ledger {req['ledger']})")
    if run.get("pane"):
        tail.append(f"Herdr pane {run['pane']} (optional)")

    def build(described, body):
        # Hermes lists the numbered options under the text, matching its buttons: only add what a label lacks.
        notes = [f"• {o['label']}: {o['description']}" for o in q["options"]
                 if described and o.get("description") and o["description"].strip() != o["label"].strip()]
        return "\n\n".join([*head, body, *(["\n".join(notes)] if notes else []), *tail])

    text = build(True, q["text"])
    if len(text) > CAP:
        tail.append(f"Full options on wait card {card}")
        text = build(False, q["text"])
    if len(text) > CAP:
        room = CAP - len(build(False, ""))
        text = build(False, q["text"][:max(room - 1, 0)] + "…")
    return text[:CAP]


async def present(req):
    """Each question as a Hermes clarify prompt, with a waiter thread for its answer."""
    rid = req["id"]
    S.presenting.add(rid)
    old = req.get("presented") or {}
    messages = {k: list(v) for k, v in (old.get("messages") or {}).items()}
    failure = None
    try:
        if (await asyncio.to_thread(decisions.load, rid))["status"] != "open":
            return
        from tools import clarify_gateway

        chat = core.notify_target()["chat_id"]
        S.partial.pop(rid, None)
        for n, q in enumerate(req["questions"]):
            cid, choices = f"mu{rid}q{n}", req["choices"][n]
            clarify_gateway.register(cid, session(rid, n), q["text"], choices, bool(q.get("multi")))
            S.shown[rid] = n + 1
            threading.Thread(target=waiter, args=(rid, n, cid, S.loop), daemon=True).start()
            try:
                res = await S.adapter.send_clarify(chat, render(req, n), choices, cid, session(rid, n))
            except Exception as caught:  # noqa: BLE001
                res = SimpleNamespace(success=False, error=str(caught))
            if not res.success:
                failure = res.error or "send failed"
                break
            messages.setdefault(str(n), []).append(str(res.message_id))
            S.messages[str(res.message_id)] = (rid, n)
        await presented(rid, messages, failure)
    finally:
        S.presenting.discard(rid)


async def presented(rid, messages, failure):
    """Record a presentation: on failure, cancel what was sent and back off; else mark it this boot's. Every
    earlier message of the request is dead (a tap gets Hermes's "expired" notice) and is edited to say so."""
    S.watch.add(rid)
    earlier = [mid for ids in messages.values() for mid in ids[:-1]]
    if failure:
        release(rid)
        delay = min((S.retry.get(rid, (0, 1))[1]) * 2, BACKOFF_MAX)
        S.retry[rid] = (time.monotonic() + delay, delay)
        log(f"request {rid}: not presented, retry in {delay}s: {failure}")
        await asyncio.to_thread(decisions.update, rid, presented={"messages": messages})
        earlier = [mid for ids in messages.values() for mid in ids]  # this boot's partial send is dead too
    else:
        S.retry.pop(rid, None)
        await asyncio.to_thread(decisions.update, rid, presented={"boot": BOOT, "messages": messages})
    chat = core.notify_target()["chat_id"]
    for mid in earlier:
        await edit(chat, mid, "Superseded: see the newer message")


async def present_approval(req):
    """A permission prompt as Hermes's own approval card (formatted command; Allow once and Deny only, see
    bridge.approval_card), queued and waited on by Hermes's approval wait, so its buttons, edit and
    approvals.timeout apply unchanged. A reply to the card denies with that text."""
    rid, card = req["id"], req["card"]
    S.presenting.add(rid)
    old, chat, loop = (req.get("presented") or {}), core.notify_target()["chat_id"], asyncio.get_running_loop()
    sent = loop.create_future()
    why = f"{title(req)}: {card['why']}. To deny with a message to the agent, reply to this message."

    def notify(_data):  # runs in the wait thread, after Hermes queued the request
        future = asyncio.run_coroutine_threadsafe(S.adapter.send_exec_approval(
            chat, card["command"], f"muster:{rid}", why, allow_permanent=False, allow_session=False), loop)
        try:
            res = future.result(60)
        except Exception as caught:  # noqa: BLE001
            res = SimpleNamespace(success=False, error=str(caught), message_id=None)
        loop.call_soon_threadsafe(lambda: sent.done() or sent.set_result(res))
        if not res.success:
            raise RuntimeError(res.error or "send failed")  # Hermes then returns notify_failed

    try:
        if (await asyncio.to_thread(decisions.load, rid))["status"] != "open":
            return
        S.approvals.add(rid)
        threading.Thread(target=approval_waiter, args=(rid, card["command"], notify, loop), daemon=True).start()
        res = await sent
        messages = {k: list(v) for k, v in (old.get("messages") or {}).items()}
        if res.success:
            messages.setdefault("0", []).append(str(res.message_id))
            S.messages[str(res.message_id)] = (rid, 0)
        await presented(rid, messages, None if res.success else (res.error or "send failed"))
    finally:
        S.presenting.discard(rid)


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
    soon(loop, record(rid, answer))


async def record(rid, answer):
    S.approvals.discard(rid)
    req, ok = await asyncio.to_thread(decisions.transition, rid, ("open",), "answered", answer=answer)
    if ok:
        on_answered(req)


# -- answers --------------------------------------------------------------------------------------

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
    soon(loop, answered(rid, n, value))


def shape(req, n, value):
    """(answer for question n, chosen indexes or None for free text) from the value Hermes resolved: a label,
    a JSON array of labels (multi-select), or free text."""
    q, choices = req["questions"][n], req["choices"][n]
    labels = [value]
    if q.get("multi") and value.startswith("["):
        try:
            labels = [str(v) for v in json.loads(value)]
        except ValueError:
            pass
    if not all(label in choices for label in labels):
        return value, None
    chosen = [choices.index(label) for label in labels]
    return ", ".join(q["options"][i]["label"] for i in chosen), chosen


def compose(req, parts):
    """The request's `answer` from the per-question answers (see decisions: the shape depends on kind)."""
    if req["kind"] == "question":
        return {q["text"]: parts[n][0] for n, q in enumerate(req["questions"])}
    chosen, text = parts[0][1], parts[0][0]
    return {"action": req["actions"][chosen[0]]} if chosen else {"text": text}  # build, feedback


async def answered(rid, n, value):
    """Question n of a request was answered; the request moves to `answered` once every question has been."""
    req = await asyncio.to_thread(decisions.load, rid)
    if req["status"] != "open":
        return
    got = S.partial.setdefault(rid, {})
    got[n] = shape(req, n, value)
    if len(got) < len(req["questions"]):
        return
    got = S.partial.pop(rid)
    parts = [got[i] for i in range(len(req["questions"]))]
    req, ok = await asyncio.to_thread(decisions.transition, rid, ("open",), "answered", answer=compose(req, parts))
    if ok:
        on_answered(req)


def background(fn, *args):
    """Run blocking work off the loop without waiting for it; errors are logged (fn catches its own)."""
    async def go():
        try:
            await asyncio.to_thread(fn, *args)
        except Exception as caught:  # noqa: BLE001
            log(f"{getattr(fn, '__name__', fn)}: {caught}")

    CTX.spawn_task(go())


def on_answered(req):
    """An answered request. question and permission need nothing more: the pane's hook picks the answer up.
    build and feedback are executed here, off the loop; a request they create is presented by the next scan."""
    if req["kind"] in ("build", "feedback"):
        background(decisions.execute, req["id"], BOOT)


# -- typed replies --------------------------------------------------------------------------------

async def on_dispatch(event=None, gateway=None, **kw):
    """pre_gateway_dispatch: route a typed reply to a muster prompt; None lets Hermes handle the message."""
    try:
        return await _dispatch(event, gateway)
    except Exception as caught:  # noqa: BLE001 - never break normal message handling
        log(f"dispatch: {caught}")
        return None


async def _dispatch(event, gateway):
    src = event.source
    if getattr(src.platform, "value", src.platform) != "telegram" or not (event.text or "").strip():
        return None
    ensure_config()
    if not S.messages or not authorized(src.user_id, src.chat_id):  # this hook runs before Hermes authorizes
        return None
    from tools import approval, clarify_gateway

    text, reply = event.text, event.reply_to_message_id
    bound = S.messages.get(str(reply)) if reply is not None else None
    if bound:
        rid, n = bound
        if rid in S.approvals:
            ok = approval.resolve_gateway_approval(f"muster:{rid}", "deny", reason=text) > 0
        else:
            entry = clarify_gateway.get_pending_for_session(session(rid, n), include_choice_prompts=True)
            ok = (entry is not None and clarify_gateway.mark_awaiting_text(entry.clarify_id)
                  and clarify_gateway.resolve_text_response_for_session(session(rid, n), text))
        if ok:
            await edit(src.chat_id, str(reply), f"Received ✓: {text[:200]}")
            return {"action": "skip"}
        return None  # not (or no longer) waiting in this process: leave it to Hermes
    # after "Other", the next message is the answer whatever it replies to (as Hermes's own clarify)
    waiting = [key for rid, count in S.shown.items() for key in (session(rid, n) for n in range(count))
               if clarify_gateway.get_pending_for_session(key) is not None]
    if len(waiting) == 1 and clarify_gateway.resolve_text_response_for_session(waiting[0], text):
        return {"action": "skip"}
    return None
