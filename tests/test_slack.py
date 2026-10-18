"""slack: muster's own Block Kit prompts, its action handler (the only authorization a muster tap gets on Slack),
approvals and the thread lifecycle, against a fake Bolt client."""

import asyncio
import json
import time
import types

import pytest

from muster import config, core, decisions, gateway
from muster.channels import shared, slack

from tests import fake_slack as fs

CHAN, ME, OTHER = "C1", "U1", "U9"


@pytest.fixture(autouse=True)
def channel(monkeypatch):
    for key, value in {"notify_platform": "slack", "notify_chat_id": CHAN, "notify_user_id": f"{ME},U2",
                       "notify_chat_type": "group"}.items():
        monkeypatch.setitem(config.settings, key, value)
    order = []
    client = fs.Client(order)
    monkeypatch.setattr(slack, "T", slack.State())
    slack.T.client = client
    monkeypatch.setattr(gateway, "S", gateway.State())
    monkeypatch.setattr(gateway, "CHANNEL", slack)
    monkeypatch.setattr(shared, "checked", {})
    monkeypatch.setattr(shared, "configured", True)
    monkeypatch.setattr(shared, "CTX", types.SimpleNamespace(
        get_config=lambda k, d: config.settings.get(k, d),
        spawn_task=lambda coro, name=None: asyncio.get_running_loop().create_task(coro)))
    monkeypatch.setattr(decisions, "obsolete", lambda req: None)
    return types.SimpleNamespace(client=client, order=order)


def question(text="Which?", labels=("Alpha", "Beta"), multi=False):
    return {"text": text, "header": "H", "multi": multi,
            "options": [{"label": label, "description": f"about {label}"} for label in labels]}


def ask(questions=None, ledger="led1", **fields):
    questions = questions or [question()]
    return decisions.create("question", ledger, questions=questions,
                            choices=[[o["label"] for o in q["options"]] for q in questions],
                            run={"repo": "o/r", "issue": 7, "branch": "muster/7"}, wait="w1", alive=time.time(),
                            **fields)


def action_ids(block):
    if "elements" in block:
        return [e["action_id"] for e in block["elements"] if "action_id" in e]
    return [block["element"]["action_id"]] if "element" in block else []


# -- Task 6: factory, blocks, presenting ------------------------------------------------------------

def test_register_wires_one_platform_handler_and_one_action_handler():
    ctx = fs.Ctx()
    assert slack.register(ctx) is True
    assert ctx.platform_handlers == [("slack", slack.factory)]
    ((pattern, callback),) = ctx.action_handlers
    assert callback is slack.on_action
    for good in ("muster_choice_0", "muster_choice_12", "muster_text", "muster_multi", "muster_submit",
                 "muster_allow", "muster_deny", "muster_deny_text"):
        assert pattern.match(good), good
    for bad in ("hermes_clarify_choice_0", "muster_choice_", "muster_allowx", "xmuster_text"):
        assert not pattern.match(bad), bad


def test_a_hermes_without_slack_action_handlers_gets_nothing_registered():
    class Old:
        def __init__(self):
            self.calls = []

        def register_platform_handler(self, platform, factory):
            self.calls.append(platform)

    old = Old()
    assert slack.register(old) is False and old.calls == []


def test_the_factory_keeps_the_client_and_runs_one_scan(monkeypatch):
    started = []
    monkeypatch.setattr(shared, "start_scan", lambda: started.append(1))
    monkeypatch.setattr(core, "prepare_env", lambda: None)
    first, second = fs.App(), fs.App()
    adapter = object()
    slack.factory(first, adapter)
    slack.factory(second, adapter)  # a reconnect hands a new app
    assert slack.T.client is second.client and slack.T.adapter is adapter and started == [1, 1]


def test_the_factory_logs_bad_settings_once_at_connect(monkeypatch):
    monkeypatch.setattr(shared, "start_scan", lambda: None)
    monkeypatch.setattr(core, "prepare_env", lambda: None)
    monkeypatch.setattr(shared, "CTX", fs.Ctx({**config.settings, "notify_user_id": ""}))
    slack.factory(fs.App(), object())
    assert "notify_user_id" in core.log_path("gateway").read_text()


