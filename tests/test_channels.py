"""channels: one module per platform, picked by name; the neutral gateway against a channel's contract."""

import pytest

from muster import channels, config
from muster.channels import slack, telegram


def test_get_picks_a_channel_by_name_and_refuses_any_other():
    assert channels.get("telegram") is telegram and channels.get("slack") is slack
    assert (telegram.name, telegram.durable_prompts, slack.name, slack.durable_prompts) == (
        "telegram", False, "slack", True)
    with pytest.raises(config.ConfigError, match="telegram, slack"):
        channels.get("discord")


def test_slack_has_no_dm_fallback():
    with pytest.raises(config.ConfigError, match="notify_chat_id"):
        slack.home()


# -- the neutral gateway against a durable channel (the skip rule) ---------------------------------

import asyncio  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402

from muster import decisions, gateway  # noqa: E402
from muster.channels import shared  # noqa: E402


class Durable:
    """A channel whose prompts outlive the process, as Slack's do: records what it is asked to present."""
    name, durable_prompts = "fake", True

    def __init__(self):
        self.calls, self.edits, self.fail_at, self.count = [], [], None, 0

    async def steps(self):
        pass

    def track(self, req):
        pass

    def release(self, rid):
        pass

    def release_ended(self, live):
        pass

    async def destination(self, req):
        return True, None

    def sent(self, req, slots):
        self.calls.append((req["id"], list(slots)))
        messages = {}
        for n in slots:
            if self.fail_at == n:
                self.fail_at = None
                return {"messages": messages, "failure": "boom"}
            self.count += 1
            messages[str(n)] = [f"m{self.count}"]
        return {"messages": messages, "failure": None}

    async def present(self, req, meta, questions):
        return self.sent(req, questions)

    async def present_approval(self, req, meta):
        return self.sent(req, ["0"])

    async def outcome(self, req):
        return True

    async def edit(self, chat, mid, text):
        self.edits.append(mid)


@pytest.fixture
def durable(monkeypatch):
    channel = Durable()
    monkeypatch.setattr(gateway, "CHANNEL", channel)
    monkeypatch.setattr(gateway, "S", gateway.State())
    monkeypatch.setattr(shared, "CTX", types.SimpleNamespace(
        spawn_task=lambda coro, name=None: asyncio.get_running_loop().create_task(coro)))
    monkeypatch.setattr(decisions, "obsolete", lambda req: None)
    return channel


def restart(monkeypatch, boot):
    monkeypatch.setattr(gateway, "S", gateway.State())
    monkeypatch.setattr(gateway, "BOOT", boot)


def two(**fields):
    q = {"text": "Q", "options": [{"label": "A"}, {"label": "B"}]}
    return decisions.create("question", "led1", questions=[q, q], choices=[["A", "B"], ["A", "B"]],
                            alive=time.time(), **fields)["id"]


def test_a_durable_prompt_is_presented_once_across_restarts(durable, monkeypatch):
    rid = two()
    asyncio.run(gateway.scan())
    asyncio.run(gateway.scan())
    assert durable.calls == [(rid, [0, 1])]
    restart(monkeypatch, "boot2")
    asyncio.run(gateway.scan())
    assert durable.calls == [(rid, [0, 1])] and durable.edits == []
    assert decisions.load(rid)["presented"]["platform"] == "fake"


def test_a_failed_durable_send_retries_only_the_unsent_questions(durable):
    rid = two()
    durable.fail_at = 1
    asyncio.run(gateway.scan())
    gateway.S.retry[rid] = (0, 2)  # the backoff has passed
    asyncio.run(gateway.scan())
    assert durable.calls == [(rid, [0, 1]), (rid, [1])] and durable.edits == []  # no Superseded edit
    assert decisions.load(rid)["presented"]["messages"] == {"0": ["m1"], "1": ["m2"]}


def test_another_platforms_ids_are_not_trusted(durable):
    rid = two(presented={"boot": "old", "platform": "telegram", "messages": {"0": ["7"], "1": ["8"]}})
    asyncio.run(gateway.scan())
    assert durable.calls == [(rid, [0, 1])]
    assert decisions.load(rid)["presented"]["messages"] == {"0": ["m1"], "1": ["m2"]}


def test_the_first_answer_to_a_question_wins(durable):
    rid = two()
    req, result, part = asyncio.run(shared.answer(rid, 0, "A"))
    assert (result, part) == ("accepted", ["A", [0]])
    req, result, part = asyncio.run(shared.answer(rid, 0, "B"))
    assert (result, part, req["status"]) == ("duplicate", ["A", [0]], "open")
    req, result, part = asyncio.run(shared.answer(rid, 1, "own words"))
    assert (result, part) == ("completed", ["own words", None])
    assert decisions.load(rid)["answer"] == {"Q": "own words"} and decisions.load(rid)["status"] == "answered"


def test_a_durable_permission_is_presented_once_and_again_only_without_its_message(durable, monkeypatch):
    rid = decisions.create("permission", "led1", card={"command": "touch x", "why": "w"}, alive=time.time())["id"]
    asyncio.run(gateway.scan())
    restart(monkeypatch, "boot2")
    asyncio.run(gateway.scan())
    assert durable.calls == [(rid, ["0"])]
    decisions.update(rid, presented={"platform": "fake", "messages": {"0": []}})
    restart(monkeypatch, "boot3")
    asyncio.run(gateway.scan())
    assert durable.calls == [(rid, ["0"]), (rid, ["0"])]


def test_a_shown_durable_build_whose_pull_request_moved_on_is_staled(durable, monkeypatch):
    q = {"text": "Merge?", "options": [{"label": "Merge"}, {"label": "Do nothing"}]}
    rid = decisions.create("build", "led1", questions=[q], choices=[["Merge", "Do nothing"]],
                           actions=["merge", "nothing"], run={"branch": "b"})["id"]
    asyncio.run(gateway.scan())
    assert durable.calls == [(rid, [0])]
    monkeypatch.setattr(decisions, "obsolete", lambda req: "the pull request has new commits")
    restart(monkeypatch, "boot2")  # a fresh vetted clock
    asyncio.run(gateway.scan())
    req = decisions.load(rid)
    assert req["status"] == "stale" and req["outcome"] == "Not shown: the pull request has new commits"
    assert durable.calls == [(rid, [0])]
