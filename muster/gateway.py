"""gateway: the Hermes-gateway side of a channel decision, channel-neutral.

Runs inside the Hermes gateway process. A scan task presents each `open` request (muster.decisions) through the
configured channel (muster.channels: Telegram or Slack, picked by `notify_platform`), waits for the answer the
channel hands back (`channels.shared.answer` / `permission`), and edits every message of an ended request to its
outcome (Answer sent to the builder, Denied · sent to the builder, Answered in the pane...), so the human knows whether muster passed
their answer on (not that Claude applied it). It also retries a failed send with backoff (Hermes sends once), and
on a channel whose prompts die with the process (Telegram) presents open requests again on each boot.
Nothing here runs at import or register time: the channel's Hermes factory starts the scan when the adapter connects.
"""

import asyncio
import json
import secrets
import time

from . import channels, config, conversation, core, decisions, progress
from .channels import shared
from .channels.shared import log

ALIVE_MAX = 30  # s without a hook heartbeat before a question or permission request is stale
BACKOFF_MAX = 60
BOOT = secrets.token_hex(4)
CHANNEL = None  # the configured channel module; set by setup()


class State:
    def __init__(self):
        self.loop = self.task = None
        self.recovered = False
        self.presenting = set()  # request ids being presented right now
        self.retry = {}  # id -> (monotonic time not before, next delay)
        self.watch = set()  # ids presented or seen open: edited to their outcome when they end
        self.edit_failed = set()
        self.swept = False
        self.vetted = {}  # build or feedback request id -> clock() of the last read of its pull request
        self.covered = set()  # (ledger, proposal) of each open design review: it stands in for that approval question
        self.final = set()  # ledgers whose status message is final: never read again this boot


S = State()


def setup(ctx):
    """From the plugin's register(): wire the configured channel's Hermes surfaces, and only those."""
    shared.CTX = ctx
    global CHANNEL
    platform = ctx.get_config("notify_platform", "telegram") if hasattr(ctx, "get_config") else "telegram"
    try:
        CHANNEL = channels.get(platform)
    except config.ConfigError as caught:  # the plugin still loads; `hermes muster` reports it through require()
        log(f"no channel: {caught}")
        return
    if not CHANNEL.register(ctx):
        log(f"this Hermes lacks the {platform} surfaces muster needs; no prompt will be shown")


def start_scan():
    """From a channel's Hermes factory, on the gateway loop: one scan task, kept across adapter reconnects."""
    S.loop = asyncio.get_running_loop()
    if S.task is None or S.task.done() or S.task.get_loop() is not S.loop:
        S.task = shared.CTX.spawn_task(scan_loop(), name="muster:scan")  # Hermes cancels it on plugin unload


# -- the scan -------------------------------------------------------------------------------------

async def scan_loop():
    while True:
        try:
            await scan()
        except Exception as caught:  # noqa: BLE001
            log(f"scan: {caught}")
        await asyncio.sleep(shared.SCAN_EVERY)


async def scan():
    """One pass: each request in its own try block, then release the prompts of the ones that ended."""
    S.loop = asyncio.get_running_loop()
    try:  # before the steps: a resolved run's last edit lands while its topic is still closing, not closed
        await progress_pass()
    except Exception as caught:  # noqa: BLE001
        log(f"progress: {caught}")
    try:  # before the requests are read: a repair clears the presentations it moves
        await CHANNEL.steps()
    except Exception as caught:  # noqa: BLE001
        log(f"topics: {caught}")
    reqs = await asyncio.to_thread(decisions.open_requests)
    live = {r["id"]: r["status"] for r in reqs}
    if not S.recovered:  # once per boot: what an earlier boot left half done
        S.recovered = True
        for req in reqs:
            if req["kind"] not in ("build", "feedback", "design"):
                continue
            if req["status"] == "executing" and req.get("executing_boot") != BOOT:
                background(decisions.recover, req)
            elif req["status"] == "answered":  # tapped, but the gateway went down before it started
                background(decisions.execute, req["id"], BOOT)
    S.covered = {(r["ledger"], pinned(r)) for r in reqs if r["kind"] == "design" and r["status"] == "open"}
    for req in reqs:
        CHANNEL.track(req)
        if (req.get("presented") or {}).get("messages"):
            S.watch.add(req["id"])
        try:
            if req["kind"] == "design" and req["status"] == "executing":
                await waiting(req, reqs)
            await handle(req)
        except Exception as caught:  # noqa: BLE001 - one poisoned request must not stop the others
            log(f"request {req.get('id')}: {caught}")
    for rid in list(S.retry):  # a request that ended unsent is no longer a failing send
        if live.get(rid) != "open":
            S.retry.pop(rid)
    CHANNEL.release_ended(live)
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


