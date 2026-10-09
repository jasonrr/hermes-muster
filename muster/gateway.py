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
- send retry with backoff: Hermes sends once;
- one forum topic per run (`notify_topics`, muster.conversation): created with Hermes's create_handoff_thread, a
  lost create reconciled from the bot's own forum_topic_created service message, a deleted topic found by a
  reopen probe and recreated with the run's subscriptions and open prompts moved to it, closed when the run is
  finished and reopened when it resumes. Hermes's kanban notifier already sends pings and runs wakes in a
  subscription's thread, so the coordinator follows the topic without any code here.
Private Hermes names are reached only through muster.hermes_private. Nothing here runs at import or register
time: Hermes calls `telegram_factory` when the Telegram adapter connects.
"""

import asyncio
import contextlib
import json
import re
import secrets
import threading
import time
from types import SimpleNamespace

from . import config, conversation, core, decisions, hermes_private

CTX = None  # the Hermes plugin context; set by register()
SCAN_EVERY = 2  # s between scans
ALIVE_MAX = 30  # s without a hook heartbeat before a question or permission request is stale
CAP = 3500  # characters per message (Telegram allows 4096)
LABEL_MAX = 32  # characters on a button; Telegram's phone client cuts longer ones
BACKOFF_MAX = 60
BOOT = secrets.token_hex(4)
TOPIC_RETRY = 10  # s before another try at checking the group, after a check that could not run
TOPIC_ATTEMPTS = 3
NOTICE_WAIT = 20  # s to wait for Telegram's topic-created notice after a create that returned no thread
MIGRATE_MAX = 60  # s, the longest wait between retries of a card subscription that did not move
PROBE_EVERY = 30  # s between deleted-topic probes of an open run topic
now, clock = time.time, time.monotonic  # topic timing only; tests patch these


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
        self.ledgers = {}  # request id -> ledger card: whose conversation (topic) the request belongs to
        self.probed = {}  # ledger -> clock() of the last deleted-topic probe
        self.repairing = set()  # ledgers whose cards and prompts are moving to a new topic


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
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    config.load(CTX)
    S.configured = True
    core.prepare_env()
    app.add_handler(CallbackQueryHandler(guard, pattern=r"^cl:mu"), -1)
    app.add_handler(CallbackQueryHandler(approval_guard, pattern=r"^ea:"), -1)
    app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CREATED, topic_created), -1)
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


async def in_topic(rid, thread):
    """The message (or reply) is in the request's conversation: its run's topic, or the main chat (General) for a
    run without one. A request muster no longer has passes: Hermes answers it with its own "expired"."""
    ledger = S.ledgers.get(rid)
    try:
        if ledger is None:
            ledger = S.ledgers[rid] = (await asyncio.to_thread(decisions.load, rid))["ledger"]
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

    if await in_topic(rid, getattr(query.message, "message_thread_id", None)):
        return
    log(f"request {rid}: tap outside its run's topic refused; presenting it again")
    S.probed.pop(S.ledgers.get(rid), None)
    release(rid)
    with contextlib.suppress(OSError, ValueError, KeyError):
        presented = (await asyncio.to_thread(decisions.load, rid)).get("presented") or {}
        await asyncio.to_thread(decisions.update, rid, presented={k: v for k, v in presented.items() if k != "boot"})
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
    """A tap on a Hermes approval card: a muster card (session muster:...) takes only the notify user's tap;
    Hermes's own cards pass untouched."""
    query = update.callback_query
    key = str(hermes_private.approval_session(S.adapter, query.data) or "")
    if key.startswith("muster:"):
        await refuse_unless_authorized(query)
        await refuse_unless_in_topic(query, key.split(":")[1])


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
            message_thread_id=getattr(query.message, "message_thread_id", None),
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
    try:  # before the requests are read: a repair clears the presentations it moves
        await topics()
    except Exception as caught:  # noqa: BLE001
        log(f"topics: {caught}")
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
        S.ledgers[req["id"]] = req["ledger"]
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
    if req.get("prompt_sha"):  # a retry whose first send may have arrived after all
        S.presenting.add(rid)  # held across the await, so a concurrent scan cannot present it meanwhile
        try:
            retired = await asyncio.to_thread(decisions.reconcile, req)
        finally:
            S.presenting.discard(rid)
        if retired:
            release(rid)
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
    q = req["questions"][n]
    card = req.get("wait") or req["ledger"]
    head = [title(req)]
    tail = ["Several: tap Other and type the numbers, e.g. 1,3" if q.get("multi")
            else "Your own words: tap Other, or reply to this message."]
    if req.get("proposal"):
        p = req["proposal"]
        tail.append(f"Proposal v{p.get('version')} {str(p.get('sha', ''))[:8]} (full text on ledger {req['ledger']})")
    shown = buttons(req, n)

    def build(described, body):
        # The buttons carry the labels: add only what a button lacks, a description (dropped first) or a cut label,
        # under the button's number when it has one.
        notes = []
        for i, (o, choice, button) in enumerate(zip(q["options"], req["choices"][n], shown)):
            said = described and o.get("description") and o["description"].strip() != o["label"].strip()
            if said or choice not in button:
                name = f"{i + 1}. {choice}" if button.startswith(f"{i + 1}. ") else choice
                notes.append(f"• {name}: {o['description']}" if said else f"• {name}")
        return "\n\n".join([*head, body, *(["\n".join(notes)] if notes else []), *tail])

    text = build(True, q["text"])
    if len(text) > CAP:
        tail.append(f"Full options on wait card {card}")
        text = build(False, q["text"])
    if len(text) > CAP:
        room = CAP - len(build(False, ""))
        text = build(False, q["text"][:max(room - 1, 0)] + "…")
    return text[:CAP]