def test_choice_blocks():
    req = ask([question(labels=("Redis", "SQLite", "x" * 100))])
    rid = req["id"]
    section, actions, text = slack.blocks(req, 0)
    assert section["type"] == "section" and section["text"]["text"].startswith("o/r #7")
    assert "tap Other" not in section["text"]["text"] and "Pick one" in section["text"]["text"]
    assert actions["block_id"] == f"mu:{rid}:0:choices"
    assert [e["action_id"] for e in actions["elements"]] == ["muster_choice_0", "muster_choice_1", "muster_choice_2"]
    assert [e["value"] for e in actions["elements"]] == [f"{rid}|0|{i}" for i in range(3)]
    labels = [e["text"]["text"] for e in actions["elements"]]
    assert labels[:2] == ["Redis", "SQLite"] and len(labels[2]) == 75
    assert all(e["text"]["type"] == "plain_text" for e in actions["elements"])
    assert text["type"] == "input" and text["block_id"] == f"mu:{rid}:0:text" and text["dispatch_action"] is True
    assert text["element"]["action_id"] == "muster_text"


def test_multi_blocks():
    req = ask([question(labels=("A", "B", "C"), multi=True)])
    _, multi, _ = slack.blocks(req, 0)
    assert multi["block_id"] == f"mu:{req['id']}:0:multi"
    box, submit = multi["elements"]
    assert box["action_id"] == "muster_multi" and [o["value"] for o in box["options"]] == ["0", "1", "2"]
    assert submit["action_id"] == "muster_submit" and submit["value"] == f"{req['id']}|0"


def test_no_choices_means_no_actions_block():
    req = ask([question(labels=())])
    assert [b["type"] for b in slack.blocks(req, 0)] == ["section", "input"]


def test_too_many_choices_go_to_the_pane(channel):
    many = ask([question(labels=tuple(f"o{i}" for i in range(26)))])
    with pytest.raises(ValueError):
        slack.blocks(many, 0)
    with pytest.raises(ValueError):
        slack.blocks(ask([question(labels=tuple(f"o{i}" for i in range(11)), multi=True)]), 0)
    sent = asyncio.run(slack.present(many, None, [0]))  # to the pane, never retried forever
    (card,) = channel.client.posts
    assert sent == {"messages": {"0": ["1.000100"]}, "failure": None} and "too many options" in json.dumps(card)
    assert not [b for b in card["blocks"] if b["type"] in ("actions", "input")]
    saved = decisions.load(many["id"])
    assert saved["status"] == "stale" and saved["outcome"] == "Too many options for Slack: answer it in the pane"


def test_ids_never_repeat_within_a_block_and_blocks_never_repeat_across_questions_and_runs():
    one = ask([question(), question("Second?", multi=True)])
    two = ask([question()], ledger="led2")
    seen = []
    for req, n in ((one, 0), (one, 1), (two, 0)):
        for block in slack.blocks(req, n):
            ids = action_ids(block)
            assert len(ids) == len(set(ids))
            if "block_id" in block:
                seen.append(block["block_id"])
    assert len(seen) == len(set(seen))


def test_shown_text_is_escaped_never_a_live_mention_or_link():
    req = ask([question("<!channel> pick <https://x|here> & go", labels=("<@U2>", "B"))])
    section = slack.blocks(req, 0)[0]
    shown = section["text"]["text"]
    assert "&lt;!channel&gt; pick &lt;https://x|here&gt; &amp; go" in shown and "<!channel>" not in shown


def test_present_posts_each_question_in_the_thread_and_returns_new_ids(channel):
    req = ask([question(), question("Second?")])
    sent = asyncio.run(slack.present(req, {"thread_id": "9.0001"}, [0, 1]))
    assert sent == {"messages": {"0": ["1.000100"], "1": ["2.000100"]}, "failure": None}
    assert [p["thread_ts"] for p in channel.client.posts] == ["9.0001", "9.0001"]
    assert all(p["channel"] == CHAN and p["blocks"] for p in channel.client.posts)
    asyncio.run(slack.present(req, None, [1]))
    assert "thread_ts" not in channel.client.posts[-1]


def test_a_failed_post_keeps_what_was_sent(channel):
    req = ask([question(), question("Second?")])
    channel.client.fail["chat_postMessage"] = [None, "ratelimited"]
    sent = asyncio.run(slack.present(req, None, [0, 1]))
    assert sent["messages"] == {"0": ["1.000100"]} and "ratelimited" in sent["failure"]


def test_in_process_state_is_a_no_op_and_replies_go_to_hermes():
    slack.release("x")
    slack.release_ended({})
    slack.track({"id": "x"})
    event = types.SimpleNamespace(text="hello", source=types.SimpleNamespace(platform="slack"))
    assert asyncio.run(slack.reply(event)) is None


# -- Task 7: the action handler ---------------------------------------------------------------------

def shown(req, channel, meta=None):
    """Present the request as the gateway would, so its message ids are saved."""
    sent = asyncio.run(slack.present(req, meta, list(range(len(req["questions"])))))
    decisions.update(req["id"], presented={"boot": "b", "platform": "slack", "messages": sent["messages"]})
    channel.client.posts.clear()
    channel.order.clear()
    return {n: ids[0] for n, ids in sent["messages"].items()}


