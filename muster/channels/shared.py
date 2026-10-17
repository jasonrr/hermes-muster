"""shared: the channel-neutral helpers both channels and muster.gateway import (a channel never imports gateway).

The hub (`answer`, `permission`, `start_scan`) is assigned by muster.gateway: a channel hands an answer to the
gateway through it.
"""

import asyncio
import time

from .. import config, conversation, core, decisions, events

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
    run = req.get("run") or {}
    return f"{run['repo']} #{run['issue']}" if run.get("repo") and run.get("issue") else run.get("branch") or req["ledger"]


def render(req, n, *, cap, label_max, tail, number_multi=True):
    """Question n's message text, at most `cap` characters; `tail` is the channel's hint lines. The text names only
    what the buttons (see `buttons`) lack."""
    q = req["questions"][n]
    card = req.get("wait") or req["ledger"]
    head = [title(req)]
    tail = list(tail)
    if req.get("proposal"):
        p = req["proposal"]
        tail.append(f"Proposal v{p.get('version')} {str(p.get('sha', ''))[:8]} (full text on ledger {req['ledger']})")
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
        tail.append(f"Full options on wait card {card}")
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

    def fit(s):
        # ponytail: cuts by code point, so a long emoji sequence can split; cut by grapheme if labels carry them
        return s if len(s) <= label_max else s[:label_max - 1] + "…"

    plain = [fit(c) for c in choices]
    if (number_multi and req["questions"][n].get("multi")) or len(set(plain)) < len(plain) or not all(
            p.strip() for p in plain):
        return [fit(f"{i + 1}. {c}") for i, c in enumerate(choices)]
    return plain


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
    """A topic close or delete Telegram did not confirm: the state stays, tried again later."""
    delay = min(max(ref.get("retry_delay", 0) * 2, SCAN_EVERY), RETRY_MAX)
    log(f"{ref['ledger']}: {why}; retry in {delay} s")
    await asyncio.to_thread(conversation.update, ref["ledger"], why=why, retry_delay=delay, retry_at=now() + delay)
