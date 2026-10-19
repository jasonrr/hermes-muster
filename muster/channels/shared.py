"""shared: the channel-neutral helpers both channels and muster.gateway import (a channel never imports gateway).

The hub (`answer`, `permission`, `start_scan`) is assigned by muster.gateway: a channel hands an answer to the
gateway through it.
"""

import asyncio
import time

from .. import config, conversation, core, decisions, events, progress

CTX = None  # the Hermes plugin context; set by gateway.setup
configured = False
SCAN_EVERY = 2  # s between scans
CHECK_EVERY = 30  # s between board reads of an open run's ledger
CLOSED_EVERY = 300  # s between board reads of a closed run's ledger
RETRY_MAX = 3600  # s, the longest wait between retries of a conversation close or delete the platform did not confirm
LIVE = ("pending", "creating", "open", "closing", "closed", "fallback")  # every state a run can retire from
now, clock = time.time, time.monotonic  # conversation timing only; tests patch these
checked = {}  # ledger -> clock() of the last board read of its ledger card

answer = permission = start_scan = None  # the hub: set by muster.gateway


def log(line):
    core.log("gateway", line)


def ensure_config():
    global configured
    if not configured:
        config.load(CTX)
        configured = True


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


# -- text -----------------------------------------------------------------------------------------

def title(req):
    """The run's short title (progress.identity): what people call the task, no repo or card id."""
    run = req.get("run") or {}
    return progress.identity(req["ledger"], run.get("title") or (
        f"Issue #{run['issue']}" if run.get("issue") else run.get("branch")))[0]


def plain(text):
    """Text without muster's own emphasis markers (`**`), for a surface that shows them literally."""
    return str(text).replace("**", "")


def fallback(req, n):
    """A message's notification and screen-reader text: its first lines, never a whole review."""
    first = plain(req["questions"][n]["text"]).strip().split("\n\n")[0]
    if decisions.approval_slot(req) == n:
        first = plain(lead(req, n)).split("\n")[0]
    return (first if req["kind"] != "question" or decisions.approval_slot(req) == n else f"{title(req)}: {first}")[:300]


DETAIL_OVER = 3000  # characters of a review or send-back past which its whole text also gets its own message


def detail(req):
    """The whole text behind a decision card, posted once as its own message just above it: the review (with the
    plan's full text, for a plan), or a send-back too long for one message. None when the card itself holds it all."""
    kind, review, run = req["kind"], req.get("review") or "", req.get("run") or {}
    if kind in ("build", "design") and review and (req.get("summary") or len(review) > DETAIL_OVER):
        who = config.settings.get("coordinator_name") or "Coordinator"
        if kind == "build":
            return f"**{who}'s full review · {progress.pr_name(req.get('pr'))} · {title(req)}**\n\n{review}"
        n = (req.get("proposal") or {}).get("version")
        try:
            plan = (events.proposals(req["ledger"]) / f"v{n}.md").read_text().strip()
        except OSError:
            plan = ""
        return (f"**{who}'s full review · Design v{n} · {title(req)}**\n\n{review}"
                + (f"\n\n**Design v{n}, full text**\n\n{plan}" if plan else ""))
    if kind == "feedback" and len(req.get("feedback") or "") > DETAIL_OVER:
        return f"**Feedback, full text · {progress.pr_name(req.get('pr'))} · {title(req)}**\n\n{req['feedback']}"
    return None


def detail_label(req):
    return {"design": "Full review and design", "build": "Full review"}.get(req["kind"], "Full text")


def reviewing(req, n):
    """True for a builder's Approval question (decisions.approval_slot) whose design version still waits for the
    coordinator's review: answering it skips that review."""
    if decisions.approval_slot(req) != n:
        return False
    m = progress.mtimes(req["ledger"])
    return m.get("proposal", 0) > m.get("recommended", 0)


def lead(req, n):
    """The lines a question message opens with: the task, and for a design approval the design's state. A build, design
    or feedback request's text opens with its own (decisions)."""
    if req["kind"] != "question":
        return ""
    count = len(req["questions"])
    if decisions.approval_slot(req) == n:
        plan = f"Design v{(req.get('proposal') or {}).get('version')}"
        if reviewing(req, n):
            who = config.settings.get("coordinator_name") or "Coordinator"
            return (f"**{plan} ready · {title(req)}**\n{who} is reviewing. No action needed.\n\n"
                    f"To decide without {who}'s review, answer the builder's question:")
        return f"**{plan} · {title(req)}**\nNeeds your decision."
    return f"**{title(req)}**" + (f" · question {n + 1} of {count}" if count > 1 else "")


