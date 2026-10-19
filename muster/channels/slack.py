"""slack: muster's Slack channel (docs/plans/2026-10-17-channels-design.md).

Hermes's Slack adapter registers its own clarify and approval listeners before any plugin's, and Bolt runs only the
first match, so a plugin cannot narrow who answers Hermes's own Slack prompts (their check is Hermes's allowlist:
"may talk to the agent", not "may approve this merge"). So muster does not use send_clarify or send_exec_approval
here. It posts its own Block Kit messages with muster-owned action ids, through the public
ctx.register_slack_action_handler, and authorizes every tap itself. Upstream ask: a plugin-first or namespace filter
for Slack interactive actions, or an `authorize` hook on send_clarify/send_exec_approval.

A tap reads the request file, so a prompt stays answerable across a gateway restart (`durable_prompts`): nothing is
presented again on boot, and nothing about a request is kept in memory. Posting uses the factory's app.client
(Bolt's AsyncWebClient, the bot token Hermes loaded); muster reads no Slack secret. Every string an agent or a human
wrote goes through shared.slack_text before a Block Kit section shows it.
"""

import asyncio
import json
import re

from .. import config, conversation, core, decisions, progress
from ..config import ConfigError
from . import shared
from .shared import log

name = "slack"
durable_prompts = True
CAP = 3000  # characters in a section's text (Slack's limit)
TEXT_MAX = 40000  # characters of a message's text Slack keeps; a card is split into sections, never cut, below it
BLOCKS_MAX = 50  # blocks in a message (Slack's limit)
LABEL_MAX = 75  # characters on a button or a checkbox (Slack's limit)
CHOICES_MAX = 25  # buttons in an actions block (Slack's limit)
MULTI_MAX = 10  # options in a checkboxes element (Slack's limit)
COMMAND_MAX = 2900  # characters of a command shown whole on a card with buttons; a longer one is answered in the pane
SHOWN_MAX = 500  # characters of a too-long command, and of a card's why, shown
GONE = ("message_not_found", "channel_not_found", "is_archived", "thread_not_found")  # the parent cannot change
PARENT = {"running": "Dispatched · waiting for the builder to start", "reopened": "Reopened for a new request",
          "done": "Finished", "archived": "Archived"}
ACTIONS = re.compile(r"^muster_(choice_\d+|text|multi|submit|allow|deny|deny_text)$")


class State:
    def __init__(self):
        self.client = self.adapter = None


T = State()


def validate(settings):
    """A channel (C…), its approvers and group chat type: Slack has no DM fallback, and Hermes's wake session for
    a thread is a group one."""
    entries = core.approver_entries(settings["notify_user_id"])
    return ([] if str(settings["notify_chat_id"]).strip() else ["notify_chat_id"]) + (
        [] if entries and all(entries) else ["notify_user_id"]) + (
        [] if settings["notify_chat_type"] == "group" else ["notify_chat_type"])


def home():
    raise ConfigError("muster: Slack needs notify_chat_id (a channel id, C…); it has no DM fallback")


def register(ctx):
    if not (hasattr(ctx, "register_platform_handler") and hasattr(ctx, "register_slack_action_handler")):
        return False
    ctx.register_platform_handler("slack", factory)
    ctx.register_slack_action_handler(ACTIONS, on_action)
    return True


def factory(app, adapter):
    """Called by the Slack adapter's connect() (gateway/platforms/base.py _wire_plugin_handlers), again with a new
    app when it reconnects."""
    config.load(shared.CTX)
    shared.configured = True
    core.prepare_env()
    T.client, T.adapter = app.client, adapter
    problems = validate(config.settings)
    if problems:  # the gateway never runs require(): without this a bad setting shows only as refused taps
        log(f"Slack settings missing or invalid: {', '.join(problems)}; muster's prompts cannot be answered")
    shared.start_scan()


def chat():
    return str(config.settings["notify_chat_id"])


def error_of(caught):
    """Slack's error code (slack_sdk's SlackApiError carries it in .response), else the exception's text."""
    response = getattr(caught, "response", None)
    try:
        return str(response.get("error") or caught)
    except AttributeError:
        return str(caught)