def tap(channel, req, n, action_id, value=None, user=ME, chan=CHAN, kind=None, state=None, message_ts=None):
    kind = kind or ("text" if action_id == "muster_text" else "multi" if action_id in ("muster_multi", "muster_submit")
                    else "choices")
    block = f"mu:{req['id']}:{n}:{kind}" if isinstance(n, int) else f"mu:{req['id']}:{n}"
    body, action = fs.body(user, chan, block, action_id, value, message_ts=message_ts or "1.000100", state=state)
    return answer_tap(channel, body, action)


def answer_tap(channel, body, action):
    async def go():
        ack = fs.Ack(channel.order)
        await slack.on_action(ack, body, action)
        for task in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            await task
        return ack

    return asyncio.run(go())


def refused(channel):
    return [e["text"] for e in channel.client.ephemerals] == ["Not authorized"]


def test_ack_comes_before_any_slack_call(channel):
    req = ask()
    shown(req, channel)
    tap(channel, req, 0, "muster_choice_1", f"{req['id']}|0|1")
    assert channel.order[0] == "ack" and "chat_update" in channel.order


def test_a_choice_answers_and_the_message_keeps_its_text(channel):
    req = ask()
    ts = shown(req, channel)
    tap(channel, req, 0, "muster_choice_1", f"{req['id']}|0|1", message_ts=ts["0"])
    saved = decisions.load(req["id"])
    assert saved["status"] == "answered" and saved["answer"] == {"Which?": "Beta"}
    (update,) = channel.client.updates
    assert update["ts"] == ts["0"] and update["channel"] == CHAN and update["blocks"]
    section, context = update["blocks"]
    assert section == slack.blocks(req, 0)[0] and "Answered: Beta" in json.dumps(context)
    assert not [b for b in update["blocks"] if b["type"] in ("actions", "input")]
    assert channel.client.ephemerals == []


def test_the_first_answer_of_two_gets_no_notice(channel):
    req = ask([question(), question("Second?")])
    shown(req, channel)
    tap(channel, req, 0, "muster_choice_0", f"{req['id']}|0|0")
    assert decisions.load(req["id"])["status"] == "open" and channel.client.ephemerals == []
    assert len(channel.client.updates) == 1


def test_typed_text_answers_its_question(channel):
    req = ask()
    shown(req, channel)
    tap(channel, req, 0, "muster_text", "use sqlite")
    assert decisions.load(req["id"])["answer"] == {"Which?": "use sqlite"}


def test_multi_answers_on_submit_only(channel):
    req = ask([question(labels=("A", "B", "C"), multi=True)])
    shown(req, channel)
    rid = req["id"]
    tap(channel, req, 0, "muster_multi")
    assert decisions.load(rid)["status"] == "open" and channel.client.updates == [] == channel.client.ephemerals
    tap(channel, req, 0, "muster_submit", f"{rid}|0", state={f"mu:{rid}:0:multi": {"muster_multi": {
        "type": "checkboxes", "selected_options": []}}})
    assert [e["text"] for e in channel.client.ephemerals] == ["Tick at least one"]
    tap(channel, req, 0, "muster_submit", f"{rid}|0", state={f"mu:{rid}:0:multi": {"muster_multi": {
        "type": "checkboxes", "selected_options": [{"value": "0"}, {"value": "2"}]}}})
    assert decisions.load(rid)["answer"] == {"Which?": "A, C"}


@pytest.mark.parametrize("how", ["not an approver", "another channel", "unknown block", "no such request",
                                 "check raises"])
def test_each_refusal_answers_nothing(channel, monkeypatch, how):
    req = ask()
    shown(req, channel)
    if how == "not an approver":
        tap(channel, req, 0, "muster_choice_0", user=OTHER)
    elif how == "another channel":
        tap(channel, req, 0, "muster_choice_0", chan="C2")
    elif how == "unknown block":
        body, action = fs.body(ME, CHAN, "mu:zz:0:choices", "muster_choice_0")
        answer_tap(channel, body, action)
    elif how == "no such request":
        body, action = fs.body(ME, CHAN, "mu:00000000000000aa:0:choices", "muster_choice_0")
        answer_tap(channel, body, action)
    else:
        monkeypatch.setattr(core, "approvers", lambda: 1 / 0)
        tap(channel, req, 0, "muster_choice_0")
    assert refused(channel) and channel.client.updates == []
    assert decisions.load(req["id"])["status"] == "open"
    assert "refused a Slack tap" in core.log_path("gateway").read_text()


