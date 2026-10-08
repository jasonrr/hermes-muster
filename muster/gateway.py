"""gateway: the Hermes-gateway side of a channel decision.

Runs inside the Hermes gateway process. A scan task presents each `open` request (muster.decisions) as
Hermes clarify prompts, maps taps and typed replies back to answers, and settles the Telegram messages
when a request ends. A guard in front of Hermes's own callback handler lets only the notify user, in the
notify chat, act on a muster prompt. Nothing here runs at import or register time: Hermes calls
`telegram_factory` when the Telegram adapter connects.
"""

import asyncio
import fcntl
import functools
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from . import config, core, decisions

CTX = None  # the Hermes plugin context; set by register()
SCAN_EVERY = 2  # s between scans
ALIVE_MAX = 30  # s without a hook heartbeat before a question or permission request is stale
SETTLE_MAX = 3600  # s after the hook delivered an answer before the request is closed without Claude's confirmation
CAP = 3500  # characters per message (Telegram allows 4096)
BACKOFF_MAX = 60
HEALTHY_FOR = 30  # s of failing sends before the heartbeat stops
STALE = "\x00stale"  # resolves a leftover clarify so its waiter thread exits
BOOT = secrets.token_hex(4)


class State:
    def __init__(self):
        self.adapter = self.loop = self.task = self.lock_fd = self.pool = self.bot = None
        self.configured = self.warned = self.swept = self.recovered = False
        self.presenting = set()  # request ids being presented right now
        self.retry = {}  # id -> (monotonic time not before, next delay)
        self.cids = {}  # id -> clarify ids registered in this process
        self.approvals = set()  # permission request ids whose Hermes approval wait runs in this process
        self.partial = {}  # id -> {question index: answer}: lost on restart, the request is re-presented
        self.messages = {}  # message id -> (request id, question index), including superseded ones
        self.watch = set()  # ids presented or seen open: edited when they end
        self.edit_failed = set()
        self.tasks = set()
        self.failing_since = None  # monotonic time sends started failing


S = State()


def log(line):
    core.log("gateway", line)


def ensure_config():
    if not S.configured:
        config.load(CTX)
        S.configured = True


async def blocking(fn, *args, **kw):
    """Run blocking work (flock, herdr, gh, file IO) off the event loop."""
    S.pool = S.pool or ThreadPoolExecutor(4)
    return await asyncio.get_running_loop().run_in_executor(S.pool, functools.partial(fn, *args, **kw))


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
    s = config.settings
    if s["notify_chat_id"] and not s["notify_user_id"] and not S.warned:
        S.warned = True
        log("config error: notify_chat_id is set but notify_user_id is empty; nobody can act on a muster prompt")
    if S.lock_fd is None:
        path = decisions.root() / ".gateway.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(path, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            log("another gateway process holds decisions/.gateway.lock; this one does not scan")
            return
        S.lock_fd = fd  # held for the life of the process
    if S.task is None or S.task.done() or S.task.get_loop() is not S.loop:
        S.task = S.loop.create_task(scan_loop())


def authorized(user, chat):
    """Only the notify user, in the notify chat. A group with no notify_user_id authorizes nobody."""
    ensure_config()
    s, target = config.settings, core.notify_target()
    return (not (s["notify_chat_id"] and not s["notify_user_id"])
            and str(user) == target["user_id"] and str(chat) == target["chat_id"])


async def guard(update, context):
    """Group -1: only the notify user, in the notify chat, may tap a muster prompt."""
    from telegram.ext import ApplicationHandlerStop

    query = update.callback_query
    try:
        ok = authorized(query.from_user.id, query.message.chat.id)
    except Exception as caught:  # noqa: BLE001 - fail closed
        log(f"guard error, refused: {caught}")
        ok = False
    if ok:
        if str(getattr(query, "data", "")).endswith(":other"):
            await ask_for_text(query)
        return  # Hermes's own clarify handler (group 0) records the tap
    log(f"refused a tap from user {getattr(query.from_user, 'id', '?')} in chat "
        f"{getattr(getattr(query.message, 'chat', None), 'id', '?')}")
    await query.answer("Not authorized")
    raise ApplicationHandlerStop