# -- blocks ---------------------------------------------------------------------------------------

def plain(text):
    return {"type": "plain_text", "text": text, "emoji": True}


def mrkdwn(text):
    """Agent or human text as Slack mrkdwn: escaped first, so it pings, links and formats nothing it did not mean
    (shared.slack_text), then Hermes's own Markdown conversion (adapter.format_message: headings, bold, links, code),
    the one Hermes's own replies get."""
    # A Markdown link would become <target|label>: "[x](!here)" pings, "[x](@U1)" mentions. Muster's own links are
    # built apart (headline, the detail link), so agent text gets none: the "](" is broken with a zero-width space.
    escaped = shared.slack_text(text).replace("](", "]\u200b(")
    escaped = re.sub(r"(?m)^&gt;( |$)", r">\1", escaped)  # a line's leading "> " stays a quote: it pings nothing
    convert = getattr(T.adapter, "format_message", None)
    return convert(escaped) if callable(convert) else re.sub(r"\*\*(.+?)\*\*", r"*\1*", escaped)  # muster's own bold


def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn(text)[:CAP]}}


def sections(text):
    """Text as section blocks of at most CAP characters, split at a paragraph, else a line, else a space: a long card
    or review is never cut short (Slack's 3,000-character limit is per section)."""
    out, rest = [], mrkdwn(text)
    while rest:
        at = len(rest) if len(rest) <= CAP else next(
            (i for i in (rest.rfind(sep, 0, CAP) for sep in ("\n\n", "\n", " ")) if i > 0), CAP)
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": rest[:at]}})
        rest = rest[at:].lstrip("\n ")
    return out


def body(req, n):
    """Question n's message without its inputs: what stays once it is answered. Its text in full, and a link to its
    detail message (shared.detail) when it has one."""
    q = req["questions"][n]
    tail = ("Tick any, then Submit, or type your own answer below." if q.get("multi")
            else "Pick one, or type your own answer below." if req["choices"][n] else "Type your answer below.")
    if req["kind"] == "question":
        tail += " Typing here answers the builder; a thread reply goes to the coordinator."
    tails = [] if req["kind"] in ("build", "design") or not shared.hints(req) else [tail]  # their hints say it
    out = sections(shared.render(req, n, cap=TEXT_MAX, label_max=LABEL_MAX, tail=tails, number_multi=False))
    return out + link(req)


def link(req):
    """The link to the request's detail message (shared.detail), when it has one: Slack's own permalink."""
    url = (req.get("detail") or {}).get("url")
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{url}|{shared.detail_label(req)}>"}]}] \
        if url else []


# A decision card's input: its label, and its hint (shown unless `button_hints` is off). Enter there sends it back,
# with the proposed feedback or revisions when the card has them.
TYPED = {"build": "feedback", "design": "revisions"}


def typed(req):
    """(label, hint or None) of the card's text input."""
    what = TYPED.get(req["kind"])
    if not what:
        return "Your own words", None
    if not req.get("feedback"):
        return f"Your {what}", f"Enter sends it back to the builder with these words as the {what}."
    return f"Add to the {what}", f"Enter sends it back with your words added to the {what}; start with replace: to " \
                                 "send only yours."