def buttons(req, n):
    """Each choice's button text: its label cut to LABEL_MAX, numbered when several may be picked (the typed answer
    names the numbers) or when plain labels would leave a button blank or two alike after the cut."""
    choices = req["choices"][n]

    def fit(s):
        # ponytail: cuts by code point, so a long emoji sequence can split; cut by grapheme if labels carry them
        return s if len(s) <= LABEL_MAX else s[:LABEL_MAX - 1] + "…"

    plain = [fit(c) for c in choices]
    if req["questions"][n].get("multi") or len(set(plain)) < len(plain) or not all(p.strip() for p in plain):
        return [fit(f"{i + 1}. {c}") for i, c in enumerate(choices)]
    return plain


async def present(req):
    """Each question as a Hermes clarify prompt, with a waiter thread for its answer."""
    rid = req["id"]
    S.presenting.add(rid)
    old = req.get("presented") or {}
    messages = {k: list(v) for k, v in (old.get("messages") or {}).items()}
    failure = None
    try:
        ready, meta = await destination(req["ledger"])  # first: nothing is registered for a request that waits
        if not ready or (await asyncio.to_thread(decisions.load, rid))["status"] != "open":
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
                res = await hermes_private.send_labelled_clarify(S.adapter, chat, render(req, n), buttons(req, n),
                                                                 cid, session(rid, n), metadata=meta)
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
    meta = None

    def notify(_data):  # runs in the wait thread, after Hermes queued the request
        future = asyncio.run_coroutine_threadsafe(S.adapter.send_exec_approval(
            chat, card["command"], f"muster:{rid}", why, metadata=meta, allow_permanent=False, allow_session=False),
            loop)
        try:
            res = future.result(60)
        except Exception as caught:  # noqa: BLE001
            res = SimpleNamespace(success=False, error=str(caught), message_id=None)
        loop.call_soon_threadsafe(lambda: sent.done() or sent.set_result(res))
        if not res.success:
            raise RuntimeError(res.error or "send failed")  # Hermes then returns notify_failed

    try:
        ready, meta = await destination(req["ledger"])  # before the approval wait starts
        if not ready or (await asyncio.to_thread(decisions.load, rid))["status"] != "open":
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
    thread = getattr(src, "thread_id", None)
    bound = S.messages.get(str(reply)) if reply is not None else None
    if bound:
        rid, n = bound
        if not await in_topic(rid, thread):
            return None  # a reply from outside the run's conversation is not an answer
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
    # after "Other", the next message in that conversation is the answer whatever it replies to (as Hermes's own
    # clarify): only prompts of the topic it was typed in count, so two runs' prompts never compete
    waiting = [key for rid, count in list(S.shown.items()) if await in_topic(rid, thread)
               for key in (session(rid, n) for n in range(count))
               if clarify_gateway.get_pending_for_session(key) is not None]
    if len(waiting) == 1 and clarify_gateway.resolve_text_response_for_session(waiting[0], text):
        return {"action": "skip"}
    return None


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


async def destination(ledger):
    """(ready, send metadata) for a request of this run: its topic, reopened and checked first. Not ready while
    the topic is being made, moved or closed: the request waits for a later scan."""
    ref = await asyncio.to_thread(conversation.load, ledger)
    state = (ref or {}).get("state")
    if ref and (ledger in S.repairing or (ref.get("previous") and not prompts_moved(ref))):
        return False, None  # a new destination its prompts have not moved to yet
    if state in (None, "fallback"):
        return True, None  # the main chat
    if state not in ("open", "closed"):
        return False, None  # being made, or being closed
    if await probe(ref) == "deleted":
        return False, None
    if state == "closed":  # the run resumed: the probe reopened it
        await asyncio.to_thread(conversation.swap, ledger, ("closed",), state="open")
        log(f"{ledger}: topic {ref['thread_id']} reopened")
    return True, {"thread_id": ref["thread_id"]}