def test_a_refused_user_learns_nothing_about_the_request(channel, monkeypatch):
    req = ask()
    shown(req, channel)
    read = []
    monkeypatch.setattr(decisions, "load", lambda rid: read.append(rid) or 1 / 0)
    tap(channel, req, 0, "muster_choice_0", user=OTHER)
    assert read == [] and refused(channel)


def test_a_double_tap_answers_once(channel):
    req = ask()
    shown(req, channel)
    tap(channel, req, 0, "muster_choice_1")
    tap(channel, req, 0, "muster_choice_0")  # a second tap, or Slack redelivering the first
    assert decisions.load(req["id"])["answer"] == {"Which?": "Beta"}
    assert [e["text"] for e in channel.client.ephemerals] == ["Already answered: Beta"]


def test_two_approvers_racing_both_see_the_stored_answer(channel):
    req = ask([question(), question("Second?")])
    shown(req, channel)
    rid = req["id"]
    a, _ = fs.body(ME, CHAN, f"mu:{rid}:0:choices", "muster_choice_0")
    b, _ = fs.body("U2", CHAN, f"mu:{rid}:0:choices", "muster_choice_1")

    async def go():
        await asyncio.gather(slack.handle(a, a["actions"][0]), slack.handle(b, b["actions"][0]))

    asyncio.run(go())
    stored = decisions.load(rid)["partial"]["0"][0]
    assert len(channel.client.updates) == 2
    assert all(f"Answered: {stored}" in json.dumps(u["blocks"]) for u in channel.client.updates)
    assert [e["text"] for e in channel.client.ephemerals] == [f"Already answered: {stored}"]


def test_after_a_restart_the_last_question_completes_the_request(channel, monkeypatch):
    req = ask([question(), question("Second?")])
    shown(req, channel)
    tap(channel, req, 0, "muster_choice_0")
    monkeypatch.setattr(slack, "T", slack.State())  # a restarted gateway, the same files
    slack.T.client = channel.client
    tap(channel, req, 1, "muster_choice_1")
    saved = decisions.load(req["id"])
    assert saved["status"] == "answered" and saved["answer"] == {"Which?": "Alpha", "Second?": "Beta"}


def test_a_retired_runs_prompt_says_its_outcome(channel):
    req = ask()
    shown(req, channel)
    decisions.transition(req["id"], ("open",), "stale", outcome="The run is archived")
    tap(channel, req, 0, "muster_choice_0")
    assert [e["text"] for e in channel.client.ephemerals] == ["The run is archived"]
    assert "answer" not in decisions.load(req["id"])


def test_two_runs_with_the_same_question_answer_only_their_own(channel):
    one, two = ask(), ask(ledger="led2")
    shown(one, channel, {"thread_id": "5.1"})
    shown(two, channel, {"thread_id": "6.1"})
    tap(channel, two, 0, "muster_choice_0")
    assert decisions.load(one["id"])["status"] == "open" and decisions.load(two["id"])["status"] == "answered"


# -- Task 8: approvals ------------------------------------------------------------------------------

def permission(command="touch x", why="Create x"):
    return decisions.create("permission", "led1", card={"command": command, "why": why},
                            run={"repo": "o/r", "issue": 7, "branch": "muster/7"}, alive=time.time())


def card_of(channel, req):
    sent = asyncio.run(slack.present_approval(req, None))
    decisions.update(req["id"], presented={"boot": "b", "platform": "slack", "messages": sent["messages"]})
    return sent, channel.client.posts[-1]


def preformatted(blocks):
    return [e["elements"][0]["text"] for b in blocks if b["type"] == "rich_text" for e in b["elements"]
            if e["type"] == "rich_text_preformatted"]


def test_the_approval_card(channel):
    req = permission()
    sent, post = card_of(channel, req)
    rid = req["id"]
    assert sent == {"messages": {"0": [post_ts(channel)]}, "failure": None}
    buttons = next(b for b in post["blocks"] if b.get("block_id") == f"mu:{rid}:approval")
    assert [e["action_id"] for e in buttons["elements"]] == ["muster_allow", "muster_deny"]
    deny = next(b for b in post["blocks"] if b.get("block_id") == f"mu:{rid}:deny")
    assert deny["type"] == "input" and deny["dispatch_action"] is True and deny["element"]["action_id"] == "muster_deny_text"
    assert preformatted(post["blocks"]) == ["touch x"]


def post_ts(channel):
    return f"{channel.client.count}.000100"