def blocks(req, n):
    """Question n as its own message: the text, its choices and an input for free text. Each block_id carries the
    request and the question, so a tap or a typed answer names exactly what it answers."""
    rid, q, choices = req["id"], req["questions"][n], req["choices"][n]
    multi = bool(q.get("multi"))
    if len(choices) > (MULTI_MAX if multi else CHOICES_MAX):
        raise ValueError(f"{len(choices)} choices: a Slack message shows at most {MULTI_MAX if multi else CHOICES_MAX}")
    labels = shared.buttons(req, n, label_max=LABEL_MAX, number_multi=False)
    if shared.reviewing(req, n):  # answering now skips the coordinator's review: an approve button says so
        who = config.settings.get("coordinator_name") or "Coordinator"
        labels = [shared.fit(f"{label} without {who}'s review", LABEL_MAX) if label.lower().startswith("approve")
                  else label for label in labels]
    out = body(req, n)
    if multi:
        out.append({"type": "actions", "block_id": f"mu:{rid}:{n}:multi", "elements": [
            {"type": "checkboxes", "action_id": "muster_multi",
             "options": [{"text": plain(label), "value": str(i)} for i, label in enumerate(labels)]},
            {"type": "button", "action_id": "muster_submit", "text": plain("Submit"), "value": f"{rid}|{n}"}]})
    elif choices:
        recommended = [a == req.get("recommended") for a in req.get("actions") or []]  # a build or design review's
        out.append({"type": "actions", "block_id": f"mu:{rid}:{n}:choices", "elements": [
            {"type": "button", "action_id": f"muster_choice_{i}", "text": plain(label), "value": f"{rid}|{n}|{i}",
             **({"style": "primary"} if i < len(recommended) and recommended[i] else {})}
            for i, label in enumerate(labels)]})
    label, hint = typed(req)
    out.append({"type": "input", "block_id": f"mu:{rid}:{n}:text", "dispatch_action": True, "optional": True,
                "label": plain(label), **({"hint": plain(hint)} if hint and shared.hints(req) else {}),
                "element": {"type": "plain_text_input", "action_id": "muster_text",
                            "dispatch_action_config": {"trigger_actions_on": ["on_enter_pressed"]}}})
    return out


# -- presenting -----------------------------------------------------------------------------------

async def post(blocks_, text, meta=None):
    """One message in the channel (in the run's thread with meta); returns its ts."""
    # no unfurl: a card links its full review, posted just above it, and Slack would show that review a second time
    res = await T.client.chat_postMessage(channel=chat(), text=shared.slack_text(text), blocks=blocks_,
                                          unfurl_links=False, unfurl_media=False,
                                          **({"thread_ts": meta["thread_id"]} if meta else {}))
    return str(res["ts"])


async def post_detail(req, meta):
    """The request's detail message (shared.detail), posted once, above its card; its permalink goes on the card.
    Saved before the permalink is asked, so a lost permalink never posts it twice. Returns a failure, or None."""
    text = shared.detail(req)
    if not text or (req.get("detail") or {}).get("platform") == name:
        return None
    blocks_, ts = sections(text), None
    try:
        for i in range(0, len(blocks_), BLOCKS_MAX):  # a review and a plan can pass one message's 50 blocks
            sent = await post(blocks_[i:i + BLOCKS_MAX], shared.plain(text.split("\n", 1)[0])[:300], meta)
            ts = ts or sent
    except Exception as caught:  # noqa: BLE001 - the card still goes out: a missing detail never hides the decision
        log(f"request {req['id']}: detail message: {error_of(caught)}")
        if ts is None:
            return None
    req["detail"] = {"platform": name, "id": ts}
    await asyncio.to_thread(decisions.update, req["id"], detail=req["detail"])
    try:
        url = (await T.client.chat_getPermalink(channel=chat(), message_ts=ts)).get("permalink")
    except Exception as caught:  # noqa: BLE001 - the card still says where the detail is
        log(f"permalink of {ts}: {error_of(caught)}")
        url = None
    if url:
        req["detail"] = {**req["detail"], "url": str(url)}
        await asyncio.to_thread(decisions.update, req["id"], detail=req["detail"])
    return None


async def present(req, meta, questions):
    """Each question given as its own message, after the request's detail message. A failed post keeps what was sent
    before it."""
    messages = {}
    await post_detail(req, meta)
    for n in questions:
        q = req["questions"][n]
        if len(req["choices"][n]) > (MULTI_MAX if q.get("multi") else CHOICES_MAX):
            return await too_many(req, n, meta, messages)
        try:
            messages[str(n)] = [await post(blocks(req, n), shared.fallback(req, n), meta)]
        except Exception as caught:  # noqa: BLE001 - reported as the send's failure, retried with backoff
            return {"messages": messages, "failure": error_of(caught)}
    return {"messages": messages, "failure": None}