async def probe(ref):
    """Is the run's topic still there? Reopening an open topic changes nothing (TOPIC_NOT_MODIFIED) and renames
    nothing; a deleted one fails "thread not found". A deleted topic is recreated by the next scans."""
    ledger, thread = ref["ledger"], ref["thread_id"]
    S.probed[ledger] = clock()
    try:
        await S.bot.reopen_forum_topic(ref["chat_id"], int(thread))
        return "alive"
    except Exception as caught:  # noqa: BLE001
        found, error = outcome_of(caught), caught
    if found == "deleted":
        log(f"{ledger}: topic {thread} is gone; making a new one")
        await asyncio.to_thread(conversation.swap, ledger, ("open", "closed"), state="creating", thread_id=None,
                                previous=[*ref.get("previous", []), thread], attempts=0, since=0, asked=False,
                                moved=None, repaired=None, migrate_at=0, migrate_delay=0)
    elif found == "unknown":
        log(f"{ledger}: topic {thread} probe: {error}")
    return found


async def topics():
    """Once per scan, before the requests: each run's topic one step further."""
    if S.bot is None or S.adapter is None:
        return
    for ref in await asyncio.to_thread(conversation.active):
        try:
            await topic_step(ref)
        except Exception as caught:  # noqa: BLE001 - one run's topic must not stop the others
            log(f"topic {ref.get('ledger')}: {caught}")


async def topic_step(ref):
    ledger, state, chat = ref["ledger"], ref.get("state"), ref.get("chat_id")
    if state in ("pending", "creating"):
        await create(ref)
    elif state == "fallback":
        if not ref.get("noticed"):
            await S.adapter.send(chat, f"Topic not created for {ref.get('name')}: {ref.get('why')}. "
                                       f"This run stays in the main chat.")
            await asyncio.to_thread(conversation.update, ledger, noticed=True)
        if ref.get("previous") and ref.get("repaired") != "main":
            await repair(ref)  # a recreate that failed: the run moves to the main chat
    elif state == "closing":
        _, ok = await asyncio.to_thread(conversation.swap, ledger, ("closing",), state="closed")
        if ok:  # once: a failed send or close is logged, never repeated
            try:
                await S.adapter.send(chat, "Run finished; worktree removed. History kept.",
                                     metadata={"thread_id": ref["thread_id"]})
                await S.bot.close_forum_topic(chat, int(ref["thread_id"]))
            except Exception as caught:  # noqa: BLE001
                if outcome_of(caught) == "unknown":
                    log(f"{ledger}: closing topic {ref['thread_id']}: {caught}")
    elif state == "open":
        if ref.get("previous") and ref.get("repaired") != ref["thread_id"]:
            await repair(ref)  # until every card's subscription reads back here
            ref = await asyncio.to_thread(conversation.load, ledger)
        if ref.get("state") == "open" and clock() - S.probed.get(ledger, float("-inf")) >= PROBE_EVERY:
            await probe(ref)  # on its own timer: a card still moving must not hide a new deletion


async def create(ref):
    """At most one createForumTopic call per destination. Its result can be lost: Hermes's create_handoff_thread
    turns every error into None, including a timeout after Telegram made the topic. So a None is never retried
    (that could make a second topic). The topic is adopted from Telegram's topic-created notice when one arrives
    (topic_created), and after NOTICE_WAIT without one the run uses the main chat; a notice that turns up later
    closes that topic as unused. Only a prerequisite check that could not run is tried again (3 times)."""
    ledger, attempts = ref["ledger"], ref.get("attempts", 0)
    if ref.get("asked"):
        if now() - ref.get("since", 0) >= NOTICE_WAIT:
            await asyncio.to_thread(
                conversation.swap, ledger, ("creating",), state="fallback", noticed=False,
                why="Telegram neither confirmed the topic nor sent its notice; not retried, so no second topic is made")
        return
    if ref["state"] == "creating" and now() - ref.get("since", 0) < TOPIC_RETRY:
        return
    if attempts >= TOPIC_ATTEMPTS:
        await asyncio.to_thread(conversation.swap, ledger, ("pending", "creating"), state="fallback", noticed=False,
                                why=f"could not check the group after {TOPIC_ATTEMPTS} attempts")
        return
    ref, ok = await asyncio.to_thread(conversation.swap, ledger, ("pending", "creating"), state="creating",
                                      attempts=attempts + 1, since=now())
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
    _, ok = await asyncio.to_thread(conversation.swap, ledger, ("creating",), asked=True, since=now())
    if not ok:
        return
    thread = await S.adapter.create_handoff_thread(ref["chat_id"], ref["name"])
    if thread:
        await settle(ledger, str(thread))
    else:
        log(f"{ledger}: the topic create returned no thread; waiting {NOTICE_WAIT} s for Telegram's notice, no retry")