@pytest.mark.parametrize("command", ["echo ```\n*Approved by admin*", "<!channel>"])
def test_a_command_never_breaks_out_of_its_preformatted_block(channel, command):
    _, post = card_of(channel, permission(command, why="<!here> & why"))
    assert preformatted(post["blocks"]) == [command]
    sections = json.dumps([b for b in post["blocks"] if b["type"] == "section"])
    assert command not in sections and "<!here>" not in sections and "&lt;!here&gt; &amp; why" in sections


def test_a_long_why_is_cut_before_escaping(channel):
    _, post = card_of(channel, permission(why="<" * 2000))
    (head,) = [b for b in post["blocks"] if b["type"] == "section"]
    assert len(head["text"]["text"]) <= slack.CAP and "&lt;" * 500 in head["text"]["text"]


def test_a_command_too_long_for_slack_goes_to_the_pane(channel):
    req = permission("x" * 2901)
    sent, post = card_of(channel, req)
    assert not [b for b in post["blocks"] if b["type"] in ("actions", "input")]
    assert "command too long for Slack (2901 chars)" in json.dumps(post["blocks"])
    assert preformatted(post["blocks"]) == ["x" * 500]
    saved = decisions.load(req["id"])
    assert saved["status"] == "stale" and saved["outcome"] == "Command too long for Slack: answer it in the pane"
    assert sent["messages"] == {"0": [post_ts(channel)]} and sent["failure"] is None


@pytest.mark.parametrize("how, answer", [
    ("allow", {"decision": "allow"}),
    ("deny", {"decision": "deny"}),
    ("deny with unsubmitted text", {"decision": "deny", "message": "use make"}),
    ("enter in the deny input", {"decision": "deny", "message": "not now"}),
])
def test_approval_answers(channel, how, answer):
    req = permission()
    card_of(channel, req)
    rid = req["id"]
    if how == "allow":
        tap(channel, req, "approval", "muster_allow", rid)
    elif how == "deny":
        tap(channel, req, "approval", "muster_deny", rid)
    elif how.startswith("deny with"):
        tap(channel, req, "approval", "muster_deny", rid, state={f"mu:{rid}:deny": {"muster_deny_text": {
            "type": "plain_text_input", "value": " use make "}}})
    else:
        tap(channel, req, "deny", "muster_deny_text", "not now")
    saved = decisions.load(rid)
    assert saved["status"] == "answered" and saved["answer"] == answer
    assert [a["step"] for a in saved["audit"]].count("answered") == 1
    assert channel.client.updates and not [b for b in channel.client.updates[-1]["blocks"]
                                          if b["type"] in ("actions", "input")]


def test_approval_refusals(channel):
    req = permission()
    card_of(channel, req)
    tap(channel, req, "approval", "muster_allow", req["id"], user=OTHER)
    assert refused(channel) and decisions.load(req["id"])["status"] == "open"
    decisions.transition(req["id"], ("open",), "stale", outcome="Answered in the pane")
    channel.client.ephemerals.clear()
    tap(channel, req, "approval", "muster_allow", req["id"])
    assert [e["text"] for e in channel.client.ephemerals] == ["Answered in the pane"]


def test_an_ended_card_shows_its_outcome(channel):
    req = permission()
    sent, _ = card_of(channel, req)
    decisions.transition(req["id"], ("open",), "done", outcome="Allow sent to Claude")
    assert asyncio.run(slack.outcome(decisions.load(req["id"]))) is True
    (update,) = channel.client.updates
    assert update["ts"] == sent["messages"]["0"][0] and "Allow sent to Claude" in json.dumps(update["blocks"])
    assert preformatted(update["blocks"]) == ["touch x"]
    assert not [b for b in update["blocks"] if b["type"] in ("actions", "input")]


def test_an_ended_question_shows_its_outcome_on_every_message(channel):
    req = ask([question(), question("Second?")])
    shown(req, channel)
    decisions.transition(req["id"], ("open",), "stale", outcome="Answered in the pane")
    channel.client.fail["chat_update"] = ["message_not_found"]
    assert asyncio.run(slack.outcome(decisions.load(req["id"]))) is False
    assert len(channel.client.updates) == 1 and "Answered in the pane" in json.dumps(channel.client.updates[0])


# -- Task 9: the thread lifecycle -------------------------------------------------------------------

from muster import conversation  # noqa: E402

from tests import fake_hermes as fh  # noqa: E402


@pytest.fixture
def threads(channel, monkeypatch):
    monkeypatch.setitem(config.settings, "notify_topics", True)
    subs = fh.Subs()
    monkeypatch.setattr(core, "kanban", subs)
    clock = [1000.0]
    monkeypatch.setattr(shared, "now", lambda: clock[0])
    monkeypatch.setattr(shared, "clock", lambda: clock[0])
    channel.subs, channel.clock = subs, clock
    return channel