def approval_body(req):
    """A permission card without its buttons: who asks and why, then the command verbatim. The command is a
    rich_text preformatted text element, never mrkdwn, so nothing in it (a ``` or a <!channel>) can format,
    link or ping."""
    card = req["card"]
    command = card["command"]
    if len(command) > COMMAND_MAX:
        head = (f"**Permission needed · {shared.title(req)}**\nThe command is too long for Slack ({len(command)} "
                f"characters): answer it in the pane. Its start:")
        command, scope = command[:SHOWN_MAX], []
    else:
        head = f"**Permission needed · {shared.title(req)}**\n{str(card.get('why') or '')[:SHOWN_MAX]}"
        scope = [section("Allow once lets the builder run this one command, once. Muster can't confirm it ran.")]
    return [section(head), {"type": "rich_text", "elements": [
        {"type": "rich_text_preformatted", "elements": [{"type": "text", "text": command}]}]}, *scope]


async def present_approval(req, meta):
    """Allow once / Deny buttons and a "Deny with a message" input (no session or permanent tiers: learnings #17).
    Hermes's approvals.timeout does not apply here: the bridge's own deadline denies an unanswered prompt. A command
    too long to show whole gets a card with no buttons, and the request goes to the pane (stale): nothing is approved
    unseen, and nothing retries forever."""
    rid, kept = req["id"], approval_body(req)
    text = f"{shared.title(req)}: permission needed"
    oversized = len(req["card"]["command"]) > COMMAND_MAX
    blocks_ = kept if oversized else [*kept, {"type": "actions", "block_id": f"mu:{rid}:approval", "elements": [
        {"type": "button", "action_id": "muster_allow", "text": plain("Allow once"), "style": "primary", "value": rid},
        {"type": "button", "action_id": "muster_deny", "text": plain("Deny"), "style": "danger", "value": rid}]},
        {"type": "input", "block_id": f"mu:{rid}:deny", "dispatch_action": True, "optional": True,
         "label": plain("Deny with a message"),
         "element": {"type": "plain_text_input", "action_id": "muster_deny_text",
                     "dispatch_action_config": {"trigger_actions_on": ["on_enter_pressed"]}}}]
    try:
        ts = await post(blocks_, text, meta)
    except Exception as caught:  # noqa: BLE001 - retried with backoff
        return {"messages": {}, "failure": error_of(caught)}
    if oversized:
        await asyncio.to_thread(decisions.transition, rid, ("open",), "stale",
                                outcome="Command too long for Slack: answer it in the pane")
    return {"messages": {"0": [ts]}, "failure": None}


async def outcome(req):
    """Every message of an ended request shows its outcome in place of its inputs. True when every update took."""
    presented = req.get("presented") or {}
    if presented.get("platform", "telegram") != name:
        return True  # another platform's messages: not ours to edit
    line = req.get("outcome") or req["status"]
    ok = True
    for n, ids in [*(presented.get("messages") or {}).items(), *(presented.get("redirected") or {}).items()]:
        if req["kind"] in shared.DECISIONS:  # the outcome is the headline; the request text goes
            kept = [*sections(shared.settled(req)), *link(req)]
        else:
            kept = [*(approval_body(req) if req["kind"] == "permission" else body(req, int(n))), context(line)]
        for ts in ids:
            ok = await update(ts, kept, line) and ok
    return ok


async def too_many(req, n, meta, messages):
    """A question Slack cannot show whole: a card saying so, and the request goes to the pane (stale), as a command
    too long for Slack does. Nothing retries forever."""
    try:
        messages[str(n)] = [await post([section(
            f"{shared.title(req)} has a question with too many options for Slack ({len(req['choices'][n])}): "
            f"answer it in the pane")], f"{shared.title(req)}: answer in the pane", meta)]
    except Exception as caught:  # noqa: BLE001 - retried with backoff
        return {"messages": messages, "failure": error_of(caught)}
    await asyncio.to_thread(decisions.transition, req["id"], ("open",), "stale",
                            outcome="Too many options for Slack: answer it in the pane")
    return {"messages": messages, "failure": None}


async def edit(chat_, mid, text):
    """A message's text in place of its blocks (its buttons and inputs go). True when Slack took it."""
    return await update(mid, [section(text)], text)