async def approval_guard(update, context):
    """Group -1, for Hermes's approval cards: a muster card (its session key starts with muster:) only takes the
    notify user's tap in the notify chat. Hermes's own cards pass untouched."""
    from telegram.ext import ApplicationHandlerStop

    query = update.callback_query
    try:
        key = (getattr(S.adapter, "_approval_state", None) or {}).get(int(str(query.data).split(":", 2)[2]))
    except (ValueError, IndexError):
        return
    if not str(key or "").startswith("muster:"):
        return
    try:
        ok = authorized(query.from_user.id, query.message.chat.id)
    except Exception as caught:  # noqa: BLE001 - fail closed
        log(f"approval guard error, refused: {caught}")
        ok = False
    if ok:
        return
    log(f"refused an approval tap from user {getattr(query.from_user, 'id', '?')}")
    await query.answer("Not authorized")
    raise ApplicationHandlerStop


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
        await blocking(add_reply, rid, n, mid)
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


def _touch():
    path = decisions.root() / ".gateway"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


async def scan():
    """One pass: heartbeat, then each request in its own try block, then the ones that ended."""
    S.loop = asyncio.get_running_loop()
    if not S.retry:
        S.failing_since = None  # nothing is waiting on a failed send
    if not S.failing_since or time.monotonic() - S.failing_since < HEALTHY_FOR:
        await blocking(_touch)  # wait cards go wake-only on this; a gateway that cannot send must let Hermes ping
    reqs = await blocking(decisions.open_requests)
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
        if (req.get("presented") or {}).get("messages"):
            S.watch.add(req["id"])
        try:
            await handle(req)
        except Exception as caught:  # noqa: BLE001 - one poisoned request must not stop the others
            log(f"request {req.get('id')}: {caught}")
    for rid in list(S.retry):  # a request that ended unsent is no longer a failing send
        if live.get(rid) != "open":
            S.retry.pop(rid)
    for rid in list(S.cids):  # a request that left `open`: let its waiter threads exit
        if live.get(rid) != "open":
            release(rid)
    if not S.swept:  # requests that ended while the gateway was down
        S.swept = True
        for req in await blocking(decisions.read_all, True):
            if (req.get("presented") or {}).get("messages") and not req.get("edited"):
                S.watch.add(req["id"])
    for rid in list(S.watch):
        if rid in live:
            continue
        try:
            await settle_edit(rid)
        except Exception as caught:  # noqa: BLE001
            log(f"request {rid}: edit: {caught}")


def release(rid):
    from tools import clarify_gateway

    for cid in S.cids.pop(rid, []):
        clarify_gateway.resolve_gateway_clarify(cid, STALE)
    S.partial.pop(rid, None)
    if rid in S.approvals:
        from tools import approval

        S.approvals.discard(rid)
        approval.withdraw_gateway_approval(f"muster:{rid}", rid, "muster: the request ended")


async def handle(req):
    rid, kind = req["id"], req["kind"]
    if kind in ("question", "permission") and req["status"] in ("open", "answered") and not req.get("delivered_by_hook"):
        if time.time() - req.get("alive", req.get("created_at", 0)) > ALIVE_MAX:
            await end(rid, "The agent is no longer waiting")
            return
    if req.get("delivered_by_hook") and time.time() - req.get("alive", req.get("created_at", 0)) > SETTLE_MAX:
        # The hook gave Claude the answer but no PostToolUse ever came (Claude died mid-tool): stop watching it.
        await blocking(decisions.transition, rid, ("answered",), "done", outcome="Answered; muster could not confirm where")
        return
    if req["status"] != "open" or rid in S.presenting:
        return
    if (req.get("presented") or {}).get("boot") == BOOT:
        return
    wait = S.retry.get(rid)
    if wait and time.monotonic() < wait[0]:
        return
    await present(req)


async def end(rid, why):
    await blocking(decisions.transition, rid, ("open", "answered"), "stale", outcome=why)
    release(rid)


# -- presenting -----------------------------------------------------------------------------------