def render(req, n, *, cap, label_max, tail, number_multi=True):
    """Question n's message text, at most `cap` characters; `tail` is the channel's hint lines. The text names only
    what the buttons (see `buttons`) lack."""
    q = req["questions"][n]
    head = [h for h in [lead(req, n)] if h]
    tail = list(tail)
    shown = buttons(req, n, label_max=label_max, number_multi=number_multi)

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
    if len(text) > cap:
        tail.append("Some option details did not fit in this message.")
        text = build(False, q["text"])
    if len(text) > cap:
        room = cap - len(build(False, ""))
        text = build(False, q["text"][:max(room - 1, 0)] + "…")
    return text[:cap]


def buttons(req, n, *, label_max, number_multi):
    """Each choice's button text: its label cut to label_max, numbered when several may be picked and the typed
    answer names the numbers (number_multi), or when plain labels would leave a button blank or two alike after the
    cut."""
    choices = req["choices"][n]
    plain = [fit(c, label_max) for c in choices]
    if (number_multi and req["questions"][n].get("multi")) or len(set(plain)) < len(plain) or not all(
            p.strip() for p in plain):
        return [fit(f"{i + 1}. {c}", label_max) for i, c in enumerate(choices)]
    return plain


def fit(s, label_max):
    # ponytail: cuts by code point, so a long emoji sequence can split; cut by grapheme if labels carry them
    return s if len(s) <= label_max else s[:label_max - 1] + "…"


def slack_text(s):
    """Slack mrkdwn shows `<…>` as a mention or a link and `&…;` as an entity: escaped, every agent- or
    human-written string reads as written (Slack's "Escaping text")."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# -- conversation lifecycle guards ----------------------------------------------------------------

async def archived(ledger):
    """True when the run's ledger card reads archived on the board, False for any other status, None when the
    board cannot be read. Only an explicit "archived" retires a run: a card missing from the board (or a gateway
    pointed at another board) must never delete a topic."""
    checked[ledger] = clock()
    try:
        return await asyncio.to_thread(events.status, ledger) == "archived"
    except Exception as caught:  # noqa: BLE001
        log(f"{ledger}: board status unreadable: {caught}")
        return None


async def retire(ledger):
    _, ok = await asyncio.to_thread(conversation.swap, ledger, LIVE, state="retiring", retry_at=0, retry_delay=0)
    if ok:
        log(f"{ledger}: ledger archived; its topic is deleted and never made again")


def stale_requests(ledger):
    """An archived run's open requests end here, so no stale prompt brings its topic back."""
    for req in decisions.for_ledger(ledger):
        if req["status"] == "open":
            decisions.transition(req["id"], ("open",), "stale", outcome="The run is archived")


async def backoff(ref, why):
    """A conversation step the platform did not confirm: the state stays, tried again later."""
    delay = min(max(ref.get("retry_delay", 0) * 2, SCAN_EVERY), RETRY_MAX)
    log(f"{ref['ledger']}: {why}; retry in {delay} s")
    await asyncio.to_thread(conversation.update, ref["ledger"], why=why, retry_delay=delay, retry_at=now() + delay)


def unsubscribe_cards(ref):
    """Remove each of an archived run's card subscriptions at its conversation, so no stale one pings or wakes the
    coordinator there. True when none is left ("no such subscription" and a card gone from the board count)."""
    thread, left = ref.get("thread_id"), []
    for card in ref.get("cards", []):
        try:
            core.kanban("notify-unsubscribe", card, "--platform", ref.get("platform", "telegram"), "--chat-id",
                        str(ref["chat_id"]), *(["--thread-id", str(thread)] if thread else []))
        except core.CommandError as caught:
            if "no such" not in str(caught):
                log(f"{ref['ledger']}: unsubscribing {card}: {caught}")
                left.append(card)
    return not left


async def unsubscribe(ref):
    if now() < ref.get("retry_at", 0):
        return
    if await asyncio.to_thread(unsubscribe_cards, ref):
        await asyncio.to_thread(conversation.update, ref["ledger"], unsubscribed=True, retry_at=0, retry_delay=0)
    else:
        await backoff(ref, "card subscriptions not removed")