def release(rid):
    """Nothing in memory: a prompt is answered from the request file."""


def release_ended(live):
    pass


def track(req):
    pass


async def reply(event):
    """Thread replies are the coordinator's conversation: free text has its own input on each prompt."""
    return None


# -- taps -----------------------------------------------------------------------------------------

async def on_action(ack, body_, action):
    """Every muster_* action: acked first (Slack's 3 s)."""
    await ack()
    shared.CTX.spawn_task(handle(body_, action))


BLOCK = re.compile(r"mu:([0-9a-f]{16}):(?:(\d+):(choices|text|multi)|(approval|deny))")
KINDS = {"choices": r"muster_choice_\d+", "text": "muster_text", "multi": "muster_multi|muster_submit",
         "approval": "muster_allow|muster_deny", "deny": "muster_deny_text"}  # the ids each block may carry


async def handle(body_, action):
    """A tap or a typed answer. Authorized before any request is read, so nothing about a request reaches a
    refused user: only an approver, in the configured channel. The block_id binds the tap to its request (only the
    bot posts muster blocks, and only where destination put them), so there is no thread or workspace check: user
    ids are unique across workspaces, and a check on an unverified payload field would refuse every tap if its
    shape differed. Any error in the check refuses (fail closed)."""
    try:
        block, action_id = str(action.get("block_id") or ""), str(action.get("action_id") or "")
        found = BLOCK.fullmatch(block)
        kind = found and (found.group(3) or found.group(4))
        if not found or not re.fullmatch(KINDS[kind], action_id):
            return await refuse(body_, f"unknown block {block!r} or action {action_id!r}")
        shared.ensure_config()
        user, channel = (body_.get("user") or {}).get("id"), (body_.get("channel") or {}).get("id")
        if str(channel) != chat() or str(user) not in core.approvers():
            return await refuse(body_, "not an approver in the notify channel")
        rid = found.group(1)
        req = await asyncio.to_thread(decisions.load, rid)
    except FileNotFoundError:
        return await refuse(body_, "no such request")
    except Exception as caught:  # noqa: BLE001 - fail closed
        return await refuse(body_, f"check failed: {caught}")
    if kind in ("approval", "deny"):
        return await approve(body_, action, req)
    n = int(found.group(2))
    if action_id == "muster_multi":
        return  # a tick; the answer is the Submit
    try:
        value = await choice(body_, action, req, n, block, action_id)
    except (IndexError, KeyError, TypeError, ValueError) as caught:  # a question or option the request lacks
        return await refuse(body_, f"no such question or option: {caught}")
    if value is None:
        return
    if req["status"] != "open":
        part = (req.get("partial") or {}).get(str(n))
        return await notice(body_, f"Already answered: {part[0]}" if part else ended(req))
    req, result, part = await shared.answer(rid, n, value)
    if result == "closed":
        return await notice(body_, ended(req))
    if result == "duplicate":
        await notice(body_, f"Already answered: {part[0]}")
    await settled(body_, req, n, body(req, n), f"Answered: {part[0]}" if req["kind"] == "question"
                  else f"Recorded: {part[0]}")


async def choice(body_, action, req, n, block, action_id):
    """The answer a tap or typed text gives question n, or None when it gives none yet."""
    if action_id == "muster_submit":
        ticked = (((body_.get("state") or {}).get("values") or {}).get(block) or {}).get("muster_multi") or {}
        chosen = sorted(int(o["value"]) for o in ticked.get("selected_options") or [])
        if not chosen:
            await notice(body_, "Tick at least one")
            return None
        return json.dumps([req["choices"][n][i] for i in chosen])
    if action_id == "muster_text":
        return str(action.get("value") or "").strip() or None
    return req["choices"][n][int(action_id.rsplit("_", 1)[1])]