async def progress_pass():
    """Each launched run's one status message (muster.progress), one step further. It shows evidence only: nothing
    here retries, redispatches, approves, merges or cleans up."""
    waits = await asyncio.to_thread(lambda: progress.needs(decisions.open_requests()))
    for ledger in await asyncio.to_thread(progress.tracked, S.final):
        try:
            await show_progress(ledger, waits.get(ledger, []))
        except Exception as caught:  # noqa: BLE001 - one run's message must not stop the others
            log(f"{ledger}: progress: {caught}")


async def show_progress(ledger, waits):
    now, saved = shared.now(), await asyncio.to_thread(progress.load, ledger)
    if now < saved.get("retry_at", 0):
        return
    ref = await asyncio.to_thread(conversation.load, ledger)
    archived = None  # a run with a conversation reference: its state says archived (the channel steps read the board)
    if ref is None and shared.clock() - shared.checked.get(ledger, float("-inf")) >= shared.CLOSED_EVERY:
        archived = await shared.archived(ledger)
    key, text, final = await asyncio.to_thread(progress.state, ledger, now, waits, ref, archived)
    thread = (await asyncio.to_thread(conversation.target, ledger))["thread_id"]
    posting = not (saved.get("mid") and saved.get("thread") == thread)
    if not (posting or progress.due(saved, key, text, now)):
        return
    if posting and final:  # archived or finished before it was ever shown here: nothing worth a first post
        await asyncio.to_thread(progress.save, ledger, final=True, posting=None)
        S.final.add(ledger)
        return
    if posting and {"thread": thread} in (saved.get("lost"), saved.get("posting")):
        # A post here whose outcome is unknown (it raised, or the gateway died mid-send): never a second one.
        if saved.get("posting"):
            log(f"{ledger}: status message post not confirmed; not posted again")
            await asyncio.to_thread(progress.save, ledger, lost={"thread": thread}, posting=None)
        return
    if posting:
        await asyncio.to_thread(progress.save, ledger, posting={"thread": thread})
    try:
        shown = await CHANNEL.show(ledger, text, saved)
    except Exception as caught:  # noqa: BLE001
        log(f"{ledger}: status message: {caught}")
        if posting:
            await asyncio.to_thread(progress.save, ledger, lost={"thread": thread}, posting=None)
        return
    if shown:
        fields = {**shown, "key": key, "text": text, "edited_at": now, "final": final, "retry_at": 0}
    elif shown is None and final:  # nothing left to show it on (no message, or the run's conversation is gone)
        fields = {"final": True}
    else:  # None: not ready yet, tried again next scan; False: refused (a final edit too), tried after RETRY
        fields = {} if shown is None else {"retry_at": now + progress.RETRY}
    await asyncio.to_thread(progress.save, ledger, posting=None, **fields)
    if fields.get("final"):
        S.final.add(ledger)


async def settle_edit(rid):
    """Edit a finished request's messages to its outcome; one retry on a later scan."""
    req = await asyncio.to_thread(decisions.load, rid)
    if req.get("edited"):
        S.watch.discard(rid)
        return
    ok = await CHANNEL.outcome(req)
    if ok or rid in S.edit_failed:
        S.watch.discard(rid)
        S.edit_failed.discard(rid)
        await asyncio.to_thread(decisions.update, rid, edited=True)
    else:
        S.edit_failed.add(rid)