def render(req, n):
    q, run = req["questions"][n], req.get("run") or {}
    title = f"{run['repo']} #{run['issue']}" if run.get("repo") and run.get("issue") else run.get("branch") or req["ledger"]
    card = req.get("wait") or req["ledger"]
    head = [title]
    tail = []
    if req["kind"] == "permission":
        tail.append("To deny with a message to the agent, reply to this message.")
    elif q.get("multi"):
        tail.append("Several: tap Other and type the numbers, e.g. 1,3")
    else:
        tail.append("Your own words: tap Other, or reply to this message.")
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
    if req["kind"] == "permission" and req.get("card"):
        return await present_approval(req)
    rid = req["id"]
    S.presenting.add(rid)
    sent, old = {}, (req.get("presented") or {})
    try:
        if (await blocking(decisions.load, rid))["status"] != "open":
            return
        from tools import clarify_gateway

        chat = core.notify_target()["chat_id"]
        messages = {k: list(v) for k, v in (old.get("messages") or {}).items()}
        S.partial.pop(rid, None)
        failure = None
        for n, q in enumerate(req["questions"]):
            cid, choices = f"mu{rid}q{n}", req["choices"][n]
            clarify_gateway.register(cid, f"muster:{rid}", q["text"], choices, bool(q.get("multi")))
            S.cids.setdefault(rid, []).append(cid)
            threading.Thread(target=waiter, args=(rid, n, cid), daemon=True).start()
            try:
                res = await S.adapter.send_clarify(chat, render(req, n), choices, cid, f"muster:{rid}")
            except Exception as caught:  # noqa: BLE001
                res, failure = None, str(caught)
            if res is None or not res.success:
                failure = failure or getattr(res, "error", None) or "send failed"
                break
            mid = str(res.message_id)
            sent[n] = mid
            messages.setdefault(str(n), []).append(mid)
            S.messages[mid] = (rid, n)
        await presented(rid, chat, old, messages, sent, failure)
    finally:
        S.presenting.discard(rid)


async def presented(rid, chat, old, messages, sent, failure):
    """Record a presentation: on failure, back off and supersede what was sent; else mark it this boot's."""
    if failure:
        S.failing_since = S.failing_since or time.monotonic()
        release(rid)
        delay = min((S.retry.get(rid, (0, 1))[1]) * 2, BACKOFF_MAX)
        S.retry[rid] = (time.monotonic() + delay, delay)
        log(f"request {rid}: not presented, retry in {delay}s: {failure}")
        if sent:  # questions already sent get replaced on the retry
            await blocking(decisions.update, rid, presented={"boot": old.get("boot"), "messages": messages})
            for mid in sent.values():
                await edit(chat, mid, "Superseded: see the newer message")
        return
    S.retry.pop(rid, None)
    S.failing_since = None
    S.watch.add(rid)
    await blocking(decisions.update, rid, presented={"boot": BOOT, "messages": messages})
    for n, ids in (old.get("messages") or {}).items():  # earlier boots' messages
        for mid in ids:
            await edit(chat, mid, "Superseded: see the newer message")


async def present_approval(req):
    """A permission prompt as Hermes's own approval card (formatted command; Allow once, Allow session for a
    Bash command, Deny), queued and waited on by Hermes's approval wait, so its buttons, callback and
    approvals.timeout apply unchanged. A reply to the card denies with that text."""
    rid, card = req["id"], req["card"]
    S.presenting.add(rid)
    old, chat, loop = (req.get("presented") or {}), core.notify_target()["chat_id"], asyncio.get_running_loop()
    sent = loop.create_future()
    run = req.get("run") or {}
    where = f"{run['repo']} #{run['issue']}" if run.get("repo") and run.get("issue") else run.get("branch") or req["ledger"]
    why = f"{where}: {card['why']}. To deny with a message to the agent, reply to this message."

    def notify(_data):  # runs in the wait thread, after Hermes queued the request
        future = asyncio.run_coroutine_threadsafe(S.adapter.send_exec_approval(
            chat, card["command"], f"muster:{rid}", why, allow_permanent=False, allow_session=card["session"]), loop)
        try:
            res = future.result(60)
        except Exception as caught:  # noqa: BLE001
            res = SimpleNamespace(success=False, error=str(caught), message_id=None)
        loop.call_soon_threadsafe(lambda: sent.done() or sent.set_result(res))
        if not res.success:
            raise RuntimeError(res.error or "send failed")  # Hermes then returns notify_failed

    try:
        if (await blocking(decisions.load, rid))["status"] != "open":
            return
        S.approvals.add(rid)
        threading.Thread(target=approval_waiter, args=(rid, card["command"], notify, loop), daemon=True).start()
        res = await sent
        messages = {k: list(v) for k, v in (old.get("messages") or {}).items()}
        if res.success:
            mid = str(res.message_id)
            messages.setdefault("0", []).append(mid)
            S.messages[mid] = (rid, 0)
        await presented(rid, chat, old, messages, {}, None if res.success else (res.error or "send failed"))
    finally:
        S.presenting.discard(rid)


