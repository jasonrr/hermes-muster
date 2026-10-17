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

from .. import config, conversation, core, decisions
from ..config import ConfigError
from . import shared
from .shared import log

name = "slack"
durable_prompts = True
CAP = 3000  # characters in a section's text (Slack's limit)
LABEL_MAX = 75  # characters on a button or a checkbox (Slack's limit)
CHOICES_MAX = 25  # buttons in an actions block (Slack's limit)
MULTI_MAX = 10  # options in a checkboxes element (Slack's limit)
COMMAND_MAX = 2900  # characters of a command shown whole on a card with buttons; a longer one is answered in the pane
SHOWN_MAX = 500  # characters of a too-long command, and of a card's why, shown
GONE = ("message_not_found", "channel_not_found", "is_archived", "thread_not_found")  # the parent cannot change
PARENT = {"running": "⏳ running", "done": "✅ done", "archived": "archived"}
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


def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": shared.slack_text(text)[:CAP]}}


def body(req, n):
    """Question n's message without its inputs: what stays once it is answered."""
    q = req["questions"][n]
    tail = ("Tick any, then Submit, or type your own answer below." if q.get("multi")
            else "Pick one, or type your own answer below." if req["choices"][n] else "Type your answer below.")
    return [section(shared.render(req, n, cap=CAP, label_max=LABEL_MAX, tail=[tail], number_multi=False))]


def blocks(req, n):
    """Question n as its own message: the text, its choices and an input for free text. Each block_id carries the
    request and the question, so a tap or a typed answer names exactly what it answers."""
    rid, q, choices = req["id"], req["questions"][n], req["choices"][n]
    multi = bool(q.get("multi"))
    if len(choices) > (MULTI_MAX if multi else CHOICES_MAX):
        raise ValueError(f"{len(choices)} choices: a Slack message shows at most {MULTI_MAX if multi else CHOICES_MAX}")
    labels = shared.buttons(req, n, label_max=LABEL_MAX, number_multi=False)
    out = body(req, n)
    if multi:
        out.append({"type": "actions", "block_id": f"mu:{rid}:{n}:multi", "elements": [
            {"type": "checkboxes", "action_id": "muster_multi",
             "options": [{"text": plain(label), "value": str(i)} for i, label in enumerate(labels)]},
            {"type": "button", "action_id": "muster_submit", "text": plain("Submit"), "value": f"{rid}|{n}"}]})
    elif choices:
        out.append({"type": "actions", "block_id": f"mu:{rid}:{n}:choices", "elements": [
            {"type": "button", "action_id": f"muster_choice_{i}", "text": plain(label), "value": f"{rid}|{n}|{i}"}
            for i, label in enumerate(labels)]})
    out.append({"type": "input", "block_id": f"mu:{rid}:{n}:text", "dispatch_action": True, "optional": True,
                "label": plain("Your own words"),
                "element": {"type": "plain_text_input", "action_id": "muster_text",
                            "dispatch_action_config": {"trigger_actions_on": ["on_enter_pressed"]}}})
    return out


# -- presenting -----------------------------------------------------------------------------------

async def post(blocks_, text, meta=None):
    """One message in the channel (in the run's thread with meta); returns its ts."""
    res = await T.client.chat_postMessage(channel=chat(), text=shared.slack_text(text), blocks=blocks_,
                                          **({"thread_ts": meta["thread_id"]} if meta else {}))
    return str(res["ts"])


async def present(req, meta, questions):
    """Each question given as its own message. A failed post keeps what was sent before it."""
    messages = {}
    for n in questions:
        q = req["questions"][n]
        if len(req["choices"][n]) > (MULTI_MAX if q.get("multi") else CHOICES_MAX):
            return await too_many(req, n, meta, messages)
        try:
            messages[str(n)] = [await post(blocks(req, n), f"{shared.title(req)}: {req['questions'][n]['text']}", meta)]
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
        head = f"{shared.title(req)} needs a permission in the pane: command too long for Slack ({len(command)} chars)"
        command = command[:SHOWN_MAX]
    else:
        head = f"{shared.title(req)} wants to run this. {str(card.get('why') or '')[:SHOWN_MAX]}"
    return [section(head), {"type": "rich_text", "elements": [
        {"type": "rich_text_preformatted", "elements": [{"type": "text", "text": command}]}]}]


async def present_approval(req, meta):
    """Allow once / Deny buttons and a "Deny with a message" input (no session or permanent tiers: learnings #17).
    Hermes's approvals.timeout does not apply here: the bridge's own deadline denies an unanswered prompt. A command
    too long to show whole gets a card with no buttons, and the request goes to the pane (stale): nothing is approved
    unseen, and nothing retries forever."""
    rid, kept = req["id"], approval_body(req)
    text = f"{shared.title(req)}: permission"
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
    for n, ids in (presented.get("messages") or {}).items():
        kept = approval_body(req) if req["kind"] == "permission" else body(req, int(n))
        for ts in ids:
            ok = await update(ts, [*kept, context(line)], line) and ok
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
    await settled(body_, req, n, body(req, n), f"Answered: {part[0]}")


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
    line = "Answered: Allow once" if answer["decision"] == "allow" else (
        f"Answered: Deny: {answer['message']}" if answer.get("message") else "Answered: Deny")
    await settled(body_, req, 0, approval_body(req), line)


def ended(req):
    return req.get("outcome") or f"This request is {req['status']}"


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
                                          closed_by=None, parent_text="running")
        if not ok:
            return False, None
        log(f"{ledger}: thread {ref['thread_id']} in use again for request {req['id']}, made after its close")
        await parent(ref)  # cosmetic: retried by the steps, never holds the prompt
        return True, {"thread_id": ref["thread_id"]}
    if state != "open":
        return False, None  # being made, or being closed
    return True, {"thread_id": ref["thread_id"]}


def parent_line(ref, status):
    return f"muster · {ref.get('name') or ref['ledger']}   {PARENT[status]}"


async def parent(ref):
    """Bring the parent's text to `parent_text`; kept for a later step when Slack does not take it."""
    try:
        await T.client.chat_update(channel=ref["chat_id"], ts=ref["thread_id"],
                                   text=shared.slack_text(parent_line(ref, ref["parent_text"])))
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
                                              text=shared.slack_text(parent_line(ref, "running")))
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
    """The run's work is resolved: a finish note in the thread (once), then the parent marked done. closed only
    once Slack took the update, or the parent is gone."""
    ledger, thread = ref["ledger"], ref["thread_id"]
    if not ref.get("said"):
        try:
            await T.client.chat_postMessage(channel=ref["chat_id"], thread_ts=thread, text=(
                "Work finished: this thread is done. History kept; the worktree is cleaned up separately."))
        except Exception as caught:  # noqa: BLE001
            log(f"{ledger}: finish note: {error_of(caught)}")
        await asyncio.to_thread(conversation.update, ledger, said=True)
    try:
        await T.client.chat_update(channel=ref["chat_id"], ts=thread, text=shared.slack_text(parent_line(ref, "done")))
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
                                       text=shared.slack_text(parent_line(ref, "archived")))
        except Exception as caught:  # noqa: BLE001
            if error_of(caught) not in GONE:
                await shared.backoff(ref, f"marking thread {thread} archived: {error_of(caught)}")
                return
    ref, ok = await asyncio.to_thread(conversation.swap, ledger, ("retiring",), state="deleted",
                                      deleted_at=shared.now(), why="", retry_at=0, retry_delay=0, unsubscribed=False)
    if ok:
        log(f"{ledger}: thread {thread} archived")
        await shared.unsubscribe(ref)