async def handle(req):
    rid, kind = req["id"], req["kind"]
    if kind in ("question", "permission") and time.time() - req.get("alive", req.get("created_at", 0)) > ALIVE_MAX:
        # the hook stopped writing `alive`: it died without SIGTERM (a SIGTERM closes the request itself)
        await asyncio.to_thread(decisions.transition, rid, ("open", "answered"), "stale",
                                outcome="Ended: the builder stopped waiting. Nothing was sent.")
        CHANNEL.release(rid)
        return
    if req["status"] != "open" or rid in S.presenting:
        return
    if req.get("prompt_sha"):  # a retry whose first send may have arrived after all
        S.presenting.add(rid)  # held across the await, so a concurrent scan cannot present it meanwhile
        try:  # retired now, or answered by a tap during the await: either way not to present
            ended = await asyncio.to_thread(lambda: decisions.reconcile(req) or decisions.load(rid)["status"] != "open")
        finally:
            S.presenting.discard(rid)
        if ended:
            CHANNEL.release(rid)
            return
    if CHANNEL.durable_prompts and await obsolete(req):  # first: a durable Merge button stays live otherwise
        return
    shown = req.get("presented") or {}
    if shown and shown.get("platform", "telegram") != CHANNEL.name:  # shown on another platform (none: Telegram)
        await asyncio.to_thread(decisions.update, rid, presented={}, partial={})  # its ids and answers are not ours
        shown, req = {}, {**req, "partial": {}}
    covered = decisions.approval_slot(req)
    if covered is not None and (req["ledger"], pinned(req)) in S.covered:
        shown = await redirect(req, covered, shown)
    else:
        covered = None
    # a question already answered keeps its answer across restarts; one a design review stands in for is not shown
    slots = ["0"] if kind == "permission" else [str(n) for n in range(len(req["questions"]))
                                                if str(n) not in (req.get("partial") or {}) and n != covered]
    if CHANNEL.durable_prompts or shown.get("boot") == BOOT:  # a prompt sent (this boot) still works: only new ones
        slots = [n for n in slots if not (shown.get("messages") or {}).get(n)]
    if not slots:
        return
    wait = S.retry.get(rid)
    if wait and time.monotonic() < wait[0]:
        return
    if not CHANNEL.durable_prompts and await obsolete(req):
        return
    S.presenting.add(rid)
    try:
        ready, meta = await CHANNEL.destination(req)  # first: nothing is registered for a request that waits
        if not ready or (await asyncio.to_thread(decisions.load, rid))["status"] != "open":
            return
        if kind == "permission":
            sent = await CHANNEL.present_approval(req, meta)
        else:
            sent = await CHANNEL.present(req, meta, [int(n) for n in slots])
        messages = {k: list(v) for k, v in (shown.get("messages") or {}).items()}
        for n, ids in sent["messages"].items():
            messages.setdefault(n, []).extend(ids)
        await presented(rid, messages, sent["failure"])
    finally:
        S.presenting.discard(rid)


def pinned(req):
    return json.dumps(req.get("proposal"), sort_keys=True)


async def redirect(req, slot, shown):
    """The builder's Approval question while a design review of its version stands in for it: its messages say so
    and lose their controls (a typed answer still counts: first answer wins). Their ids move to `redirected`, so
    the question shows again if the review ends without answering it. Returns the new presentation."""
    ids = (shown.get("messages") or {}).get(str(slot))
    if not ids:
        return shown
    who = config.settings.get("coordinator_name") or "Coordinator"
    line = f"Plan v{req['proposal'].get('version')} ready · decide on {who}'s review below."
    chat = core.notify_target()["chat_id"]
    for mid in ids:
        await CHANNEL.edit(chat, mid, line)
    messages = {k: v for k, v in shown["messages"].items() if k != str(slot)}
    redirected = {**(shown.get("redirected") or {}), str(slot): [*(shown.get("redirected") or {}).get(str(slot), []),
                                                                 *ids]}
    shown = {**shown, "messages": messages, "redirected": redirected}
    await asyncio.to_thread(decisions.update, req["id"], presented=shown)
    return shown


async def waiting(req, reqs):
    """An `executing` design request one step further (decisions.settle_design); its card says what it waits on."""
    line = await asyncio.to_thread(decisions.settle_design, req, reqs)
    if line and line != req.get("interim") and await CHANNEL.outcome({**req, "outcome": line}):
        await asyncio.to_thread(decisions.update, req["id"], interim=line)