async def approve(body_, action, req):
    """Allow once, Deny (with the deny input's unsubmitted text, if any), or Enter in the deny input."""
    rid, action_id = req["id"], action.get("action_id")
    if action_id == "muster_allow":
        answer = {"decision": "allow"}
    else:
        typed = action.get("value") if action_id == "muster_deny_text" else (
            (((body_.get("state") or {}).get("values") or {}).get(f"mu:{rid}:deny") or {}).get("muster_deny_text")
            or {}).get("value")
        typed = str(typed or "").strip()
        answer = {"decision": "deny", **({"message": typed} if typed else {})}
    if req["status"] != "open":
        return await notice(body_, ended(req))
    req, ok = await shared.permission(rid, answer)
    if not ok:
        return await notice(body_, ended(req))
    line = "Allow once recorded · sending to the builder" if answer["decision"] == "allow" else (
        f"Deny recorded · sending to the builder: {answer['message']}" if answer.get("message")
        else "Deny recorded · sending to the builder")
    await settled(body_, req, 0, approval_body(req), line)


def ended(req):
    return req.get("outcome") or "This decision already ended. Nothing was changed."


async def notice(body_, text):
    """A line only the tapper sees."""
    user, channel = (body_.get("user") or {}).get("id"), (body_.get("channel") or {}).get("id")
    try:
        await T.client.chat_postEphemeral(channel=channel, user=user, text=text)
    except Exception as caught:  # noqa: BLE001
        log(f"notice to {user}: {error_of(caught)}")


def context(line):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": shared.slack_text(line)[:CAP]}]}


async def update(ts, blocks_, text):
    """Replace a message's blocks (its buttons and inputs go). True when Slack took it."""
    try:
        await T.client.chat_update(channel=chat(), ts=ts, text=shared.slack_text(text), blocks=blocks_)
        return True
    except Exception as caught:  # noqa: BLE001
        log(f"update {ts}: {error_of(caught)}")
        return False


async def settled(body_, req, n, kept, line):
    """The tapped message, and every saved message of the same question, show `line` instead of their inputs."""
    tapped = ((body_.get("container") or {}).get("message_ts"))
    saved = ((req.get("presented") or {}).get("messages") or {}).get(str(n)) or []
    for ts in dict.fromkeys([*saved, *([str(tapped)] if tapped else [])]):
        await update(ts, [*kept, context(line)], line)


async def refuse(body_, why):
    user, channel = (body_.get("user") or {}).get("id"), (body_.get("channel") or {}).get("id")
    log(f"refused a Slack tap from user {user} (team {(body_.get('team') or {}).get('id')}) in {channel}: {why}")
    try:
        await T.client.chat_postEphemeral(channel=channel, user=user, text="Not authorized")
    except Exception as caught:  # noqa: BLE001
        log(f"refusal notice: {error_of(caught)}")


# -- run threads (notify_topics) ------------------------------------------------------------------
#
# One thread per run: the bot posts the parent once (its text names the run and its ledger id), and the run's
# messages go under it. A thread cannot close, so "closed" is the parent marked done, and a request made after the
# close is posted in the thread anyway. muster deletes nothing on Slack: retiring marks the parent archived.
# Known limit: a parent whose post reply was lost stays "⏳ running" (Slack sends no created notice to adopt it).

def mine(ref):
    return bool(ref) and ref.get("platform", "telegram") == name


async def destination(req):
    """(ready, send metadata) for a request of this run: its thread; the channel itself without one (or with
    another platform's conversation). A closed thread takes only a request made after its close."""
    ledger = req["ledger"]
    ref = await asyncio.to_thread(conversation.load, ledger)
    if not mine(ref):
        return True, None
    state = ref.get("state")
    if state in ("retiring", "deleted"):
        await asyncio.to_thread(shared.stale_requests, ledger)
        return False, None
    if state == "fallback":
        return True, None
    if state == "closed":
        if req.get("created_at", 0) <= (ref.get("closed_at") or float("inf")):
            return False, None  # older than the close: a replay, a restart, a saved approval
        archive = await shared.archived(ledger)  # a fresh read: nothing reopens an archived run's thread
        if archive is not False:
            if archive:
                await shared.retire(ledger)
            return False, None
        ref, ok = await asyncio.to_thread(conversation.swap, ledger, ("closed",), state="open", closed_at=None,
                                          closed_by=None, parent_text="reopened")
        if not ok:
            return False, None
        log(f"{ledger}: thread {ref['thread_id']} in use again for request {req['id']}, made after its close")
        await parent(ref)  # cosmetic: retried by the steps, never holds the prompt
        return True, {"thread_id": ref["thread_id"]}
    if state != "open":
        return False, None  # being made, or being closed
    return True, {"thread_id": ref["thread_id"]}


