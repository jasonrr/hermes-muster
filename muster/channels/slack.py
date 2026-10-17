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

import re

from .. import config, core
from ..config import ConfigError
from . import shared
from .shared import log

name = "slack"
durable_prompts = True
CAP = 3000  # characters in a section's text (Slack's limit)
LABEL_MAX = 75  # characters on a button or a checkbox (Slack's limit)
CHOICES_MAX = 25  # buttons in an actions block (Slack's limit)
MULTI_MAX = 10  # options in a checkboxes element (Slack's limit)
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
        try:
            messages[str(n)] = [await post(blocks(req, n), f"{shared.title(req)}: {req['questions'][n]['text']}", meta)]
        except Exception as caught:  # noqa: BLE001 - reported as the send's failure, retried with backoff
            return {"messages": messages, "failure": error_of(caught)}
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


async def handle(body_, action):
    await refuse(body_, "not wired yet")


async def refuse(body_, why):
    user, channel = (body_.get("user") or {}).get("id"), (body_.get("channel") or {}).get("id")
    log(f"refused a Slack tap from user {user} (team {(body_.get('team') or {}).get('id')}) in {channel}: {why}")
    try:
        await T.client.chat_postEphemeral(channel=channel, user=user, text="Not authorized")
    except Exception as caught:  # noqa: BLE001
        log(f"refusal notice: {error_of(caught)}")
