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


def test_too_many_choices_is_a_failed_send(channel):
    many = ask([question(labels=tuple(f"o{i}" for i in range(26)))])
    with pytest.raises(ValueError):
        slack.blocks(many, 0)
    with pytest.raises(ValueError):
        slack.blocks(ask([question(labels=tuple(f"o{i}" for i in range(11)), multi=True)]), 0)
    sent = asyncio.run(slack.present(many, None, [0]))
    assert sent["messages"] == {} and "choices" in sent["failure"] and channel.client.posts == []


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