def headline(ledger, state, name=None):
    """A run as people scan the channel: its short title in bold, then `state`, then its issue as a native link
    (mrkdwn). The title and state are escaped (shared.slack_text); the link comes from progress.identity."""
    title, url, label = progress.identity(ledger, name)
    tail = f"<{url}|{label}>" if url else shared.slack_text(label) if label != title else ""
    return f"*{shared.slack_text(title)}*\n{shared.slack_text(state)}" + (f" · {tail}" if tail else "")


def parent_line(ref, status, line=None):
    """The thread parent: the run and its state. While running, the status message's line (muster.progress), so every
    parent write keeps it; a final line never shows under "running" (a reopened thread is not finished). Done shows
    the final line when it is a finish ("Finished · PR #9 merged")."""
    if line is None and status in ("running", "done"):
        saved = progress.load(ref["ledger"])
        text = str(saved.get("text") or "")
        if status == "running" and not saved.get("final"):
            line = text or None
        elif status == "done" and saved.get("final") and saved.get("key") == "finished":
            line = text
    return headline(ref["ledger"], line or PARENT[status], ref.get("name"))


async def show(ledger, line, saved):
    """The run's status line (muster.progress, the channel contract): on its thread's parent, the run-status message
    it already has; without a thread, one channel message posted once, then edited."""
    if T.client is None:
        return None
    ref = await asyncio.to_thread(conversation.load, ledger)
    state = ref.get("state") if mine(ref) else None
    if state in ("pending", "creating", "retiring", "deleted"):
        return None
    if state in ("open", "closing", "closed"):
        thread = ref["thread_id"]
        # Closing or closed, or reopened after the work resolved: the lifecycle owns the parent; nothing to add.
        if state == "open" and not ref.get("resolved_at"):
            try:
                await T.client.chat_update(channel=ref["chat_id"], ts=thread,
                                           text=parent_line(ref, "running", line))
            except Exception as caught:  # noqa: BLE001 - an update is safe to repeat
                if error_of(caught) not in GONE:  # a parent gone by hand: nothing left to edit
                    log(f"{ledger}: status line on the thread parent: {error_of(caught)}")
                    return False
        return {"mid": thread, "thread": thread}
    text = await asyncio.to_thread(headline, ledger, line, ref and ref.get("name"))
    if saved.get("mid") and saved.get("thread") is None:
        try:
            await T.client.chat_update(channel=chat(), ts=saved["mid"], text=text)
        except Exception as caught:  # noqa: BLE001
            log(f"{ledger}: status message: {error_of(caught)}")
            return False
        return {"mid": saved["mid"], "thread": None}
    try:
        res = await T.client.chat_postMessage(channel=chat(), text=text)
    except Exception as caught:
        if getattr(caught, "response", None) is None:
            raise  # no answer from Slack: the post may have landed (the gateway never repeats it)
        log(f"{ledger}: status message: {error_of(caught)}")
        return False
    return {"mid": res.get("ts"), "thread": None}


async def parent(ref):
    """Bring the parent's text to `parent_text`; kept for a later step when Slack does not take it."""
    try:
        await T.client.chat_update(channel=ref["chat_id"], ts=ref["thread_id"],
                                   text=parent_line(ref, ref["parent_text"]))
    except Exception as caught:  # noqa: BLE001
        if error_of(caught) not in GONE:
            log(f"{ref['ledger']}: thread parent: {error_of(caught)}")
            return
    await asyncio.to_thread(conversation.update, ref["ledger"], parent_text=None)


async def steps():
    """Once per scan, before the requests: each of this platform's run threads one step further."""
    if T.client is None:
        return
    for ref in await asyncio.to_thread(conversation.active):
        if not mine(ref):
            continue
        try:
            await step(ref)
        except Exception as caught:  # noqa: BLE001 - one run's thread must not stop the others
            log(f"thread {ref.get('ledger')}: {caught}")