def requested(ledger="led1", **fields):
    """What the launch writes before it waits (conversation.request)."""
    return conversation.update(ledger, **{
        "platform": "slack", "chat_id": CHAN, "user_id": ME, "thread_id": None,
        "name": f"r#7 Fix it · {ledger}", "attempts": 0, "since": 0, "previous": [], "cards": [], "noticed": False,
        "why": "", "state": "pending", **fields})


def tick(channel, seconds=0, n=1):
    for _ in range(n):
        channel.clock[0] += seconds
        asyncio.run(gateway.scan())


def opened(channel, ledger="led1", cards=()):
    requested(ledger, cards=list(cards))
    tick(channel)
    for card in cards:
        core.subscribe(card, ledger)
    return conversation.load(ledger)["thread_id"]


def test_presenting_through_the_gateway_saves_the_ids_once(channel):
    req = ask([question(), question("Second?")])
    asyncio.run(gateway.scan())
    asyncio.run(gateway.scan())
    assert len(channel.client.posts) == 2
    assert decisions.load(req["id"])["presented"]["messages"] == {"0": ["1.000100"], "1": ["2.000100"]}


def test_a_long_command_through_the_gateway_is_sent_once(channel):
    req = permission("x" * 2901)
    asyncio.run(gateway.scan())
    asyncio.run(gateway.scan())
    assert len(channel.client.posts) == 1 and decisions.load(req["id"])["status"] == "stale"


def test_the_gateway_posts_the_thread_parent_once(threads):
    requested()
    seen = []
    real = threads.client.chat_postMessage

    async def post(**kw):
        seen.append(conversation.load("led1"))
        return await real(**kw)

    threads.client.chat_postMessage = post
    tick(threads)
    (parent,) = threads.client.posts
    assert seen[0]["asked"] is True and seen[0]["state"] == "creating" and seen[0]["attempts"] == 1
    assert "thread_ts" not in parent and "· led1" in parent["text"] and "⏳ running" in parent["text"]
    ref = conversation.load("led1")
    assert ref["state"] == "open" and ref["thread_id"] == "1.000100"
    req = ask()
    tick(threads)
    assert threads.client.posts[-1]["thread_ts"] == "1.000100" and decisions.load(req["id"])["presented"]


@pytest.mark.parametrize("fail", ["raises", "restart mid-call"])
def test_a_parent_that_did_not_post_falls_back_once(threads, monkeypatch, fail):
    if fail == "raises":
        requested()
        threads.client.fail["chat_postMessage"] = ["ratelimited"]
    else:
        requested(state="creating", asked=True, attempts=1)
    tick(threads)
    ref = conversation.load("led1")
    assert ref["state"] == "fallback" and ref["why"]
    tick(threads)
    monkeypatch.setattr(slack, "T", slack.State())
    slack.T.client = threads.client
    tick(threads, n=2)
    (notice,) = threads.client.posts
    assert "thread_ts" not in notice and notice["text"].startswith("Thread not created for r#7 Fix it · led1: ")
    assert notice["text"].endswith("This run stays in the channel.") and conversation.load("led1")["noticed"]


def test_an_archived_run_gets_no_parent(threads):
    requested()
    threads.subs.status["led1"] = "archived"
    tick(threads, n=2)
    assert conversation.load("led1")["state"] == "deleted" and threads.client.posts == []


def test_closing_says_done_once_and_marks_the_parent(threads):
    thread = opened(threads)
    conversation.finish("led1")
    tick(threads)
    tick(threads)
    (note,) = threads.client.posts[1:]
    assert note["thread_ts"] == thread and "finished" in note["text"]
    (parent,) = threads.client.updates
    assert parent["ts"] == thread and "✅ done" in parent["text"]
    ref = conversation.load("led1")
    assert ref["state"] == "closed" and ref["closed_at"] == threads.clock[0] and ref["closed_by"] == "muster"


def test_a_parent_update_that_fails_is_retried_after_its_backoff(threads):
    opened(threads)
    conversation.finish("led1")
    threads.client.fail["chat_update"] = ["ratelimited"]
    tick(threads)
    ref = conversation.load("led1")
    assert ref["state"] == "closing" and ref["retry_at"] > threads.clock[0]
    tick(threads, 1)
    assert conversation.load("led1")["state"] == "closing"
    tick(threads, 10)
    assert conversation.load("led1")["state"] == "closed" and len(threads.client.posts) == 2  # one finish note


