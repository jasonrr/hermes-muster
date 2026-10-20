"""shared: the channel-neutral helpers both channels and muster.gateway import (a channel never imports gateway).

The hub (`answer`, `permission`, `start_scan`) is assigned by muster.gateway: a channel hands an answer to the
gateway through it.
"""

import asyncio
import hashlib
import re
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


DETAIL_OVER = 3000  # characters of a send-back past which its whole text also gets its own message


def detail(req):
    """The whole text of a send-back too long for one message, posted once as its own message just above its card,
    else None. A build or design card's full texts are files instead (documents)."""
    if req["kind"] == "feedback" and len(req.get("feedback") or "") > DETAIL_OVER:
        return f"**Feedback, full text · {progress.pr_name(req.get('pr'))} · {title(req)}**\n\n{req['feedback']}"
    return None


def documents(req):
    """A build or design card's full texts as Markdown files, [(file name, text)], posted once above the card: the
    coordinator's review when the card shows its summary (otherwise the card holds the whole, short review), and a
    design's proposal exactly as saved. Names carry the version, or the reviewed head, so no file passes for another.
    Raises ValueError when the saved proposal no longer has the card's pinned hash."""
    if req["kind"] not in ("build", "design"):
        return []
    slug = re.sub(r"[^a-z0-9]+", "-", title(req).lower()).strip("-")[:40].strip("-") or "run"
    if req["kind"] == "build":
        name = f"{slug}-pr-{str(req.get('pr') or '').rstrip('/').rsplit('/', 1)[-1]}-review-{req['head'][:7]}.md"
        return [(name, req["review"])] if req.get("summary") else []
    n, sha = req["proposal"]["version"], req["proposal"]["sha"]
    plan = (events.proposals(req["ledger"]) / f"v{n}.md").read_text()
    if hashlib.sha256(plan.encode()).hexdigest()[:12] != sha:
        raise ValueError(f"Design v{n} changed after it was reviewed")
    return ([(f"{slug}-design-v{n}-review.md", req["review"])] if req.get("summary") else []) + [
        (f"{slug}-design-v{n}.md", plan)]


def detail_label(req):
    if req["kind"] == "design":
        return "Full review and design" if req.get("summary") else "Full design"
    return {"build": "Full review"}.get(req["kind"], "Full text")


def caption(req):
    """The line the documents are posted under."""
    n = (req.get("proposal") or {}).get("version")
    subject = f"Design v{n}" if req["kind"] == "design" else progress.pr_name(req.get("pr"))
    return f"{detail_label(req)} · {subject} · {title(req)}"


def attach_line(req):
    """While the card is open, the line saying its documents could not be attached, else None: a missing file is never
    silent, and the decision still goes out."""
    failed = (req.get("detail") or {}).get("failed")
    if not failed or req["status"] != "open":
        return None
    who = config.settings.get("coordinator_name") or "Coordinator"
    return f"⚠️ {detail_label(req)} could not be attached ({failed}). Ask {who} in this thread for it."


DECISIONS = ("build", "design", "feedback")  # cards whose text decisions writes, outcome first once they end


def hints(req):
    """True when req's message shows its how-to text: always for a builder's question or a permission, and for a
    decision card unless the human turned `button_hints` off."""
    return req["kind"] not in DECISIONS or bool(config.settings.get("button_hints", True))


def settled(req):
    """An ended decision card: its outcome as the headline and what follows, then what it sent. The request text and
    its inputs go: nothing is left to decide."""
    line = req.get("outcome") or req["status"]
    head, _, rest = line.partition("\n")
    if not rest:  # a one-line outcome (a refusal, a stale card): the line itself, under a plain headline
        head, rest = ("⏹ Ended" if req["status"] == "stale" else "⚠️ Needs attention"), line
    n = (req.get("proposal") or {}).get("version")
    subject = f"Design v{n}" if req["kind"] == "design" else progress.pr_name(req.get("pr"))
    sent = req.get("resolved")
    return f"**{head} · {subject} · {title(req)}**\n{rest}" + (f"\n\n{decisions.quote(sent)}" if sent else "")


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
            return (f"**🔍 {plan} · {title(req)}**\n{who} is reviewing {plan.lower()}; the recommendation comes next. "
                    f"Nothing to decide yet.\n\nTo decide now without {who}'s review, answer the builder's question:")
        return f"**🟡 {plan} · {title(req)}**\nNeeds your decision."
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

    if req.get("hints") and hints(req):  # kept whole like the tail: a cap cuts the reason first
        tail = ["**What each button does**\n" + "\n".join(req["hints"]), *tail]
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