def approval_waiter(rid, command, notify, loop):
    """Block in Hermes's approval wait; hand the human's choice (or Hermes's timeout, a deny) to the loop."""
    from tools import approval_gateway_wait

    try:
        decision = approval_gateway_wait._await_gateway_decision(  # the entry Hermes's MCP elicitation uses too
            f"muster:{rid}", notify, {"command": command, "request_id": rid,
                                       "pattern_key": "muster_permission", "pattern_keys": ["muster_permission"]},
            surface="muster")
    except Exception as caught:  # noqa: BLE001
        log(f"approval {rid}: {caught}")
        return
    if decision.get("notify_failed") or decision.get("cancelled"):
        return  # not sent (present retries), or withdrawn by release()
    choice = decision.get("choice")
    if choice in ("once", "session"):
        answer = {"decision": "allow", **({"scope": "session"} if choice == "session" else {})}
    elif choice == "deny":
        reason = (decision.get("reason") or "").strip()
        answer = {"decision": "deny", **({"message": reason} if reason else {})}
    else:  # Hermes's approvals.timeout passed with no answer: fail closed
        answer = {"decision": "deny", "timeout": True,
                  "message": "No answer from the human in time, so muster denied this. Ask again if you still need it."}
    try:
        loop.call_soon_threadsafe(lambda: _keep(loop.create_task(approval_answered(rid, answer))))
    except RuntimeError:
        log(f"approval {rid}: loop closed, answer dropped")


async def approval_answered(rid, answer):
    S.approvals.discard(rid)
    req, ok = await blocking(decisions.transition, rid, ("open",), "answered", answer=answer,
                             by=None if answer.get("timeout") else core.notify_target()["user_id"])
    if ok:
        on_answered(req)


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
    """Edit a finished request's newest messages to its outcome; one retry on a later scan if that fails."""
    req = await blocking(decisions.load, rid)
    if req["status"] not in decisions.TERMINAL:
        return
    if req.get("edited"):
        S.watch.discard(rid)
        return
    chat = core.notify_target()["chat_id"]
    presented = req.get("presented") or {}
    newest = [ids[-1] for ids in (presented.get("messages") or {}).values() if ids]
    newest += [mid for ids in (presented.get("replies") or {}).values() for mid in ids]  # reply prompts too
    ok = all([await edit(chat, mid, req.get("outcome") or req["status"]) for mid in newest])
    if ok or rid in S.edit_failed:
        S.watch.discard(rid)
        S.edit_failed.discard(rid)
        await blocking(decisions.update, rid, edited=True)
    else:
        S.edit_failed.add(rid)


# -- answers --------------------------------------------------------------------------------------

def waiter(rid, n, cid):
    """One daemon thread per question: block until Hermes resolves the clarify, then hand the text to the loop."""
    from tools import clarify_gateway

    try:
        text = clarify_gateway.wait_for_response(cid, 0)
    except Exception as caught:  # noqa: BLE001
        log(f"waiter {cid}: {caught}")
        return
    if text is None or text.startswith("\x00"):
        return
    loop = S.loop
    try:
        loop.call_soon_threadsafe(lambda: _keep(loop.create_task(answered(rid, n, text))))
    except RuntimeError:  # the loop is closed: the gateway is shutting down
        log(f"waiter {cid}: loop closed, answer dropped")


def _keep(task):
    S.tasks.add(task)
    task.add_done_callback(S.tasks.discard)


def pick(text, choices, multi):
    """Indexes the text chooses (typed number, 'n,m' when multi, or a label, case-insensitive), or None for free text."""
    text = text.strip()
    for i, label in enumerate(choices):  # a tap returns the label itself, which may be a number ("2", "3", "5")
        if label == text:
            return [i]
    if re.fullmatch(r"\d+", text):
        i = int(text) - 1
        return [i] if 0 <= i < len(choices) else None
    if multi and re.fullmatch(r"\d+(\s*,\s*\d+)*", text):
        found = [int(t) - 1 for t in re.split(r"\s*,\s*", text)]
        return found if all(0 <= i < len(choices) for i in found) else None
    for i, label in enumerate(choices):
        if label.casefold() == text.casefold():
            return [i]
    return None