@pytest.mark.parametrize("error", slack.GONE)
def test_a_gone_parent_still_closes_and_retires(threads, error):
    opened(threads)
    conversation.finish("led1")
    threads.client.fail["chat_update"] = [error]
    tick(threads)
    assert conversation.load("led1")["state"] == "closed"
    threads.subs.status["led1"] = "archived"
    threads.client.fail["chat_update"] = [error]
    tick(threads, 301, n=2)
    assert conversation.load("led1")["state"] == "deleted"


def test_a_newer_request_reopens_a_closed_thread_without_waiting_for_the_parent(threads):
    thread = opened(threads)
    conversation.finish("led1")
    tick(threads)
    threads.client.fail["chat_update"] = ["ratelimited"]
    req = ask()
    tick(threads)
    assert threads.client.posts[-1]["thread_ts"] == thread and decisions.load(req["id"])["presented"]
    ref = conversation.load("led1")
    assert ref["state"] == "open" and ref["parent_text"] == "running"
    tick(threads)
    assert "⏳ running" in threads.client.updates[-1]["text"] and not conversation.load("led1").get("parent_text")


def test_a_closed_thread_reopens_for_nothing_older_than_its_close(threads):
    opened(threads)
    req = ask()
    tick(threads)
    conversation.finish("led1")
    decisions.update(req["id"], presented={})
    threads.clock[0] = time.time()  # closed after the request was made
    tick(threads)
    before = len(threads.client.posts)
    tick(threads, n=2)
    assert len(threads.client.posts) == before and conversation.load("led1")["state"] == "closed"


def test_a_closed_thread_of_an_archived_run_never_reopens(threads):
    opened(threads)
    conversation.finish("led1")
    tick(threads)
    threads.subs.status["led1"] = "archived"
    req = ask()
    tick(threads, n=2)
    assert decisions.load(req["id"])["status"] == "stale" and conversation.load("led1")["state"] == "deleted"
    assert not [p for p in threads.client.posts if p.get("blocks")]


def test_retiring_stales_requests_marks_the_parent_and_unsubscribes(threads):
    thread = opened(threads, cards=["t_card", "t_wait"])
    req = ask()
    tick(threads)
    threads.subs.status["led1"] = "archived"
    tick(threads, 31, n=2)
    ref = conversation.load("led1")
    assert ref["state"] == "deleted" and ref["unsubscribed"] is True
    assert decisions.load(req["id"])["status"] == "stale"
    assert any(u["ts"] == thread and "archived" in u["text"] for u in threads.client.updates)
    assert threads.subs.rows["t_card"] == [] and threads.subs.rows["t_wait"] == []
    assert ref["ledger"] not in [r["ledger"] for r in conversation.active()]


def test_an_unsubscribe_that_fails_is_retried(threads):
    opened(threads, cards=["t_card"])
    real = threads.subs.__call__
    fails = [1]

    def flaky(*argv):
        if argv[0] == "notify-unsubscribe" and fails[0]:
            fails[0] -= 1
            raise core.CommandError("database is locked")
        return real(*argv)

    core.kanban = flaky
    threads.subs.status["led1"] = "archived"
    tick(threads, 31)
    ref = conversation.load("led1")
    assert ref["state"] == "deleted" and ref["unsubscribed"] is False and "led1" in [
        r["ledger"] for r in conversation.active()]
    tick(threads, 10)
    assert conversation.load("led1")["unsubscribed"] is True and threads.subs.rows["t_card"] == []


@pytest.mark.parametrize("state", ["retiring", "deleted"])
def test_a_retired_runs_request_is_staled_not_shown(threads, state):
    requested(state=state, thread_id="5.1", unsubscribed=True)
    req = ask()
    tick(threads)
    assert decisions.load(req["id"])["status"] == "stale" and not [p for p in threads.client.posts if p.get("blocks")]


def test_a_telegram_runs_ref_is_never_stepped_and_its_requests_go_top_level(threads):
    requested(platform="telegram", chat_id="-100123", state="open", thread_id="77")
    req = ask()
    tick(threads, 31)
    assert conversation.load("led1")["state"] == "open" and threads.client.updates == []
    (post,) = threads.client.posts
    assert "thread_ts" not in post and post["blocks"][0]["text"]["text"].startswith("o/r #7")
    assert decisions.load(req["id"])["presented"]


def test_a_request_shown_on_telegram_is_posted_again_with_its_answers_forgotten(channel):
    req = ask([question(), question("Second?")], presented={"boot": "old", "messages": {"0": ["555"], "1": ["556"]}},
              partial={"0": ["Alpha", [0]]})  # shown before the switch: no platform recorded means Telegram
    asyncio.run(gateway.scan())
    assert len(channel.client.posts) == 2 and decisions.load(req["id"]).get("partial") == {}
    channel.client.posts.clear()
    tap(channel, req, 0, "muster_choice_1")
    assert decisions.load(req["id"])["partial"]["0"][0] == "Beta" and channel.client.ephemerals == []