async def prerequisites(chat):
    """None when the bot may make topics in the chat; else (definite, why). An error checking is not definite."""
    try:
        info = await S.bot.get_chat(chat)
        if not getattr(info, "is_forum", False):
            return True, "Topics are not enabled in the group (group settings > Topics)"
        member = await S.bot.get_chat_member(chat, S.bot.id)
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
        S.probed[ledger] = clock()
        log(f"{ledger}: topic {thread} open")
    elif ref and ref.get("thread_id") != thread:
        await close_duplicate(ref, thread)


async def close_duplicate(ref, thread):
    where = f'the topic "{ref.get("name")}"' if ref.get("state") in conversation.THREADED else "the main chat"
    log(f"{ref['ledger']}: duplicate topic {thread} closed")
    try:
        await S.adapter.send(ref["chat_id"], f"Duplicate topic, not used; this run is in {where}.",
                             metadata={"thread_id": thread})
        await S.bot.close_forum_topic(ref["chat_id"], int(thread))
    except Exception as caught:  # noqa: BLE001
        log(f"{ref['ledger']}: closing duplicate topic {thread}: {caught}")


async def topic_created(update, context):
    """Telegram's forum_topic_created notice of a topic the bot itself made in the notify chat. Its name carries the
    run's ledger id (conversation.name), so it names exactly one run: the thread of that run's create whose reply was
    lost is adopted; any other topic for the run is closed as unused. A name that matches no run, or several, is
    left alone."""
    try:
        msg = update.effective_message
        made = getattr(msg, "forum_topic_created", None)
        if not made or S.bot is None or getattr(msg.from_user, "id", None) != S.bot.id:
            return
        ensure_config()
        if str(msg.chat.id) != core.notify_target()["chat_id"]:
            return
        thread = str(msg.message_thread_id)
        log(f"topic notice: thread {thread} {made.name!r}")  # the live evidence that Telegram sends these to the bot
        mine = [r for r in await asyncio.to_thread(conversation.every) if r.get("name") == made.name]
        if len(mine) != 1:
            if mine:
                log(f"topic notice: {len(mine)} runs are named {made.name!r}; ambiguous, ignored")
            return
        ref = mine[0]
        if thread in ref.get("previous", []):
            return  # a late notice of a topic the run already left (deleted): never adopted, never closed
        if ref.get("state") == "creating" and ref.get("asked"):
            await settle(ref["ledger"], thread)
        elif ref.get("thread_id") != thread and thread not in ref.get("previous", []):
            await close_duplicate(ref, thread)
    except Exception as caught:  # noqa: BLE001 - never break Hermes's own update handling
        log(f"topic notice: {caught}")


async def repair(ref):
    """The run's topic was recreated (or given up on). First, once: its open requests are presented again there and
    the move is said. Then each card's subscription is moved, and a card whose move does not read back is kept in
    `migrating` and tried again (backing off to MIGRATE_MAX). `repaired` is written only when none is left."""
    ledger, dest = ref["ledger"], dest_of(ref)
    if ledger in S.repairing or now() < ref.get("migrate_at", 0):
        return
    S.repairing.add(ledger)
    try:
        if ref.get("moved") != dest:
            for req in await asyncio.to_thread(decisions.for_ledger, ledger):
                if req["status"] == "open":
                    release(req["id"])
                    presented = req.get("presented") or {}
                    await asyncio.to_thread(decisions.update, req["id"],
                                            presented={k: v for k, v in presented.items() if k != "boot"})
            if ref.get("thread_id"):
                await S.adapter.send(ref["chat_id"], "The previous topic was deleted; this run continues here.",
                                     metadata={"thread_id": ref["thread_id"]})
            ref = await asyncio.to_thread(conversation.update, ledger, moved=dest, migrating=list(ref.get("cards", [])),
                                          migrate_delay=0, migrate_at=0)
        left = await asyncio.to_thread(move_cards, ref)
        if left:
            delay = min(max(ref.get("migrate_delay", 0) * 2, SCAN_EVERY), MIGRATE_MAX)
            await asyncio.to_thread(conversation.update, ledger, migrating=left, migrate_delay=delay,
                                    migrate_at=now() + delay)
            log(f"{ledger}: {len(left)} card subscription(s) not moved to {dest} yet, retry in {delay} s: {left}")
        else:
            await asyncio.to_thread(conversation.update, ledger, migrating=[], repaired=dest)
            log(f"{ledger}: moved to {dest}")
    finally:
        S.repairing.discard(ledger)


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