def shape(req, n, text):
    """(answer for question n, extra) from what Hermes returned."""
    q, choices = req["questions"][n], req["choices"][n]
    chosen = pick(text, choices, q.get("multi"))
    if chosen is None:
        return text, None
    return ", ".join(q["options"][i]["label"] for i in chosen), chosen


def compose(req, parts):
    """The request's `answer` from the per-question answers (see decisions: the shape depends on kind)."""
    if req["kind"] == "question":
        return {q["text"]: parts[n][0] for n, q in enumerate(req["questions"])}
    chosen, text = parts[0][1], parts[0][0]
    if req["kind"] == "permission":
        if chosen and req["choices"][0][chosen[0]] == "Allow once":
            return {"decision": "allow"}
        return {"decision": "deny", **({} if chosen else {"message": text})}
    return {"action": req["actions"][chosen[0]]} if chosen else {"text": text}  # build, feedback


async def answered(rid, n, text):
    """Question n of a request was answered; the request moves to `answered` once every question has been."""
    req = await blocking(decisions.load, rid)
    if req["status"] != "open":
        return
    got = S.partial.setdefault(rid, {})
    got[n] = shape(req, n, text)
    if len(got) < len(req["questions"]):
        return
    got = S.partial.pop(rid)
    parts = [got[i] for i in range(len(req["questions"]))]
    req, ok = await blocking(decisions.transition, rid, ("open",), "answered", answer=compose(req, parts),
                                   by=core.notify_target()["user_id"])  # the guard and dispatch admit no one else
    if not ok:
        return
    chat = core.notify_target()["chat_id"]
    for ids in ((req.get("presented") or {}).get("messages") or {}).values():
        if ids:
            await edit(chat, ids[-1], "Received ✓")
    on_answered(req)


def background(fn, *args):
    """Run blocking work on the private pool without waiting for it; errors are logged (fn catches its own)."""
    async def go():
        try:
            await blocking(fn, *args)
        except Exception as caught:  # noqa: BLE001
            log(f"{getattr(fn, '__name__', fn)}: {caught}")

    _keep(asyncio.ensure_future(go()))


def on_answered(req):
    """An answered request. question and permission need nothing more: the pane's hook picks the answer up.
    build and feedback are executed here, off the loop; a request they create is presented by the next scan."""
    if req["kind"] in ("build", "feedback"):
        background(decisions.execute, req["id"], BOOT)


# -- typed replies --------------------------------------------------------------------------------

async def on_dispatch(event=None, **kw):
    """pre_gateway_dispatch: route a typed reply to a muster prompt; None lets Hermes handle the message."""
    try:
        return await _dispatch(event)
    except Exception as caught:  # noqa: BLE001 - never break normal message handling
        log(f"dispatch: {caught}")
        return None


async def _dispatch(event):
    src = event.source
    if getattr(src.platform, "value", src.platform) != "telegram" or not (event.text or "").strip():
        return None
    ensure_config()
    S.loop = asyncio.get_running_loop()
    target = core.notify_target()
    if not authorized(src.user_id, src.chat_id) or not S.messages:
        return None
    from tools import clarify_gateway

    text, reply = event.text, event.reply_to_message_id
    waiting = [e for rid in S.cids
               if (e := clarify_gateway.get_pending_for_session(f"muster:{rid}")) is not None]
    if reply is not None and str(reply) not in S.messages and len(waiting) == 1:
        reply = None  # after "Other", the next message is the answer whatever it replies to (as Hermes's own clarify)
    if reply is not None:
        bound = S.messages.get(str(reply))
        if not bound:
            return None
        rid, n = bound
        req = await blocking(decisions.load, rid)
        if rid in S.approvals:
            from tools import approval

            if req["status"] == "open" and approval.resolve_gateway_approval(f"muster:{rid}", "deny", reason=text):
                return {"action": "skip"}
        cid = f"mu{rid}q{n}"
        if req["status"] == "open" and cid not in S.cids.get(rid, []):
            return None  # not (yet) presented in this process: leave it to Hermes
        if req["status"] == "open" and clarify_gateway.resolve_gateway_clarify(cid, text):
            return {"action": "skip"}
        done = req.get("outcome") or req.get("answer") or "answer received"
        await edit(target["chat_id"], str(reply), f"already handled: {done}")
        return {"action": "skip"}
    if len(waiting) == 1 and clarify_gateway.resolve_gateway_clarify(waiting[0].clarify_id, text):
        return {"action": "skip"}
    return None