async def obsolete(req):
    """A build or feedback request whose pull request moved on is staled, never shown again (read at most every
    CHECK_EVERY). True when it was."""
    rid = req["id"]
    if req["kind"] not in ("build", "feedback") or shared.clock() - S.vetted.get(rid, float("-inf")) < shared.CHECK_EVERY:
        return False
    S.vetted[rid] = shared.clock()
    why = await asyncio.to_thread(decisions.obsolete, req)
    if not why:
        return False
    await asyncio.to_thread(decisions.transition, rid, ("open",), "stale", outcome=f"Not shown: {why}")
    CHANNEL.release(rid)
    return True


async def presented(rid, messages, failure):
    """Record a presentation: on failure, cancel what was sent and back off; else mark it this boot's. On a channel
    whose prompts die with the process, every earlier message of the request is dead (a tap gets Hermes's "expired"
    notice) and is edited to say so; a durable channel's earlier messages still work and stay."""
    S.watch.add(rid)
    earlier = [mid for ids in messages.values() for mid in ids[:-1]]
    if failure:
        CHANNEL.release(rid)
        delay = min((S.retry.get(rid, (0, 1))[1]) * 2, BACKOFF_MAX)
        S.retry[rid] = (time.monotonic() + delay, delay)
        log(f"request {rid}: not presented, retry in {delay}s: {failure}")
        await asyncio.to_thread(decisions.update, rid, presented={"messages": messages, "platform": CHANNEL.name})
        earlier = [mid for ids in messages.values() for mid in ids]  # this boot's partial send is dead too
    else:
        S.retry.pop(rid, None)
        await asyncio.to_thread(decisions.update, rid,
                                presented={"boot": BOOT, "messages": messages, "platform": CHANNEL.name})
    if CHANNEL.durable_prompts:
        return
    chat = core.notify_target()["chat_id"]
    for mid in earlier:
        await CHANNEL.edit(chat, mid, "Replaced after a restart · use the newer message below.")


async def record(rid, answer):
    """A permission request's answer: (request, True) when it moved it to answered."""
    req, ok = await asyncio.to_thread(decisions.transition, rid, ("open",), "answered", answer=answer)
    if ok:
        on_answered(req)
    return req, ok


# -- answers --------------------------------------------------------------------------------------

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
    """Question n of a request was answered: its part is kept in the request file (the first answer to a question
    wins), and the request moves to `answered` once every question has one. Returns (request, result, the stored
    part), result as decisions.answer_part's."""
    req = await asyncio.to_thread(decisions.load, rid)
    part = shape(req, n, value)
    req, result = await asyncio.to_thread(decisions.answer_part, rid, n, part, len(req["questions"]),
                                          lambda parts, asked=req: compose(asked, parts))
    if result == "completed":
        on_answered(req)
    return req, result, (req.get("partial") or {}).get(str(n))


shared.answer, shared.permission, shared.start_scan = answered, record, start_scan  # the hub channels call


def background(fn, *args):
    """Run blocking work off the loop without waiting for it; errors are logged (fn catches its own)."""
    async def go():
        try:
            await asyncio.to_thread(fn, *args)
        except Exception as caught:  # noqa: BLE001
            log(f"{getattr(fn, '__name__', fn)}: {caught}")

    shared.CTX.spawn_task(go())


def on_answered(req):
    """An answered request. question and permission need nothing more: the pane's hook picks the answer up.
    build, feedback and design are executed here, off the loop; a request they create is presented by the next scan."""
    if req["kind"] in ("build", "feedback", "design"):
        background(decisions.execute, req["id"], BOOT)


# -- typed replies --------------------------------------------------------------------------------

async def on_dispatch(event=None, gateway=None, **kw):
    """pre_gateway_dispatch: route a typed reply to a muster prompt; None lets Hermes handle the message."""
    try:
        src = event.source
        if (CHANNEL is None or getattr(src.platform, "value", src.platform) != CHANNEL.name
                or not (event.text or "").strip()):
            return None
        shared.ensure_config()
        return await CHANNEL.reply(event)
    except Exception as caught:  # noqa: BLE001 - never break normal message handling
        log(f"dispatch: {caught}")
        return None