def test_an_ended_request_shown_on_telegram_is_not_edited_here(channel):
    req = ask(presented={"messages": {"0": ["555"]}})
    decisions.transition(req["id"], ("open",), "stale", outcome="gone")
    assert asyncio.run(slack.outcome(decisions.load(req["id"]))) is True and channel.client.updates == []


def test_an_option_the_request_lacks_is_refused(channel):
    req = ask()
    shown(req, channel)
    tap(channel, req, 0, "muster_choice_7")
    tap(channel, req, 3, "muster_choice_0")
    assert [e["text"] for e in channel.client.ephemerals] == ["Not authorized"] * 2
    assert decisions.load(req["id"])["status"] == "open"


def test_a_hermes_without_the_slack_surfaces_is_logged(monkeypatch):
    class Old:
        def get_config(self, key, default):
            return "slack" if key == "notify_platform" else default

    gateway.setup(Old())
    assert "lacks the slack surfaces" in core.log_path("gateway").read_text()


# -- the run's status line (#46) ------------------------------------------------------------------

import os  # noqa: E402

from muster import events, progress  # noqa: E402

P = "t_p1"


def launched(t, ledger=P):
    directory = config.data_dir() / "intake" / ledger
    directory.mkdir(parents=True, exist_ok=True)
    progress.launched(ledger, directory, "sha")
    os.utime(progress.activity(ledger) / "launched", (t, t))
    (directory / "prompt-seen.jsonl").write_text(f'{{"sha256": "sha", "at": {int(t)}}}\n')


def test_the_thread_parent_carries_the_status_line_and_nothing_is_posted(threads):
    thread = opened(threads, ledger=P)
    posts = len(threads.client.posts)
    launched(940)
    tick(threads)
    assert len(threads.client.posts) == posts
    update = threads.client.updates[-1]
    assert update["ts"] == thread and update["channel"] == CHAN
    assert update["text"].endswith(f"⏳ running\nBrief received {progress.hm(940)} · no tool activity observed yet")
    asyncio.run(slack.parent({**conversation.load(P), "parent_text": "running"}))  # a reopen's own parent write
    assert threads.client.updates[-1]["text"] == update["text"]


def test_a_resolved_threads_parent_is_left_to_its_lifecycle(threads):
    opened(threads, ledger=P)
    launched(940)
    tick(threads)
    conversation.update(P, resolved_at=1000, state="closing")
    tick(threads)
    assert progress.load(P)["final"] is True
    assert threads.client.updates[-1]["text"].endswith("✅ done")
    assert not any("Finished" in u["text"] for u in threads.client.updates)
    asyncio.run(slack.parent({**conversation.load(P), "parent_text": "running"}))  # reopened later for new work
    assert threads.client.updates[-1]["text"].endswith("⏳ running")  # never "running" with a finished line


def test_without_a_thread_one_channel_message_is_posted_then_edited(channel, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(shared, "now", lambda: clock[0])
    monkeypatch.setattr(events, "status", lambda card: "ready")
    launched(940)
    asyncio.run(gateway.scan())
    [post] = channel.client.posts
    assert post["channel"] == CHAN and "thread_ts" not in post and post["text"].startswith(f"muster · {P}\n")
    at = progress.activity(P) / "stop"
    at.touch()
    os.utime(at, (990, 990))
    asyncio.run(gateway.scan())
    assert len(channel.client.posts) == 1
    assert channel.client.updates[-1]["ts"] == progress.load(P)["mid"] and "Turn ended" in channel.client.updates[-1]["text"]


def test_a_refused_channel_post_is_retried_and_an_unanswered_one_never(channel, monkeypatch):
    monkeypatch.setattr(shared, "now", lambda: 1000.0)
    monkeypatch.setattr(events, "status", lambda card: "ready")
    launched(940)
    channel.client.fail["chat_postMessage"] = ["channel_not_found"]
    asyncio.run(gateway.scan())
    assert channel.client.posts == [] and progress.load(P)["retry_at"] == 1000.0 + progress.RETRY

    async def lost(**kw):
        raise TimeoutError("no answer")
    monkeypatch.setattr(channel.client, "chat_postMessage", lost)
    monkeypatch.setattr(shared, "now", lambda: 2000.0)
    asyncio.run(gateway.scan())
    asyncio.run(gateway.scan())
    assert progress.load(P)["lost"] == {"thread": None}