async def step(ref):
    ledger, state = ref["ledger"], ref.get("state")
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
        await retire(ref)
    elif state in ("pending", "creating"):
        await create(ref)
    elif state == "fallback" and not ref.get("noticed"):
        await T.client.chat_postMessage(channel=ref["chat_id"], text=shared.slack_text(
            f"Thread not created for {ref.get('name')}: {ref.get('why')}. This run stays in the channel."))
        await asyncio.to_thread(conversation.update, ledger, noticed=True)
    elif state == "closing" and shared.now() >= ref.get("retry_at", 0):
        await close(ref)
    elif state == "closed" and not ref.get("closed_at"):
        await asyncio.to_thread(conversation.swap, ledger, ("closed",), closed_at=shared.now())
    elif state == "open" and ref.get("parent_text"):
        await parent(ref)


async def create(ref):
    """The parent, posted at most once: `asked` is saved before the call, so a post whose result was lost (an
    error after Slack posted it, or a restart mid-call) falls back to the channel instead of posting a second."""
    ledger = ref["ledger"]
    if ref.get("asked"):
        await asyncio.to_thread(conversation.swap, ledger, ("creating",), state="fallback", noticed=False,
                                why="the thread's first message was not confirmed; not retried, so no second is made")
        return
    gone = await shared.archived(ledger)  # a fresh read: no thread is made for an archived run
    if gone is not False:
        if gone:
            await shared.retire(ledger)
        return
    ref, ok = await asyncio.to_thread(conversation.swap, ledger, ("pending", "creating"), state="creating",
                                      attempts=ref.get("attempts", 0) + 1, since=shared.now(), asked=True)
    if not ok:
        return
    try:
        res = await T.client.chat_postMessage(channel=ref["chat_id"],
                                              text=parent_line(ref, "running"))
        ts, why = res.get("ts"), "Slack returned no message ts"
    except Exception as caught:  # noqa: BLE001
        ts, why = None, error_of(caught)
    if ts:
        _, ok = await asyncio.to_thread(conversation.swap, ledger, ("creating",), state="open", thread_id=str(ts))
        if ok:
            log(f"{ledger}: thread {ts} open")
    else:
        await asyncio.to_thread(conversation.swap, ledger, ("creating",), state="fallback", noticed=False, why=why)


async def close(ref):
    """The run's work is resolved: the parent says Finished (no note in the thread: the parent is what people scan).
    closed only once Slack took the update, or the parent is gone."""
    ledger, thread = ref["ledger"], ref["thread_id"]
    try:
        await T.client.chat_update(channel=ref["chat_id"], ts=thread, text=parent_line(ref, "done"))
    except Exception as caught:  # noqa: BLE001
        if error_of(caught) not in GONE:
            await shared.backoff(ref, f"marking thread {thread} done: {error_of(caught)}")
            return
    await asyncio.to_thread(conversation.swap, ledger, ("closing",), state="closed", why="", retry_at=0,
                            retry_delay=0, closed_at=shared.now(), closed_by="muster")


async def retire(ref):
    """The run is archived: its open prompts end, the parent says archived, and its cards' subscriptions go."""
    ledger, thread = ref["ledger"], ref.get("thread_id")
    if shared.now() < ref.get("retry_at", 0):
        return
    await asyncio.to_thread(shared.stale_requests, ledger)
    if thread:
        try:
            await T.client.chat_update(channel=ref["chat_id"], ts=thread,
                                       text=parent_line(ref, "archived"))
        except Exception as caught:  # noqa: BLE001
            if error_of(caught) not in GONE:
                await shared.backoff(ref, f"marking thread {thread} archived: {error_of(caught)}")
                return
    ref, ok = await asyncio.to_thread(conversation.swap, ledger, ("retiring",), state="deleted",
                                      deleted_at=shared.now(), why="", retry_at=0, retry_delay=0, unsubscribed=False)
    if ok:
        log(f"{ledger}: thread {thread} archived")
        await shared.unsubscribe(ref)
