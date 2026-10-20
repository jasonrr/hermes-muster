import asyncio
import hashlib
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from muster import config, core, decisions, gateway, launcher
from muster.channels import shared, telegram

from tests import fake_hermes as fh

ROOT = Path(__file__).resolve().parent.parent
DM = "4242"  # TELEGRAM_HOME_CHANNEL in the sandbox .env


@pytest.fixture(autouse=True)
def hermes(monkeypatch):
    clarify = fh.install(monkeypatch)
    from tools import approval, approval_gateway_wait
    monkeypatch.setattr(approval_gateway_wait, "TIMEOUT", 2)  # a stuck approval wait ends by itself
    monkeypatch.setattr(gateway, "S", gateway.State())
    monkeypatch.setattr(telegram, "T", telegram.State())
    monkeypatch.setattr(gateway, "CHANNEL", telegram)
    monkeypatch.setattr(shared, "checked", {})
    monkeypatch.setattr(decisions, "obsolete", lambda req: None)  # GitHub's read of the pull request: still open
    monkeypatch.setattr(shared, "CTX", types.SimpleNamespace(
        get_config=lambda k, d: config.settings.get(k, d), spawn_task=fh.spawn_task))
    monkeypatch.setattr(shared, "configured", True)
    telegram.T.adapter = fh.Adapter()
    before = set(threading.enumerate())
    LOOP.append(asyncio.new_event_loop())  # one loop per test: waiter threads hand answers back to the loop of the scan
    yield clarify
    for rid in [*telegram.T.shown, *telegram.T.approvals]:  # waiter threads block in the fakes: release them all
        telegram.release(rid)
    for key in list(clarify._index):
        clarify.clear_session(key)
    for key, queue in list(approval._gateway_queues.items()):
        for entry in list(queue):
            approval.withdraw_gateway_approval(key, entry.data.get("request_id"), "test over")
    loop = LOOP.pop()
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.run_until_complete(loop.shutdown_default_executor())  # to_thread's pool threads
    loop.close()
    for t in set(threading.enumerate()) - before:
        t.join(3)
        assert not t.is_alive(), f"thread {t.name} outlived its test"


LOOP = []


def question(text="Which?", labels=("Alpha", "Beta"), multi=False):
    return {"text": text, "header": "H", "multi": multi,
            "options": [{"label": label, "description": f"about {label}"} for label in labels]}


def ask(text="Which?", labels=("Alpha", "Beta"), multi=False, ledger="led1", **fields):
    fields = {"questions": [question(text, labels, multi)], "choices": [list(labels)], "tool": {"name": "AskUserQuestion"},
              "wait": "w1", "run": {"repo": "o/r", "issue": 7, "branch": "muster/7", "pane": "p1", "kind": "issue"},
              "alive": time.time(), **fields}
    return decisions.create("question", ledger, **fields)


def permission(**fields):
    fields = {"tool": {"name": "Bash"}, "run": {"branch": "b", "pane": "p1"}, "alive": time.time(),
              "card": {"command": "touch x", "why": "Create x"}, **fields}
    return decisions.create("permission", "led1", **fields)


def run(coro):
    return LOOP[0].run_until_complete(coro)


async def until(check, seconds=2):
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


def sent():
    return telegram.T.adapter.sent


def status(req):
    return decisions.load(req["id"])["status"]


# -- the factory ------------------------------------------------------------------------------------

def test_factory_registers_both_guards_and_one_scan_task(monkeypatch):
    monkeypatch.setattr(shared, "SCAN_EVERY", 0.01)
    prepared = []
    monkeypatch.setattr(core, "prepare_env", lambda: prepared.append(1))
    app, other = fh.Application(), fh.Application()

    async def go():
        telegram.telegram_factory(app, fh.Adapter())
        first = gateway.S.task
        new = fh.Adapter()
        telegram.telegram_factory(other, new)  # an app rebuild
        assert gateway.S.task is first and telegram.T.adapter is new and telegram.T.bot is other.bot
        first.cancel()

    run(go())
    assert [(g, h.pattern, h.callback) for h, g in app.handlers] == [
        (-1, r"^cl:mu", telegram.guard), (-1, r"^ea:", telegram.approval_guard), (-1, None, telegram.topic_created),
        (-1, None, telegram.topic_toggled)]
    assert app.handlers[2][0].filters == "forum_topic_created"
    assert app.handlers[3][0].filters == "forum_topic_closed|forum_topic_reopened"
    assert other.handlers and prepared == [1, 1]


def test_a_poisoned_request_does_not_stop_the_others():
    decisions.create("question", "led0")  # no questions: presenting it raises
    good = ask()
    run(gateway.scan())
    assert [m["cid"] for m in sent()] == [f"mu{good['id']}q0"]
    assert "request" in core.log_path("gateway").read_text()


# -- presenting -------------------------------------------------------------------------------------

def test_an_open_request_is_presented_once(hermes):
    req = ask(proposal={"version": 2, "sha": "abcdef123456"})
    run(gateway.scan())
    run(gateway.scan())
    (msg,) = sent()
    cid = f"mu{req['id']}q0"
    assert (msg["chat"], msg["cid"], msg["session"], msg["parse_mode"]) == (DM, cid, f"muster:{req['id']}:0", "HTML")
    assert msg["buttons"] == [("Alpha", f"cl:{cid}:0"), ("Beta", f"cl:{cid}:1"), (fh.OTHER, f"cl:{cid}:other")]
    assert telegram.T.adapter._clarify_state == {cid: f"muster:{req['id']}:0"}  # Hermes's tap handler needs it
    for part in ("❓ Issue #7\n\nWhich?", "• Alpha: about Alpha", "reply to this message"):
        assert part in msg["text"]
    assert "1. Alpha" not in msg["text"] and "pane" not in msg["text"]  # the buttons carry the labels
    assert "**" not in msg["text"] and "led1" not in msg["text"]  # no literal markers, no card id
    assert decisions.load(req["id"])["presented"] == {"boot": gateway.BOOT, "messages": {"0": [msg["mid"]]},
                                                      "platform": "telegram"}
    assert hermes.get_pending_for_session(f"muster:{req['id']}:0", include_choice_prompts=True)


def test_each_question_has_its_own_session():
    two = [question(f"Q{i}") for i in range(2)]
    req = ask(questions=two, choices=[["Alpha", "Beta"]] * 2)
    run(gateway.scan())
    assert [m["session"] for m in sent()] == [f"muster:{req['id']}:0", f"muster:{req['id']}:1"]


def test_two_concurrent_scans_present_once():
    ask()

    async def go():
        await asyncio.gather(gateway.scan(), gateway.scan())

    run(go())
    assert len(sent()) == 1


def test_a_multi_select_hint_is_shown():
    ask(multi=True)
    run(gateway.scan())
    assert "tap Other and type the numbers, e.g. 1,3" in sent()[0]["text"]


def test_a_send_failure_leaves_it_unpresented_then_succeeds_later(hermes):
    req = ask()
    telegram.T.adapter.fail_sends = 1
    run(gateway.scan())
    assert not sent() and "boot" not in decisions.load(req["id"])["presented"]
    assert hermes._entries == {} or all(e.event.is_set() for e in hermes._entries.values())  # released
    run(gateway.scan())  # inside the backoff: no new attempt
    assert not sent() and gateway.S.retry[req["id"]][1] == 2
    gateway.S.retry[req["id"]] = (0, 2)
    run(gateway.scan())
    assert len(sent()) == 1 and decisions.load(req["id"])["presented"]["boot"] == gateway.BOOT
    assert gateway.S.retry == {}


def test_a_request_that_ends_unsent_stops_retrying():
    req = ask()
    telegram.T.adapter.fail_sends = 1
    run(gateway.scan())
    assert req["id"] in gateway.S.retry
    decisions.transition(req["id"], ("open",), "done", outcome="Answered in the pane")
    run(gateway.scan())
    assert gateway.S.retry == {}


def test_the_backoff_doubles_up_to_a_minute():
    req = ask()
    delays = []
    for _ in range(7):
        telegram.T.adapter.fail_sends = 1
        run(gateway.scan())
        delays.append(gateway.S.retry[req["id"]][1])
        gateway.S.retry[req["id"]] = (0, delays[-1])
    assert delays == [2, 4, 8, 16, 32, 60, 60]


def test_oversize_text_is_capped_and_points_to_the_wait_card():
    ask(text="x" * 6000)
    run(gateway.scan())
    text = sent()[0]["text"]
    assert len(text) <= telegram.CAP + len("❓ ")
    assert "Some option details did not fit in this message." in text and "pane" not in text and "w1" not in text


def test_a_description_that_repeats_its_label_is_not_shown():
    ask(labels=("Skip", "No phrase"), questions=[{"text": "Phrase?", "header": "", "multi": False, "options": [
        {"label": "Skip", "description": "Skip"}, {"label": "No phrase", "description": " No phrase "}]}])
    run(gateway.scan())
    assert "•" not in sent()[0]["text"]


def test_long_descriptions_are_cut_first():
    ask(labels=("A", "B"), questions=[{"text": "Which?", "header": "", "multi": False, "options": [
        {"label": "A", "description": "d" * 2000}, {"label": "B", "description": "e" * 2000}]}])
    run(gateway.scan())
    text = sent()[0]["text"]
    assert len(text) <= telegram.CAP + len("❓ ") and "d" * 100 not in text and "did not fit" in text


def test_a_build_review_shows_its_actions_on_the_buttons():
    labels = ["Merge PR (recommended)", "Send back", "Do nothing"]
    q = {"text": "Review: fine", "header": "Build", "multi": False,
         "options": [{"label": label, "description": ""} for label in labels]}
    decisions.create("build", "led1", run={"repo": "o/r", "issue": 7, "pane": "p1"}, actions=["merge", "send-back", "nothing"],
                     choices=[labels], questions=[q], head="h" * 40, base="main", pr="u", cycle=1)
    run(gateway.scan())
    (msg,) = sent()
    assert [b for b, _ in msg["buttons"]] == [*labels, fh.OTHER]
    assert "Merge (squash)" not in msg["text"] and "•" not in msg["text"]  # no legend: the buttons say it


def test_a_long_label_is_cut_on_its_button_and_kept_whole_in_the_text():
    long = "Use the shared config loader for every entry point"
    ask(labels=(long, "Beta"), questions=[{"text": "Which?", "header": "", "multi": False, "options": [
        {"label": long, "description": ""}, {"label": "Beta", "description": "about Beta"}]}])
    run(gateway.scan())
    (msg,) = sent()
    button = msg["buttons"][0][0]
    assert len(button) == telegram.LABEL_MAX and button == long[:telegram.LABEL_MAX - 1] + "…"
    assert f"• {long}\n" in msg["text"] and "• Beta: about Beta" in msg["text"]


def test_a_long_label_stays_whole_in_oversize_text():
    long = "Use the shared config loader for every entry point"
    ask(text="x" * 6000, labels=(long, "B"), questions=[{"text": "x" * 6000, "header": "", "multi": False, "options": [
        {"label": long, "description": "d" * 500}, {"label": "B", "description": "e" * 500}]}])
    run(gateway.scan())
    text = sent()[0]["text"]
    assert f"• {long}" in text and "d" * 100 not in text and len(text) <= telegram.CAP + len("❓ ")


def test_cut_labels_that_would_look_alike_are_numbered_on_buttons_and_text():
    east, west = "Deploy to production cluster us-east-1", "Deploy to production cluster us-west-2"
    ask(labels=(east, west), questions=[{"text": "Where?", "header": "", "multi": False, "options": [
        {"label": east, "description": ""}, {"label": west, "description": ""}]}])
    run(gateway.scan())
    (msg,) = sent()
    first, second = (b for b, _ in msg["buttons"][:2])
    assert first != second and first.startswith("1. ") and second.startswith("2. ")
    assert f"• 1. {east}" in msg["text"] and f"• 2. {west}" in msg["text"]


def test_a_blank_label_gets_a_number_not_an_empty_button():
    ask(labels=("", "Beta"), questions=[{"text": "Which?", "header": "", "multi": False, "options": [
        {"label": "", "description": ""}, {"label": "Beta", "description": ""}]}])
    run(gateway.scan())
    assert [b for b, _ in sent()[0]["buttons"][:2]] == ["1. ", "2. Beta"]


def test_a_cut_choice_is_listed_whole_even_when_it_differs_from_its_option_label():
    label = "y" * 30
    ask(labels=(label, f"{label} (2)"), questions=[{"text": "Which?", "header": "", "multi": False, "options": [
        {"label": label, "description": ""}, {"label": label, "description": ""}]}])
    run(gateway.scan())
    assert f"• {label} (2)" in sent()[0]["text"]


def test_several_keep_their_numbers_on_the_buttons():
    ask(multi=True, questions=[{"text": "Which?", "header": "", "multi": True, "options": [
        {"label": "Alpha", "description": ""}, {"label": "Beta", "description": ""}]}])
    run(gateway.scan())
    assert [b for b, _ in sent()[0]["buttons"]][:2] == ["1. Alpha", "2. Beta"]
    assert "•" not in sent()[0]["text"]  # numbered, not cut: nothing to repeat in the text


def test_the_text_is_html_escaped_and_a_tap_maps_to_the_exact_choice(hermes):
    req = ask(text="Use <b> & co?", labels=("<i>", "Beta"))
    run(gateway.scan())
    (msg,) = sent()
    assert "Use &lt;b&gt; &amp; co?" in msg["text"] and msg["buttons"][0][0] == "<i>"
    # Hermes's tap handler answers cl:<id>:<idx> with the registered choices[idx], never the button text
    assert hermes._entries[msg["cid"]].choices == ["<i>", "Beta"] == decisions.load(req["id"])["choices"][0]


def presented(hermes, **fields):
    req = ask(**fields)
    run(gateway.scan())
    return req, sent()[-1]["mid"]


# -- Other: a ForceReply ----------------------------------------------------------------------------

def test_other_sends_a_force_reply_bound_to_the_question(hermes):
    req, mid = presented(hermes)
    app = fh.Application()
    telegram.T.bot = app.bot

    async def go():
        await telegram.guard(fh.update(4242, 4242, data=f"cl:mu{req['id']}q0:other"), None)
        (prompt,) = app.bot.sent
        assert isinstance(prompt["markup"], fh.ForceReply) and prompt["markup"].selective
        assert "reply to this message" in prompt["text"] and "tg://user?id=4242" in prompt["text"]
        assert decisions.load(req["id"])["presented"]["replies"] == {"0": [str(prompt["mid"])]}
        hermes.mark_awaiting_text(f"mu{req['id']}q0")  # what Hermes's own handler does next
        got = await gateway.on_dispatch(event=fh.event("typed words", "4242", "4242", reply=str(prompt["mid"])),
                                        gateway=fh.Gateway())
        await until(lambda: status(req) == "answered")
        return got

    assert run(go()) == {"action": "skip"}
    assert decisions.load(req["id"])["answer"] == {"Which?": "typed words"}
    assert (DM, mid, "Received ✓: typed words") not in telegram.T.adapter.edits  # the prompt's own message is edited
    assert telegram.T.adapter.edits[-1][2] == "Received ✓: typed words"


def test_ask_for_text_without_a_bot_or_on_foreign_data_sends_nothing():
    app = fh.Application()
    run(telegram.ask_for_text(fh.Query(4242, 4242, "cl:mu00q0:other")))  # no bot yet
    telegram.T.bot = app.bot
    run(telegram.ask_for_text(fh.Query(4242, 4242, "cl:other")))
    assert app.bot.sent == []


# -- answers ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,multi,expect", [
    ("Beta", False, ("Beta", [1])), ("Alpha", True, ("Alpha", [0])),
    ('["Beta", "Alpha"]', True, ("Beta, Alpha", [1, 0])), ('["Alpha", "nope"]', True, ('["Alpha", "nope"]', None)),
    ('["Alpha"]', False, ('["Alpha"]', None)),  # a JSON array is only a multi-select answer
    ("something else", False, ("something else", None)), ("2", False, ("2", None)),
])
def test_shape_maps_labels_to_indexes_and_anything_else_is_free_text(value, multi, expect):
    assert gateway.shape(ask(multi=multi), 0, value) == expect


def test_a_tapped_numeric_label_is_that_option_not_an_index():
    # Hermes resolves a tap with the label itself: "3" is the option labelled 3, not the third option
    assert gateway.shape(ask(labels=("2", "3", "5")), 0, "3") == ("3", [1])


def test_a_tap_answers_the_request(hermes, monkeypatch):
    called = []
    monkeypatch.setattr(gateway, "on_answered", called.append)
    req = ask()
    rid = req["id"]

    async def go():
        await gateway.scan()
        assert hermes.resolve_gateway_clarify(f"mu{rid}q0", "Beta")  # what Hermes's callback does
        await until(lambda: status(req) == "answered")

    run(go())
    assert decisions.load(rid)["answer"] == {"Which?": "Beta"} and [c["id"] for c in called] == [rid]
    assert telegram.T.adapter.edits == []  # Hermes edits the message on a tap, not muster


def test_every_question_must_be_answered_and_a_double_answer_counts_once(monkeypatch):
    called = []
    monkeypatch.setattr(gateway, "on_answered", called.append)
    two = [question(f"Q{i}", ("Yes",)) for i in range(2)]
    req = ask(questions=two, choices=[["Yes"], ["Yes"]])
    rid = req["id"]

    async def go():
        await gateway.scan()
        assert len(sent()) == 2
        await gateway.answered(rid, 0, "Yes")
        assert status(req) == "open"
        await gateway.answered(rid, 1, "free")
        assert decisions.load(rid)["answer"] == {"Q0": "Yes", "Q1": "free"}
        await gateway.answered(rid, 1, "again")
        await gateway.answered(rid, 0, "again")

    run(go())
    assert len(called) == 1


def test_a_telegram_restart_presents_only_the_unanswered_questions_and_keeps_the_answers(hermes, monkeypatch):
    called = []
    monkeypatch.setattr(gateway, "on_answered", called.append)
    two = [question(f"Q{i}", ("Yes", "No")) for i in range(2)]
    req = ask(questions=two, choices=[["Yes", "No"], ["Yes", "No"]])
    rid = req["id"]

    async def go():
        await gateway.scan()
        assert hermes.resolve_gateway_clarify(f"mu{rid}q0", "Yes")
        await until(lambda: decisions.load(rid).get("partial") == {"0": ["Yes", [0]]})
        telegram.release(rid)
        gateway.S, telegram.T = gateway.State(), telegram.State()  # a restart: its prompts are dead
        telegram.T.adapter = fh.Adapter()
        monkeypatch.setattr(gateway, "BOOT", "boot2")
        await gateway.scan()
        (again,) = sent()  # only the question still unanswered; the human's first answer stands
        assert again["cid"] == f"mu{rid}q1" and decisions.load(rid)["partial"] == {"0": ["Yes", [0]]}
        assert hermes.resolve_gateway_clarify(f"mu{rid}q1", "No")
        await until(lambda: status(req) == "answered")

    run(go())
    assert decisions.load(rid)["answer"] == {"Q0": "Yes", "Q1": "No"} and len(called) == 1


def test_record_answers_a_permission_once_and_touches_no_channel_state():
    req = permission()
    got, ok = run(gateway.record(req["id"], {"decision": "allow"}))
    assert ok and got["status"] == "answered" and got["answer"] == {"decision": "allow"}
    assert run(gateway.record(req["id"], {"decision": "deny"}))[1] is False
    assert telegram.T.approvals == set() and telegram.T.shown == {}


def test_build_answers_carry_the_action(monkeypatch):
    monkeypatch.setattr(gateway, "on_answered", lambda req: None)
    labels = ["Merge (squash)", "Do nothing"]
    req = decisions.create("build", "led1", questions=[question("Merge?", labels)], choices=[labels],
                           actions=["merge", "nothing"], run={"branch": "b"})
    run(gateway.answered(req["id"], 0, "Do nothing"))
    assert decisions.load(req["id"])["answer"] == {"action": "nothing"}


def test_a_leaving_request_releases_its_clarifies(hermes):
    req = ask()
    run(gateway.scan())
    entry = hermes._entries[f"mu{req['id']}q0"]
    decisions.transition(req["id"], ("open",), "done", outcome="Answered in the pane")
    run(gateway.scan())
    assert entry.response == hermes.CANCELLED and telegram.T.shown == {}
    assert hermes._entries == {} or f"mu{req['id']}q0" not in hermes._entries


def test_a_cancelled_waiter_answers_nothing(hermes):
    req = ask()
    run(gateway.scan())
    telegram.release(req["id"])
    time.sleep(0.2)
    assert status(req) == "open" and "answer" not in decisions.load(req["id"])


# -- typed replies ----------------------------------------------------------------------------------

def dispatch(text, user=4242, chat=4242, reply=None, platform="telegram", gw=None):
    return run(gateway.on_dispatch(event=fh.event(text, user, chat, reply, platform), gateway=gw or fh.Gateway()))


def reply_and_wait(req, text, mid, user="4242", chat="4242"):
    async def go():
        got = await gateway.on_dispatch(event=fh.event(text, user, chat, reply=mid), gateway=fh.Gateway())
        await until(lambda: status(req) == "answered")
        return got

    return run(go())


def test_a_reply_by_label_or_number_resolves_the_question(hermes):
    for text, expect in (("beta", "Beta"), ("1", "Alpha")):
        req, mid = presented(hermes)
        assert reply_and_wait(req, text, mid) == {"action": "skip"}
        assert decisions.load(req["id"])["answer"] == {"Which?": expect}
        assert telegram.T.adapter.edits[-1] == (DM, mid, f"Received ✓: {text}")


def test_a_reply_to_a_multi_select_takes_numbers(hermes):
    req, mid = presented(hermes, labels=("Alpha", "Beta", "Gamma"), multi=True)
    assert reply_and_wait(req, "1,3", mid) == {"action": "skip"}
    assert decisions.load(req["id"])["answer"] == {"Which?": "Alpha, Gamma"}


def test_a_reply_that_is_no_option_is_free_text(hermes):
    req, mid = presented(hermes)
    assert reply_and_wait(req, "neither, do X", mid) == {"action": "skip"}
    assert decisions.load(req["id"])["answer"] == {"Which?": "neither, do X"}


def test_a_reply_to_an_already_resolved_question_passes_through(hermes):
    req, mid = presented(hermes)
    hermes.resolve_gateway_clarify(f"mu{req['id']}q0", "Alpha")
    assert dispatch("beta", reply=mid) is None
    assert telegram.T.adapter.edits == []


def test_a_reply_to_something_else_passes_through(hermes):
    presented(hermes)
    assert dispatch("hello", reply="55555") is None


def test_no_reply_goes_to_the_only_question_awaiting_text(hermes):
    req, _ = presented(hermes)
    assert dispatch("some prose") is None  # not awaiting text yet
    hermes.mark_awaiting_text(f"mu{req['id']}q0")

    async def go():
        got = await gateway.on_dispatch(event=fh.event("my words", "4242", "4242"), gateway=fh.Gateway())
        await until(lambda: status(req) == "answered")
        return got

    assert run(go()) == {"action": "skip"}
    assert decisions.load(req["id"])["answer"] == {"Which?": "my words"}


def test_after_other_a_reply_to_some_other_message_is_still_the_answer(hermes):
    # seen live: the human tapped Other, then replied to the Hermes agent's message, not the question's
    req, _ = presented(hermes)
    hermes.mark_awaiting_text(f"mu{req['id']}q0")
    assert reply_and_wait(req, "hello from Telegram", "228") == {"action": "skip"}
    assert decisions.load(req["id"])["answer"] == {"Which?": "hello from Telegram"}


def test_two_questions_awaiting_text_never_guess(hermes):
    a, _ = presented(hermes)
    b, _ = presented(hermes)
    hermes.mark_awaiting_text(f"mu{a['id']}q0")
    hermes.mark_awaiting_text(f"mu{b['id']}q0")
    assert dispatch("words") is None


def test_dispatch_refuses_another_platform_chat_or_unauthorized_user(hermes):
    req, mid = presented(hermes)
    hermes.mark_awaiting_text(f"mu{req['id']}q0")
    assert dispatch("words", platform="slack") is None
    assert dispatch("words", user=999) is None  # Hermes does not authorize this user
    assert dispatch("words", user=999, reply=mid) is None
    assert dispatch("words", chat=999, reply=mid) is None
    assert dispatch("   ") is None
    assert hermes._entries[f"mu{req['id']}q0"].response is None and telegram.T.adapter.edits == []


def test_dispatch_with_nothing_presented_passes_through():
    assert dispatch("words") is None


def test_dispatch_errors_pass_through(monkeypatch):
    async def boom(event):
        raise RuntimeError("x")

    monkeypatch.setattr(telegram, "reply", boom)
    assert dispatch("words") is None


# -- the guards -------------------------------------------------------------------------------------

def tap(user, chat, data="cl:mu0q0:0", fn=None):
    update = fh.update(user, chat, data=data)
    try:
        run((fn or telegram.guard)(update, None))
    except fh.ApplicationHandlerStop:
        return "stopped", update.callback_query.answers
    return "passed", update.callback_query.answers


def test_the_guard_in_a_dm_by_default():
    assert tap(4242, 4242) == ("passed", [])  # ints from Telegram compare as strings
    assert tap(999, 4242) == ("stopped", ["Not authorized"])
    assert tap(4242, 999) == ("stopped", ["Not authorized"])


def test_the_guard_in_a_group_needs_both_ids(monkeypatch):
    monkeypatch.setitem(config.settings, "notify_chat_id", -100123)
    monkeypatch.setitem(config.settings, "notify_user_id", "55")
    assert tap(55, -100123) == ("passed", [])
    assert tap(56, -100123)[0] == "stopped"  # another group member
    assert tap(55, 4242)[0] == "stopped"     # the right user, the wrong chat
    assert "refused a tap" in core.log_path("gateway").read_text()


def test_a_group_without_a_user_id_authorizes_nobody(monkeypatch):
    monkeypatch.setitem(config.settings, "notify_chat_id", -100123)
    monkeypatch.setitem(config.settings, "notify_user_id", "")
    assert tap(55, -100123)[0] == "stopped"
    assert tap(-100123, -100123)[0] == "stopped"
    assert telegram.authorized(55, -100123) is False


def test_a_guard_error_fails_closed(monkeypatch):
    monkeypatch.setattr(telegram, "authorized", lambda u, c: 1 / 0)
    assert tap(4242, 4242) == ("stopped", ["Not authorized"])
    assert "guard error" in core.log_path("gateway").read_text()


def test_a_refused_tap_on_other_sends_no_reply_prompt():
    app = fh.Application()
    telegram.T.bot = app.bot
    assert tap(999, 4242, data="cl:mu0aq0:other")[0] == "stopped"
    assert app.bot.sent == []


def test_the_approval_guard_covers_muster_cards_and_leaves_hermess_own():
    telegram.T.adapter._approval_state[1] = f"muster:abc"
    telegram.T.adapter._approval_state[2] = "agent:main:telegram"
    assert tap(999, 4242, "ea:once:1", telegram.approval_guard) == ("stopped", ["Not authorized"])
    assert tap(4242, 4242, "ea:once:1", telegram.approval_guard) == ("passed", [])
    assert tap(999, 4242, "ea:once:2", telegram.approval_guard) == ("passed", [])  # Hermes's own card
    assert tap(999, 4242, "ea:once:77", telegram.approval_guard) == ("passed", [])  # unknown id
    assert tap(999, 4242, "ea:junk", telegram.approval_guard) == ("passed", [])


# -- settling the messages --------------------------------------------------------------------------

def test_a_finished_request_edits_its_newest_message_to_the_outcome():
    req = ask()
    run(gateway.scan())
    decisions.transition(req["id"], ("open",), "done", outcome=decisions.ANSWER_SENT)
    run(gateway.scan())
    assert (DM, sent()[0]["mid"], decisions.ANSWER_SENT) in telegram.T.adapter.edits
    assert decisions.load(req["id"])["edited"] is True
    n = len(telegram.T.adapter.edits)
    run(gateway.scan())
    assert len(telegram.T.adapter.edits) == n


def test_reply_prompts_are_edited_to_the_outcome_too(hermes):
    req, mid = presented(hermes)
    decisions.update(req["id"], presented={**decisions.load(req["id"])["presented"], "replies": {"0": ["901"]}})
    decisions.transition(req["id"], ("open",), "done", outcome=decisions.ANSWER_SENT)
    run(gateway.scan())
    assert sorted(m for _, m, _ in telegram.T.adapter.edits) == sorted([mid, "901"])
    assert {t for _, _, t in telegram.T.adapter.edits} == {decisions.ANSWER_SENT}


def test_a_failed_edit_is_retried_once_then_dropped():
    req = ask()
    run(gateway.scan())
    decisions.transition(req["id"], ("open",), "done", outcome=decisions.ANSWER_SENT)
    telegram.T.adapter.fail_edits = 2
    run(gateway.scan())
    assert not decisions.load(req["id"]).get("edited") and req["id"] in gateway.S.watch
    run(gateway.scan())  # the retry also fails: dropped
    assert decisions.load(req["id"])["edited"] is True and req["id"] not in gateway.S.watch
    assert telegram.T.adapter.edits == []


def test_a_request_that_ended_while_the_gateway_was_down_is_edited_on_the_first_scan(hermes):
    req = ask()
    run(gateway.scan())
    telegram.release(req["id"])
    decisions.transition(req["id"], ("open",), "stale", outcome="The agent session ended")
    old = telegram.T
    gateway.S, telegram.T = gateway.State(), telegram.State()  # a restarted gateway
    shared.checked.clear()
    telegram.T.adapter = fh.Adapter()
    run(gateway.scan())
    assert telegram.T.adapter.edits == [(DM, old.adapter.sent[0]["mid"], "The agent session ended")]
    assert decisions.load(req["id"])["edited"] is True


def test_representing_edits_the_earlier_messages_to_superseded(hermes):
    req = ask(presented={"boot": "old", "messages": {"0": ["7", "8"]}})
    run(gateway.scan())
    (msg,) = sent()
    assert telegram.T.adapter.edits == [(DM, "7", "Replaced after a restart · use the newer message below."),
                                       (DM, "8", "Replaced after a restart · use the newer message below.")]
    assert decisions.load(req["id"])["presented"]["messages"] == {"0": ["7", "8", msg["mid"]]}


def test_a_failed_partial_send_supersedes_what_this_boot_sent():
    two = [question(f"Q{i}") for i in range(2)]
    ask(questions=two, choices=[["Alpha", "Beta"]] * 2)
    adapter, real = telegram.T.adapter, telegram.T.adapter._send_prompt

    async def second_fails(*args, **kw):
        if adapter.sent:
            adapter.fail_sends = 1
        return await real(*args, **kw)

    adapter._send_prompt = second_fails
    run(gateway.scan())
    (first,) = sent()
    assert (DM, first["mid"], "Replaced after a restart · use the newer message below.") in adapter.edits


# -- restart ----------------------------------------------------------------------------------------

def test_a_request_from_an_earlier_boot_is_represented(hermes):
    req = ask(presented={"boot": "old", "messages": {"0": ["7"]}})
    run(gateway.scan())
    (msg,) = sent()
    assert telegram.T.adapter.edits == [(DM, "7", "Replaced after a restart · use the newer message below.")]
    assert decisions.load(req["id"])["presented"] == {"boot": gateway.BOOT, "messages": {"0": ["7", msg["mid"]]},
                                                      "platform": "telegram"}


def test_a_reply_to_an_old_boots_message_still_binds(hermes):
    req = ask(presented={"boot": "old", "messages": {"0": ["7"]}})
    run(gateway.scan())
    assert reply_and_wait(req, "Alpha", "7") == {"action": "skip"}
    assert decisions.load(req["id"])["answer"] == {"Which?": "Alpha"}


def test_a_reply_to_an_old_message_before_re_presenting_passes_through():
    ask(presented={"boot": "old", "messages": {"0": ["7"]}})
    assert dispatch("Alpha", reply="7") is None


# -- liveness ---------------------------------------------------------------------------------------

def test_a_silent_hook_stales_the_request():
    req = ask(alive=time.time() - 31)
    run(gateway.scan())
    got = decisions.load(req["id"])
    assert (got["status"], got["outcome"]) == ("stale", "Ended: the builder stopped waiting. Nothing was sent.")
    assert not sent()


def test_a_recent_heartbeat_keeps_it():
    req = ask(alive=time.time() - 20)
    run(gateway.scan())
    assert status(req) == "open" and len(sent()) == 1


def test_an_answered_request_with_a_silent_hook_is_stale():
    req = ask(alive=time.time() - 40)
    decisions.transition(req["id"], ("open",), "answered", answer={"Which?": "Alpha"})
    run(gateway.scan())
    assert status(req) == "stale"


# -- permission prompts: Hermes's approval card -----------------------------------------------------

def approve(rid, choice, reason=None):
    from tools import approval
    return approval.resolve_gateway_approval(f"muster:{rid}", choice, reason=reason)


async def card_for(req):
    await gateway.scan()
    await until(lambda: any(m.get("command") for m in telegram.T.adapter.sent))
    return telegram.T.adapter.sent[-1]


def test_a_permission_prompt_is_hermess_approval_card():
    async def go():
        req = permission()
        card = await card_for(req)
        assert card["command"] == "touch x" and card["session"] == f"muster:{req['id']}"
        assert card["permanent"] is False and card["session_button"] is False  # Allow once and Deny only
        assert card["text"].startswith("Permission needed · b: Create x.") and "reply to this message" in card["text"]
        assert decisions.load(req["id"])["presented"]["messages"] == {"0": [card["mid"]]}
    run(go())


@pytest.mark.parametrize("choice,reason,expect", [
    ("once", None, {"decision": "allow"}),
    ("session", None, {"decision": "deny"}),  # not offered: a tier the card lacks fails closed
    ("always", None, {"decision": "deny"}),
    ("deny", None, {"decision": "deny"}),
    ("deny", "use make", {"decision": "deny", "message": "use make"}),
])
def test_approval_choices_become_answers(choice, reason, expect):
    async def go():
        req = permission()
        await card_for(req)
        approve(req["id"], choice, reason)
        await until(lambda: status(req) == "answered")
        return decisions.load(req["id"])["answer"]

    assert run(go()) == expect


def test_hermess_approval_timeout_denies(monkeypatch):
    from tools import approval_gateway_wait
    monkeypatch.setattr(approval_gateway_wait, "TIMEOUT", 0.05)

    async def go():
        req = permission()
        await card_for(req)
        await until(lambda: status(req) == "answered")
        answer = decisions.load(req["id"])["answer"]
        assert answer["decision"] == "deny" and answer["timeout"] is True and answer["message"]
    run(go())


def test_a_reply_to_the_card_denies_with_its_text():
    async def go():
        req = permission()
        card = await card_for(req)
        got = await gateway.on_dispatch(event=fh.event("please use make", DM, DM, reply=card["mid"]),
                                        gateway=fh.Gateway())
        assert got == {"action": "skip"}
        await until(lambda: status(req) == "answered")
        assert decisions.load(req["id"])["answer"] == {"decision": "deny", "message": "please use make"}
        assert telegram.T.adapter.edits[-1] == (DM, card["mid"], "Received ✓: please use make")
    run(go())


def test_a_reply_to_the_card_from_an_unauthorized_user_does_nothing():
    async def go():
        req = permission()
        card = await card_for(req)
        got = await gateway.on_dispatch(event=fh.event("no", 999, DM, reply=card["mid"]), gateway=fh.Gateway())
        assert got is None and status(req) == "open" and telegram.T.approvals == {req["id"]}
    run(go())


def test_a_failed_card_send_is_retried_with_backoff():
    async def go():
        req = permission()
        telegram.T.adapter.fail_sends = 1
        await gateway.scan()
        await until(lambda: req["id"] in gateway.S.retry)
        assert status(req) == "open" and req["id"] not in telegram.T.approvals
        gateway.S.retry[req["id"]] = (0, 2)
        await gateway.scan()
        await until(lambda: req["id"] not in gateway.S.retry and telegram.T.approvals)
        assert len(sent()) == 1
    run(go())


def test_an_ended_request_withdraws_its_card():
    async def go():
        req = permission()
        await card_for(req)
        decisions.transition(req["id"], ("open",), "stale", outcome="Answered in the pane")
        await gateway.scan()
        assert telegram.T.approvals == set()
        await asyncio.sleep(0.2)
        assert status(req) == "stale" and "answer" not in decisions.load(req["id"])  # a withdrawn wait answers nothing
    run(go())


def test_a_silent_hook_stales_a_permission_request_and_withdraws_it():
    async def go():
        req = permission()
        await card_for(req)
        decisions.update(req["id"], alive=time.time() - 40)
        await gateway.scan()
        assert status(req) == "stale" and telegram.T.approvals == set()
    run(go())


# -- registration -----------------------------------------------------------------------------------

def load_root(monkeypatch):
    name = "hermes_plugins.muster_gw"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "hermes_plugins", types.ModuleType("hermes_plugins"))
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_register_adds_the_gateway_pieces_only_when_hermes_has_them(monkeypatch):
    module = load_root(monkeypatch)
    calls = []

    class Old:
        def get_config(self, key, default):
            return default

        def register_cli_command(self, **kw):
            pass

        def register_skill(self, name, path, description=""):
            pass

    class New(Old):
        def register_telegram_handler(self, factory):
            calls.append(("telegram", factory.__name__))

        def register_hook(self, name, cb):
            calls.append((name, cb.__name__))

    try:
        module.register(Old())
        assert calls == []
        ctx = New()
        module.register(ctx)
        assert calls == [("telegram", "telegram_factory"), ("pre_gateway_dispatch", "on_dispatch")]
        gw = sys.modules["hermes_plugins.muster_gw.muster.gateway"]
        hub = sys.modules["hermes_plugins.muster_gw.muster.channels.shared"]
        assert hub.CTX is ctx and gw.S.task is None  # nothing ran at register time
    finally:
        for key in [k for k in sys.modules if k.startswith("hermes_plugins.muster_gw.")]:
            sys.modules.pop(key, None)


def test_plugin_yaml_discloses_the_dispatch_hook():
    assert "pre_gateway_dispatch" in (ROOT / "plugin.yaml").read_text()


# -- executing build and feedback requests (task 6) -----------------------------------------------

def tap_request(kind, status="answered", **fields):
    req = decisions.create(kind, "led1", **{"questions": [{"text": "Q", "header": "", "multi": False, "options": []}],
                                            "choices": [["A"]], "actions": ["nothing"], "run": {"branch": "b"}, **fields})
    if status != "open":
        decisions.transition(req["id"], ("open",), status, executing_boot=fields.get("executing_boot"))
    return req["id"]


def test_an_answered_build_feedback_or_design_request_is_executed_on_the_pool_with_this_boot(monkeypatch):
    calls = []
    monkeypatch.setattr(decisions, "execute", lambda rid, boot="": calls.append((rid, boot, threading.current_thread())))
    q = tap_request("question", "open")
    b, f, d = tap_request("build"), tap_request("feedback"), tap_request("design")

    async def go():
        for rid in (q, b, f, d):
            gateway.on_answered(decisions.load(rid))
        await until(lambda: len(calls) == 3)

    run(go())
    assert sorted(c[0] for c in calls) == sorted([b, f, d]) and all(c[1] == gateway.BOOT for c in calls)
    assert all(c[2] is not threading.main_thread() for c in calls)


def test_a_tap_on_a_build_request_runs_the_action_and_settles_it(hermes):
    labels = ["Merge (squash)", "Do nothing"]
    req = decisions.create("build", "led1", run={"branch": "b"}, actions=["merge", "nothing"], choices=[labels],
                           questions=[{"text": "R", "header": "Build", "multi": False,
                                       "options": [{"label": x, "description": ""} for x in labels]}])

    async def go():
        await gateway.answered(req["id"], 0, "Do nothing")
        await until(lambda: decisions.load(req["id"])["status"] == "done")

    run(go())
    assert decisions.load(req["id"])["outcome"].startswith("⏸ Left open\n")


def test_an_ended_decision_card_on_telegram_reads_as_its_outcome_first(hermes):
    req = decisions.create("build", "led1", run={"branch": "b"}, pr="https://github.com/o/r/pull/9", status="done",
                           outcome=decisions.DELIVERED, resolved="Fix the null case.",
                           presented={"platform": "telegram", "messages": {"0": ["m1"]}}, questions=[{"text": "R"}])
    assert run(telegram.outcome(req)) is True
    text = telegram.T.adapter.edits[-1][2]
    assert text.startswith("↩️ Sent back · PR #9 · ") and "Your feedback reached the builder; revisions are pending." \
        in text and text.endswith("> Fix the null case.") and "**" not in text


def test_build_free_text_is_sent_back_with_the_humans_words_and_no_second_confirmation(hermes, monkeypatch):
    def no_run(ledger):
        raise core.CommandError(f"no run found for card {ledger}")

    monkeypatch.setattr(decisions, "run_of", no_run)
    monkeypatch.setattr(decisions, "view", lambda url, fields, env=None: {})  # no gh here: the reviewed PR, as read
    monkeypatch.setattr(decisions, "refusal", lambda run, url, pr, head: None)
    q = {"text": "R", "header": "Build", "multi": False, "options": [{"label": "Do nothing", "description": ""}]}
    req = decisions.create("build", "led1", run={"repo": "o/r", "branch": "b", "pane": "p1"}, actions=["nothing"],
                           choices=[["Do nothing"]], questions=[q], head="h" * 40, base="main", pr="u", cycle=1,
                           feedback="Fix it.", hints=[])

    async def go():
        await gateway.answered(req["id"], 0, "please also add docs")
        await until(lambda: decisions.load(req["id"])["status"] == "done")
        await gateway.scan()

    run(go())
    (fb,) = decisions.for_ledger("led1", "feedback")
    assert fb["feedback"] == "Fix it.\n\nAdditional instructions from the human:\nplease also add docs"
    # sent at once (this run has no record, so the send fails visibly), never offered again to confirm
    assert fb["status"] == "failed" and decisions.load(req["id"])["outcome"].startswith("⚠️ Needs attention\n")
    assert sent() == []


def test_a_scan_retires_an_open_retry_whose_send_arrived_late_and_presents_nothing(tmp_path):
    launcher.prompt_seen(tmp_path, {"prompt": "the revision"})
    rid = tap_request("feedback", "open", prompt_sha=hashlib.sha256(b"the revision").hexdigest(),
                      run={"branch": "b", "evidence_dir": str(tmp_path)})
    waiting = tap_request("feedback", "open", prompt_sha="0" * 64, run={"branch": "b", "evidence_dir": str(tmp_path)})
    decisions.update(rid, presented={"boot": gateway.BOOT})  # already shown this boot: reconcile still runs

    async def go():
        await asyncio.gather(gateway.scan(), gateway.scan())

    run(go())
    assert decisions.load(rid)["status"] == "done" and decisions.load(rid)["outcome"] == decisions.DELIVERED
    assert decisions.load(waiting)["status"] == "open" and len(sent()) == 1  # only the unseen retry is shown


def test_a_retry_tapped_while_it_is_reconciled_is_not_presented(monkeypatch):
    rid = tap_request("feedback", "open", prompt_sha="0" * 64, run={"branch": "b", "evidence_dir": "/nowhere"})
    monkeypatch.setattr(decisions, "execute", lambda rid, boot="": None)

    def tapped(req):  # the human's tap lands while the scan is in reconcile's thread
        decisions.transition(req["id"], ("open",), "answered", answer={"action": "send"})
        return False

    monkeypatch.setattr(decisions, "reconcile", tapped)
    run(gateway.scan())
    assert decisions.load(rid)["status"] == "answered" and sent() == []


def test_the_first_scan_recovers_executing_requests_of_an_older_boot_once(monkeypatch):
    rec, ex = [], []
    monkeypatch.setattr(decisions, "recover", lambda req: rec.append(req["id"]))
    monkeypatch.setattr(decisions, "execute", lambda rid, boot="": ex.append(rid))
    old = tap_request("build", "executing", executing_boot="older")
    mine = tap_request("feedback", "executing", executing_boot=gateway.BOOT)
    pending = tap_request("feedback", "answered")
    asked = tap_request("question", "answered")
    design = tap_request("design", "executing", executing_boot="older")

    async def go():
        await gateway.scan()
        await gateway.scan()
        await until(lambda: len(rec) == 2 and ex)

    run(go())
    assert sorted(rec) == sorted([old, design]) and ex == [pending] and mine not in rec + ex and asked not in rec + ex


# -- run topics (#24) -----------------------------------------------------------------------------

from muster import conversation  # noqa: E402

GROUP_CHAT = "-100123"


@pytest.fixture
def forum(monkeypatch):
    """Topics on in a forum group; the bot may manage topics; one clock for topic timing."""
    for key, value in (("notify_topics", True), ("notify_chat_id", GROUP_CHAT), ("notify_user_id", "42")):
        monkeypatch.setitem(config.settings, key, value)
    bot = fh.Bot()
    telegram.T.bot = telegram.T.adapter.bot = bot
    subs = fh.Subs()
    monkeypatch.setattr(core, "kanban", subs)
    clock = [1000.0]
    monkeypatch.setattr(shared, "now", lambda: clock[0])
    monkeypatch.setattr(shared, "clock", lambda: clock[0])
    return types.SimpleNamespace(bot=bot, subs=subs, clock=clock, adapter=telegram.T.adapter)


def requested(ledger="led1", name="r#7 Fix it", **fields):
    """What the launch writes before it waits (conversation.request)."""
    return conversation.update(ledger, **{
        "platform": "telegram", "chat_id": GROUP_CHAT, "user_id": "42", "thread_id": None, "name": name,
        "state": "pending", "attempts": 0, "since": 0, "previous": [], "cards": [], "noticed": False, "why": "",
        **fields})


def opened(forum, ledger="led1", name="r#7 Fix it", cards=()):
    """A run whose topic exists, with its cards subscribed there."""
    thread = forum.bot.make_topic(GROUP_CHAT, name)
    requested(ledger, name, state="open", thread_id=str(thread), attempts=1)
    for card in cards:
        core.subscribe(card, ledger)
    return str(thread)


def found_gone(forum, ledger="led1"):
    """The run's topic is found deleted (a prompt that fell back to General was tapped; see the tests below)."""
    run(telegram.gone(ledger, conversation.load(ledger)["thread_id"]))


def tick(forum, seconds, n=1):
    for _ in range(n):
        forum.clock[0] += seconds
        run(gateway.scan())


def test_the_gateway_makes_the_topic_a_launch_asked_for(forum):
    requested()
    tick(forum, 0)
    ref = conversation.load("led1")
    assert ref["state"] == "open" and forum.bot.topics[int(ref["thread_id"])] == "r#7 Fix it"
    assert ref["attempts"] == 1


@pytest.mark.parametrize("change, why", [
    ({"forum": False}, "Topics are not enabled"),
    ({"member": types.SimpleNamespace(status="administrator", can_manage_topics=False)}, "Manage Topics"),
    ({"member": types.SimpleNamespace(status="member")}, "Manage Topics"),
])
def test_missing_prerequisites_fall_back_to_the_main_chat_with_one_notice(forum, change, why):
    for key, value in change.items():
        setattr(forum.bot, key, value)
    requested()
    tick(forum, 0, n=3)
    ref = conversation.load("led1")
    assert ref["state"] == "fallback" and why in ref["why"] and not forum.bot.topics
    assert [(c, t) for c, _, t in forum.adapter.notes] == [(GROUP_CHAT, None)]
    assert why in forum.adapter.notes[0][1] and "stays in the main chat" in forum.adapter.notes[0][1]
    assert conversation.target("led1")["thread_id"] is None


def test_a_lost_create_is_adopted_from_telegrams_notice_and_never_made_twice(forum):
    forum.adapter.creates = ["lost"]
    requested()
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "creating" and list(forum.bot.topics) == [41]
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, 41, "r#7 Fix it"), None))
    assert conversation.load("led1")["state"] == "open" and conversation.load("led1")["thread_id"] == "41"
    tick(forum, 11, n=3)
    assert list(forum.bot.topics) == [41]  # no second create
    # a late notice of another topic of that name: a duplicate, said so and closed
    forum.bot.make_topic(GROUP_CHAT, "r#7 Fix it")
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, 42, "r#7 Fix it"), None))
    assert (GROUP_CHAT, 'Duplicate topic, not used; this run is in the topic "r#7 Fix it".', "42") in forum.adapter.notes
    assert 42 in forum.bot.closed and 41 not in forum.bot.closed
    assert conversation.load("led1")["thread_id"] == "41"


def test_a_notice_from_someone_else_or_another_chat_adopts_nothing(forum):
    forum.adapter.creates = ["lost"]
    requested()
    tick(forum, 0)
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, 41, "r#7 Fix it", sender=42), None))  # the human made it
    run(telegram.topic_created(fh.topic_notice("-100999", 41, "r#7 Fix it"), None))
    assert conversation.load("led1")["state"] == "creating"


def test_a_lost_create_with_no_notice_is_never_retried_and_the_run_uses_the_main_chat(forum):
    forum.adapter.creates = ["lost"]
    requested()
    tick(forum, 0)
    tick(forum, 11)
    tick(forum, 8)  # 19 s: still waiting for the notice, still one create
    assert conversation.load("led1")["state"] == "creating" and len(forum.bot.topics) == 1
    tick(forum, 1)
    ref = conversation.load("led1")
    assert ref["state"] == "fallback" and "not retried" in ref["why"]
    tick(forum, 11, n=3)
    assert len(forum.bot.topics) == 1  # never a second topic
    assert [n[1] for n in forum.adapter.notes if "stays in the main chat" in n[1]] and all(
        n[2] is None for n in forum.adapter.notes)
    # the notice turns up after all: that topic is closed as unused, the run stays in the main chat
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, 41, "r#7 Fix it"), None))
    assert 41 in forum.bot.closed and conversation.load("led1")["state"] == "fallback"


def test_a_restart_during_a_create_waits_for_the_notice_and_does_not_create_again(forum):
    requested(state="creating", attempts=1, asked=True, since=1000.0)  # the gateway died inside the create call
    tick(forum, 5)
    assert not forum.bot.topics and conversation.load("led1")["state"] == "creating"
    tick(forum, 16)
    assert not forum.bot.topics and conversation.load("led1")["state"] == "fallback"


def test_a_check_that_cannot_run_is_tried_three_times_without_creating(forum, monkeypatch):
    async def down(chat):
        raise Exception("Timed out")
    monkeypatch.setattr(forum.bot, "get_chat", down)
    requested()
    tick(forum, 11, n=4)
    ref = conversation.load("led1")
    assert ref["state"] == "fallback" and ref["attempts"] == 3 and not forum.bot.topics


def test_the_launchs_fallback_beats_a_late_create(forum):
    forum.adapter.creates = ["lost"]
    requested()
    tick(forum, 0)
    conversation.swap("led1", ("pending", "creating"), state="fallback", why="no topic within 45 s")  # the tick
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, 41, "r#7 Fix it"), None))
    assert conversation.load("led1")["state"] == "fallback" and 41 in forum.bot.closed
    assert (GROUP_CHAT, "Duplicate topic, not used; this run is in the main chat.", "41") in forum.adapter.notes


def test_a_finished_run_closes_its_topic_once_and_keeps_it(forum):
    thread = opened(forum)
    conversation.finish("led1")
    tick(forum, 0, n=3)
    assert conversation.load("led1")["state"] == "closed" and int(thread) in forum.bot.closed
    assert int(thread) in forum.bot.topics  # never deleted
    assert [n for n in forum.adapter.notes if "Finished" in n[1]] == [
        (GROUP_CHAT, "Finished. This topic is now closed.",
         thread)]


def test_a_request_for_a_closed_run_reopens_its_topic_first(forum):
    thread = opened(forum)
    conversation.finish("led1")
    tick(forum, 0)
    ask()
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "open" and int(thread) not in forum.bot.closed
    assert [s["thread"] for s in forum.adapter.sent] == [thread]


def test_a_deleted_topic_then_an_agent_prompt_lands_in_a_new_topic(forum):
    old = opened(forum, cards=["led1", "w1"])
    forum.bot.deleted.add(int(old))
    req = ask()
    tick(forum, 0)  # nothing probes the topic: Hermes resends the prompt to General
    assert [s["thread"] for s in forum.adapter.sent] == [None] and conversation.load("led1")["state"] == "open"
    update = fh.update(42, GROUP_CHAT, data=f"cl:mu{req['id']}q0:0", thread=None, mid=forum.adapter.sent[0]["mid"])
    with pytest.raises(fh.ApplicationHandlerStop):
        run(telegram.guard(update, None))  # its newest prompt landed outside the topic: the topic is gone
    assert conversation.load("led1")["state"] == "creating" and status(req) == "open"
    tick(forum, 0, n=3)  # made, then the cards and the request move
    ref = conversation.load("led1")
    new = ref["thread_id"]
    assert ref["state"] == "open" and new != old and ref["previous"] == [old] and ref["repaired"] == new
    assert [s["thread"] for s in forum.adapter.sent] == [None, new]
    assert forum.adapter.sent[1]["cid"] == f"mu{req['id']}q0"
    assert forum.subs.deliver("led1") == [new] and forum.subs.deliver("w1") == [new]
    assert (GROUP_CHAT, "The previous topic was deleted; this run continues here.", new) in forum.adapter.notes


def test_a_deleted_topic_then_a_coordinator_wake_lands_in_the_new_topic(forum):
    old = opened(forum, cards=["led1"])
    forum.bot.deleted.add(int(old))
    tick(forum, 31, n=3)
    assert conversation.load("led1")["state"] == "open" and not forum.bot.calls  # nothing probes the topic
    found_gone(forum)
    assert conversation.load("led1")["state"] == "creating"
    tick(forum, 0, n=2)
    new = conversation.load("led1")["thread_id"]
    # the ledger completes (hook done): Hermes's notifier pings and wakes the coordinator per subscription
    assert new != old and forum.subs.deliver("led1") == [new]


def test_a_recreate_that_fails_moves_the_run_to_the_main_chat(forum):
    old = opened(forum, cards=["led1"])
    forum.bot.deleted.add(int(old))
    forum.bot.forum = False  # Topics turned off meanwhile
    found_gone(forum)
    tick(forum, 0, n=3)
    ref = conversation.load("led1")
    assert ref["state"] == "fallback" and ref["repaired"] == "main"
    assert forum.subs.deliver("led1") == [""]


def test_after_a_restart_the_topic_is_reused(forum):
    thread = opened(forum)
    ask()
    gateway.S, telegram.T = gateway.State(), telegram.State()
    shared.checked.clear()
    telegram.T.adapter, telegram.T.bot = forum.adapter, forum.bot
    tick(forum, 0)
    assert [s["thread"] for s in forum.adapter.sent] == [thread] and list(forum.bot.topics) == [int(thread)]


def test_two_runs_never_cross_route(forum, hermes):
    a_thread, b_thread = opened(forum, "ledA", "r#1 A"), opened(forum, "ledB", "r#2 B")
    a, b = ask(ledger="ledA"), ask(ledger="ledB")
    tick(forum, 0)
    sent = {s["cid"]: s for s in forum.adapter.sent}
    assert sent[f"mu{a['id']}q0"]["thread"] == a_thread and sent[f"mu{b['id']}q0"]["thread"] == b_thread
    # a tap on A's message seen outside A's topic is refused, and A is presented again in its topic
    update = fh.update(42, GROUP_CHAT, data=f"cl:mu{a['id']}q0:0", thread=int(b_thread))
    with pytest.raises(fh.ApplicationHandlerStop):
        run(telegram.guard(update, None))
    assert update.callback_query.answers == ["Moved: answer in the run's topic"]
    # both wait for typed text after Other: text typed in B's topic answers B only
    tick(forum, 0)
    hermes.mark_awaiting_text(f"mu{a['id']}q0")
    hermes.mark_awaiting_text(f"mu{b['id']}q0")

    async def typed():
        got = await gateway.on_dispatch(event=fh.event("for B", "42", GROUP_CHAT, thread=b_thread),
                                        gateway=fh.Gateway())
        await until(lambda: status(b) == "answered")
        return got

    assert run(typed()) == {"action": "skip"}
    assert decisions.load(b["id"])["answer"] == {"Which?": "for B"} and status(a) == "open"
    # a reply to A's message inside A's topic answers A; the same reply from B's topic does not
    a_mid = [s["mid"] for s in forum.adapter.sent if s["cid"] == f"mu{a['id']}q0"][-1]
    assert run(gateway.on_dispatch(event=fh.event("x", "42", GROUP_CHAT, reply=a_mid, thread=b_thread),
                                   gateway=fh.Gateway())) is None

    async def replied():
        got = await gateway.on_dispatch(event=fh.event("Beta", "42", GROUP_CHAT, reply=a_mid, thread=a_thread),
                                        gateway=fh.Gateway())
        await until(lambda: status(a) == "answered")
        return got

    assert run(replied()) == {"action": "skip"}


def test_every_kind_of_request_follows_the_runs_topic(forum):
    thread = opened(forum)
    permission()
    decisions.create("build", "led1", **{**dict(actions=["merge", "send-back", "nothing"], recommended="merge",
                                                head="a" * 40, pr="https://github.com/o/r/pull/1", cycle=1,
                                                choices=[["Merge", "Send back", "Do nothing"]]),
                                         "questions": [question("Review", ("Merge", "Send back", "Do nothing"))],
                                         "run": {"repo": "o/r", "issue": 7}})
    decisions.create("feedback", "led1", actions=["send", "cancel"], choices=[["Send feedback to builder", "Don't send"]],
                     questions=[question("Fix X", ("Send feedback to builder", "Don't send"))], run={"repo": "o/r", "issue": 7})

    async def go():
        await gateway.scan()
        await until(lambda: len(forum.adapter.sent) == 3)

    run(go())
    assert sorted(s["thread"] for s in forum.adapter.sent) == [thread] * 3


def test_with_topics_off_nothing_carries_a_thread(hermes):
    ask()
    run(gateway.scan())
    assert telegram.T.adapter.sent[0]["thread"] is None and not (config.data_dir() / "runs").exists()


def test_an_open_topic_is_never_probed_or_reopened(forum):
    opened(forum)
    ask()
    tick(forum, 31, n=4)
    gateway.S, telegram.T = gateway.State(), telegram.State()  # a restart
    shared.checked.clear()
    telegram.T.adapter, telegram.T.bot = forum.adapter, forum.bot
    tick(forum, 31, n=2)
    assert not [c for c in forum.bot.calls if c[0] == "reopen"] and conversation.load("led1")["state"] == "open"


def test_a_tap_on_an_older_prompt_outside_the_topic_does_not_mark_it_gone(forum):
    thread = opened(forum)
    req = ask()
    tick(forum, 0)
    update = fh.update(42, GROUP_CHAT, data=f"cl:mu{req['id']}q0:0", thread=None, mid="1")  # not its newest message
    with pytest.raises(fh.ApplicationHandlerStop):
        run(telegram.guard(update, None))
    assert update.callback_query.answers == ["Moved: answer in the run's topic"]
    ref = conversation.load("led1")
    assert ref["state"] == "open" and ref["thread_id"] == thread


def test_an_approval_card_that_fell_back_to_general_marks_the_topic_gone(forum):
    old = opened(forum, cards=["led1"])
    forum.bot.deleted.add(int(old))
    req = permission()

    async def go():
        await gateway.scan()
        await until(lambda: forum.adapter.sent)

    run(go())
    sent = forum.adapter.sent[0]
    assert sent["thread"] is None
    update = fh.update(42, GROUP_CHAT, data=f"ea:once:{sent['mid']}", thread=None, mid=sent["mid"])
    with pytest.raises(fh.ApplicationHandlerStop):
        run(telegram.approval_guard(update, None))
    assert status(req) == "open" and conversation.load("led1")["state"] == "creating"


def test_while_a_deleted_topic_is_remade_no_tap_is_taken_anywhere(forum):
    old = opened(forum, cards=["led1"])
    req = ask()
    tick(forum, 0)
    forum.bot.deleted.add(int(old))
    found_gone(forum)
    assert conversation.load("led1")["state"] == "creating"
    for thread in (None, int(old)):
        update = fh.update(42, GROUP_CHAT, data=f"cl:mu{req['id']}q0:0", thread=thread)
        with pytest.raises(fh.ApplicationHandlerStop):
            run(telegram.guard(update, None))
    assert status(req) == "open"


def test_a_recreate_that_falls_back_presents_the_prompt_once_in_the_main_chat(forum):
    old = opened(forum, cards=["led1"])
    forum.bot.deleted.add(int(old))
    forum.bot.forum = False
    found_gone(forum)
    ask()
    tick(forum, 11, n=6)
    assert [s["thread"] for s in forum.adapter.sent] == [None]
    assert conversation.load("led1")["repaired"] == "main"


def test_an_approval_card_tapped_outside_its_topic_is_refused(forum):
    opened(forum)
    req = permission()

    async def go():
        await gateway.scan()
        await until(lambda: forum.adapter.sent)

    run(go())
    approval_id = int(forum.adapter.sent[0]["mid"])
    update = fh.update(42, GROUP_CHAT, data=f"ea:once:{approval_id}", thread=None)
    with pytest.raises(fh.ApplicationHandlerStop):
        run(telegram.approval_guard(update, None))
    assert update.callback_query.answers == ["Moved: answer in the run's topic"] and status(req) == "open"


def test_someone_elses_tap_cannot_make_a_prompt_present_again(forum):
    opened(forum)
    req = ask()
    tick(forum, 0)
    update = fh.update(99, GROUP_CHAT, data=f"cl:mu{req['id']}q0:0", thread=None)
    with pytest.raises(fh.ApplicationHandlerStop):
        run(telegram.guard(update, None))
    assert update.callback_query.answers == ["Not authorized"]
    assert req["id"] in telegram.T.shown and decisions.load(req["id"])["presented"].get("boot") == gateway.BOOT


def test_a_card_whose_subscription_does_not_move_is_retried_before_the_repair_is_done(forum):
    old = opened(forum, cards=["led1", "w1"])
    forum.bot.deleted.add(int(old))
    forum.subs.fail["led1"] = 1  # the ledger: the card the coordinator's review wake comes from
    found_gone(forum)
    tick(forum, 0)  # a new topic
    new = conversation.load("led1")["thread_id"]
    tick(forum, 0)  # the repair: w1 moves, the ledger's subscribe fails
    ref = conversation.load("led1")
    assert ref["moved"] == new and ref["migrating"] == ["led1"] and ref.get("repaired") is None
    assert forum.subs.deliver("w1") == [new] and forum.subs.deliver("led1") == [old]  # not claimed as moved
    assert "not moved to" in core.log_path("gateway").read_text()
    tick(forum, 1)  # inside the backoff: nothing tried
    assert conversation.load("led1")["migrating"] == ["led1"]
    tick(forum, 2)
    ref = conversation.load("led1")
    assert ref["migrating"] == [] and ref["repaired"] == new
    # the ledger completes: Hermes's notifier pings, and wakes the coordinator, in the new topic only
    assert forum.subs.deliver("led1") == [new] and forum.subs.deliver("w1") == [new]


def test_a_card_left_subscribed_at_the_old_topic_is_not_counted_as_moved(forum, monkeypatch):
    old = opened(forum, cards=["led1"])
    forum.bot.deleted.add(int(old))
    real = forum.subs.__class__.__call__

    def stuck(self, *argv):  # an unsubscribe that reports success but leaves the row
        if argv[0] == "notify-unsubscribe":
            self.calls.append(argv)
            return ""
        return real(self, *argv)
    monkeypatch.setattr(forum.subs.__class__, "__call__", stuck)
    found_gone(forum)
    tick(forum, 0, n=2)
    ref = conversation.load("led1")
    assert ref["migrating"] == ["led1"] and ref.get("repaired") is None


def test_topic_notices_bind_to_the_run_not_its_readable_title(forum):
    a_name = conversation.name("o/r", "Fix it", issue=7)
    b_name = a_name + " (2)"  # conversation.unique's second name
    forum.adapter.creates = ["lost", "lost"]
    requested("ledA", a_name)
    requested("ledB", b_name)
    tick(forum, 0)
    a_thread, b_thread = (t for t, n in forum.bot.topics.items() if n == a_name), \
        (t for t, n in forum.bot.topics.items() if n == b_name)
    a_thread, b_thread = next(a_thread), next(b_thread)
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, b_thread, b_name), None))  # B's notice first
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, a_thread, a_name), None))
    assert conversation.load("ledA")["thread_id"] == str(a_thread)
    assert conversation.load("ledB")["thread_id"] == str(b_thread)
    # delayed duplicates: a repeat of A's own notice changes nothing; another topic named for A is closed
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, a_thread, a_name), None))
    extra = forum.bot.make_topic(GROUP_CHAT, a_name)
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, extra, a_name), None))
    assert forum.bot.closed == {extra}
    assert conversation.load("ledA")["thread_id"] == str(a_thread)
    assert conversation.load("ledB")["thread_id"] == str(b_thread)


def test_a_notice_whose_name_matches_two_runs_is_ignored(forum):
    forum.adapter.creates = ["lost", "lost"]
    requested("ledA", "same")
    requested("ledB", "same")  # cannot happen with ledger-tagged names; refused if it ever does
    tick(forum, 0)
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, 41, "same"), None))
    assert conversation.load("ledA")["state"] == conversation.load("ledB")["state"] == "creating"
    assert not forum.bot.closed and "ambiguous, ignored" in core.log_path("gateway").read_text()


def test_a_late_notice_of_the_deleted_previous_topic_is_never_adopted(forum):
    old = opened(forum, cards=["led1"])
    name = conversation.load("led1")["name"]
    forum.bot.deleted.add(int(old))
    forum.adapter.creates = ["lost"]
    found_gone(forum)
    tick(forum, 0)  # the recreate's reply is lost
    new = max(forum.bot.topics)
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, int(old), name), None))  # the old topic's notice, late
    ref = conversation.load("led1")
    assert ref["state"] == "creating" and ref["thread_id"] is None and not forum.bot.closed
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, new, name), None))
    assert conversation.load("led1")["thread_id"] == str(new) and not forum.bot.closed
    assert run(telegram.settle("led1", old)) is None and conversation.load("led1")["thread_id"] == str(new)


def test_a_card_that_never_moves_does_not_stop_a_second_deletion(forum):
    old = opened(forum, cards=["led1", "w1"])
    forum.bot.deleted.add(int(old))
    forum.subs.fail["w1"] = 10 ** 6
    found_gone(forum)
    tick(forum, 0, n=2)
    first = conversation.load("led1")["thread_id"]
    assert conversation.load("led1")["migrating"] == ["w1"]
    forum.bot.deleted.add(int(first))  # the new topic is deleted too
    found_gone(forum)
    ref = conversation.load("led1")
    assert ref["state"] == "creating" and ref["previous"] == [old, first] and ref["migrate_at"] == 0
    tick(forum, 0, n=2)
    assert conversation.load("led1")["moved"] == conversation.load("led1")["thread_id"]  # prompts move at once


def test_a_card_deleted_from_the_board_counts_as_moved(forum):
    old = opened(forum, cards=["led1", "w1"])
    forum.bot.deleted.add(int(old))
    forum.subs.missing.add("w1")
    found_gone(forum)
    tick(forum, 0, n=2)
    ref = conversation.load("led1")
    assert ref["migrating"] == [] and ref["repaired"] == ref["thread_id"]
    assert "w1 is gone from the board" in core.log_path("gateway").read_text()


def test_a_move_to_the_main_chat_must_read_back_there(forum, monkeypatch):
    old = opened(forum, cards=["led1"])
    forum.bot.deleted.add(int(old))
    forum.bot.forum = False  # the recreate falls back to the main chat
    real = forum.subs.__class__.__call__

    def silent(self, *argv):  # a subscribe that does nothing and a read-back that still passes
        if argv[0] == "notify-subscribe" and "--thread-id" not in argv:
            self.calls.append(argv)
            return ""
        return real(self, *argv)
    monkeypatch.setattr(forum.subs.__class__, "__call__", silent)
    monkeypatch.setattr(core, "subscribe", lambda card, ledger=None: real(forum.subs, "notify-list", card))
    found_gone(forum)
    tick(forum, 0, n=3)
    ref = conversation.load("led1")
    assert ref["state"] == "fallback" and ref["migrating"] == ["led1"] and ref.get("repaired") is None



# -- retired runs (issue #28) ---------------------------------------------------------------------

def test_an_archived_run_whose_topic_was_deleted_by_hand_never_gets_a_new_one(forum):
    thread = opened(forum, cards=["led1"])
    forum.subs.status["led1"] = "archived"
    forum.bot.deleted.add(int(thread))
    tick(forum, 31, n=4)
    ref = conversation.load("led1")
    assert ref["state"] == "deleted" and ref["thread_id"] == thread
    assert list(forum.bot.topics) == [int(thread)]  # no replacement was made
    assert ("reopen", GROUP_CHAT, int(thread)) not in forum.bot.calls  # never probed once retired
    assert conversation.active() == []


def test_a_ledger_missing_from_the_board_deletes_nothing_and_makes_nothing(forum):
    """Only an explicit "archived" retires a run: a gateway reading the wrong board must not delete topics."""
    thread = opened(forum)
    forum.subs.missing.add("led1")
    tick(forum, 31, n=2)
    assert conversation.load("led1")["state"] == "open" and int(thread) in forum.bot.topics
    forum.bot.deleted.add(int(thread))
    found_gone(forum)
    tick(forum, 31, n=3)
    assert conversation.load("led1")["state"] == "creating" and list(forum.bot.topics) == [int(thread)]


def test_an_archived_run_has_its_topic_deleted_and_its_prompts_staled(forum):
    thread = opened(forum)
    req = ask()
    tick(forum, 0)
    assert [s["thread"] for s in forum.adapter.sent] == [thread]
    forum.subs.status["led1"] = "archived"
    tick(forum, 31, n=2)
    assert conversation.load("led1")["state"] == "deleted" and int(thread) not in forum.bot.topics
    assert decisions.load(req["id"])["status"] == "stale"
    assert decisions.load(req["id"])["outcome"] == "The run is archived"


def test_a_stale_prompt_for_an_archived_closed_run_never_reopens_its_topic(forum):
    thread = opened(forum)
    conversation.finish("led1")
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "closed"
    forum.subs.status["led1"] = "archived"
    req = ask()
    tick(forum, 0, n=3)
    assert forum.adapter.sent == [] and ("reopen", GROUP_CHAT, int(thread)) not in forum.bot.calls
    assert decisions.load(req["id"])["status"] == "stale"
    assert conversation.load("led1")["state"] == "deleted" and int(thread) not in forum.bot.topics


def test_a_done_run_that_is_not_archived_still_reopens_for_a_new_prompt(forum):
    thread = opened(forum)
    conversation.finish("led1")
    tick(forum, 0)
    forum.subs.status["led1"] = "done"  # its pull request is in review: questions still page
    ask()
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "open" and [s["thread"] for s in forum.adapter.sent] == [thread]


def test_a_close_that_fails_stays_closing_and_is_retried(forum, monkeypatch):
    thread = opened(forum)
    real = forum.bot.close_forum_topic
    fails = [1]

    async def flaky(chat, thread):
        if fails[0]:
            fails[0] -= 1
            raise Exception("Timed out")
        return await real(chat, thread)
    monkeypatch.setattr(forum.bot, "close_forum_topic", flaky)
    conversation.finish("led1")
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "closing" and int(thread) not in forum.bot.closed
    tick(forum, 3)
    assert conversation.load("led1")["state"] == "closed" and int(thread) in forum.bot.closed
    assert len([n for n in forum.adapter.notes if "Finished" in n[1]]) == 1


def test_a_delete_that_fails_is_retried(forum, monkeypatch):
    thread = opened(forum)
    real = forum.bot.delete_forum_topic
    fails = [1]

    async def flaky(chat, thread):
        if fails[0]:
            fails[0] -= 1
            raise Exception("Timed out")
        return await real(chat, thread)
    monkeypatch.setattr(forum.bot, "delete_forum_topic", flaky)
    forum.subs.status["led1"] = "archived"
    tick(forum, 31)
    assert conversation.load("led1")["state"] == "retiring" and int(thread) in forum.bot.topics
    tick(forum, 3)
    assert conversation.load("led1")["state"] == "deleted" and int(thread) not in forum.bot.topics


def test_no_topic_is_made_while_the_board_cannot_be_read(forum, monkeypatch):
    real = forum.subs.__class__.__call__

    def locked(self, *argv):
        if argv[0] == "show":
            raise core.CommandError("hermes kanban: exit 1\ndatabase is locked")
        return real(self, *argv)
    monkeypatch.setattr(forum.subs.__class__, "__call__", locked)
    requested()
    tick(forum, 0, n=2)
    assert not forum.bot.topics and conversation.load("led1")["state"] == "pending"


def test_a_topic_made_for_a_run_archived_meanwhile_is_deleted_on_its_notice(forum):
    forum.adapter.creates = ["lost"]
    requested()
    tick(forum, 0)
    forum.subs.status["led1"] = "archived"
    tick(forum, 31)
    assert conversation.load("led1")["state"] == "deleted"
    run(telegram.topic_created(fh.topic_notice(GROUP_CHAT, 41, "r#7 Fix it"), None))
    assert 41 not in forum.bot.topics and conversation.load("led1")["state"] == "deleted"


def test_an_open_runs_prompts_still_arrive_while_the_board_cannot_be_read(forum, monkeypatch):
    thread = opened(forum)
    real = forum.subs.__class__.__call__

    def locked(self, *argv):
        if argv[0] == "show":
            raise core.CommandError("hermes kanban: exit 1\ndatabase is locked")
        return real(self, *argv)
    monkeypatch.setattr(forum.subs.__class__, "__call__", locked)
    ask()
    tick(forum, 0)
    assert [s["thread"] for s in forum.adapter.sent] == [thread]


# -- conversation lifecycle: closing is independent of cleanup; nothing saved reopens a closed topic ---------------

def closed_later(forum, by="muster"):
    """A run's topic closed after every request made so far (the forum clock runs ahead of request times)."""
    thread = opened(forum)
    forum.clock[0] = time.time() + 60
    if by == "muster":
        conversation.finish("led1")
        tick(forum, 0)
    else:
        run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, int(thread)), None))
        forum.bot.closed.add(int(thread))
    return thread


def restart(forum):
    gateway.S, telegram.T = gateway.State(), telegram.State()
    shared.checked.clear()
    telegram.T.adapter, telegram.T.bot = forum.adapter, forum.bot


def test_a_fresh_gateway_with_an_open_topic_and_a_done_ledger_changes_nothing(forum):
    """Repro (a): the old probe reopened a topic on every boot."""
    opened(forum)
    forum.subs.status["led1"] = "done"
    restart(forum)
    tick(forum, 31, n=3)
    assert forum.bot.calls == [] and conversation.load("led1")["state"] == "open"


def test_a_saved_request_routed_after_a_restart_never_reopens_a_closed_topic(forum):
    """Repro (b): a build request saved before the close (PR114's merge) reopened topic 790."""
    thread = opened(forum)
    forum.subs.status["led1"] = "done"
    decisions.create("build", "led1", **{**dict(actions=["merge", "send-back", "nothing"], recommended="merge",
                                                head="a" * 40, pr="https://github.com/o/r/pull/1", cycle=1,
                                                choices=[["Merge", "Send back", "Do nothing"]]),
                                         "questions": [question("Review", ("Merge", "Send back", "Do nothing"))],
                                         "run": {"repo": "o/r", "issue": 7}})
    forum.clock[0] = time.time() + 60
    conversation.finish("led1")
    tick(forum, 0)
    restart(forum)
    tick(forum, 31, n=3)
    ref = conversation.load("led1")
    assert ref["state"] == "closed" and ref["closed_by"] == "muster" and int(thread) in forum.bot.closed
    assert forum.adapter.sent == [] and not [c for c in forum.bot.calls if c[0] == "reopen"]


def test_an_obsolete_merge_approval_is_staled_before_it_is_shown(forum, monkeypatch):
    opened(forum)
    monkeypatch.setattr(decisions, "obsolete", lambda req: "https://github.com/o/r/pull/1 is MERGED, not OPEN")
    req = decisions.create("build", "led1", **{**dict(actions=["merge", "send-back", "nothing"], recommended="merge",
                                                      head="a" * 40, pr="https://github.com/o/r/pull/1", cycle=1,
                                                      choices=[["Merge", "Send back", "Do nothing"]]),
                                               "questions": [question("Review", ("Merge", "Send back", "Do nothing"))],
                                               "run": {"repo": "o/r", "issue": 7}})
    tick(forum, 0)
    assert status(req) == "stale" and "is MERGED" in decisions.load(req["id"])["outcome"]
    assert forum.adapter.sent == []


def test_a_legacy_closed_topic_gets_a_close_time_so_nothing_saved_reopens_it(forum):
    thread = opened(forum)
    forum.bot.closed.add(int(thread))
    conversation.update("led1", state="closed")  # closed before closed_at was kept
    ask()
    forum.clock[0] = time.time() + 60
    tick(forum, 0, n=2)
    assert conversation.load("led1")["closed_at"] == forum.clock[0] and forum.adapter.sent == []
    assert conversation.load("led1")["state"] == "closed"


def test_a_request_made_after_the_close_reopens_the_topic(forum):
    thread = closed_later(forum)
    forum.clock[0] = 1000.0  # requests from here on are newer than the close
    conversation.update("led1", closed_at=1000.0)
    ask()
    tick(forum, 0)
    ref = conversation.load("led1")
    assert ref["state"] == "open" and ref["closed_at"] is None and int(thread) not in forum.bot.closed
    assert [s["thread"] for s in forum.adapter.sent] == [thread]
    assert "reopened for request" in core.log_path("gateway").read_text()


def test_a_reopen_that_finds_no_thread_makes_a_new_topic(forum):
    thread = closed_later(forum)
    forum.bot.deleted.add(int(thread))
    conversation.update("led1", closed_at=1000.0)
    ask()
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "creating" and forum.adapter.sent == []


def test_a_reopen_that_fails_otherwise_stays_closed_and_is_logged(forum, monkeypatch):
    closed_later(forum)
    conversation.update("led1", closed_at=1000.0)

    async def flaky(chat_id, message_thread_id):
        raise Exception("Timed out")
    monkeypatch.setattr(forum.bot, "reopen_forum_topic", flaky)
    ask()
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "closed" and forum.adapter.sent == []
    assert "reopening topic" in core.log_path("gateway").read_text()


def test_a_human_close_is_kept_through_scans_and_restarts_while_work_goes_on(forum):
    req = ask()
    thread = closed_later(forum, by="human")
    ref = conversation.load("led1")
    assert ref["state"] == "closed" and ref["closed_by"] == "human"
    tick(forum, 31, n=3)
    restart(forum)
    tick(forum, 31, n=3)
    assert conversation.load("led1")["state"] == "closed" and forum.adapter.sent == []
    assert not [c for c in forum.bot.calls if c[0] == "reopen"] and int(thread) in forum.bot.closed
    assert status(req) == "open"  # still the agent's question: the pane takes the answer


def test_a_human_reopen_resumes_and_shows_the_prompts_again(forum):
    req = ask()
    thread = closed_later(forum, by="human")
    forum.bot.closed.discard(int(thread))
    run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, int(thread), closed=False), None))
    assert conversation.load("led1")["state"] == "open"
    tick(forum, 0)
    assert [s["thread"] for s in forum.adapter.sent] == [thread] and status(req) == "open"


def test_a_human_close_while_muster_closes_settles_closed(forum, monkeypatch):
    thread = opened(forum)

    async def down(chat, thread):
        raise Exception("Timed out")
    monkeypatch.setattr(forum.bot, "close_forum_topic", down)
    conversation.finish("led1")
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "closing"
    run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, int(thread)), None))
    ref = conversation.load("led1")
    assert ref["state"] == "closed" and ref["closed_by"] == "human" and ref["retry_at"] == 0


def test_the_bots_own_or_another_chats_close_notice_changes_nothing(forum):
    thread = opened(forum)
    run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, int(thread), sender=forum.bot.id), None))
    run(telegram.topic_toggled(fh.topic_toggled("-100999", int(thread)), None))
    run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, 999), None))  # not a run's topic
    assert conversation.load("led1")["state"] == "open"


def test_two_runs_close_and_reopen_independently(forum):
    a = opened(forum, "ledA", "r#1 A")
    opened(forum, "ledB", "r#2 B")
    run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, int(a)), None))
    conversation.finish("ledB")
    tick(forum, 0)
    assert conversation.load("ledA")["closed_by"] == "human" and conversation.load("ledB")["closed_by"] == "muster"
    run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, int(a), closed=False), None))
    assert conversation.load("ledA")["state"] == "open" and conversation.load("ledB")["state"] == "closed"


def test_a_close_that_fails_never_claims_closed_and_backs_off(forum, monkeypatch):
    thread = opened(forum)

    async def down(chat, thread):
        raise Exception("Timed out")
    monkeypatch.setattr(forum.bot, "close_forum_topic", down)
    conversation.finish("led1")
    for _ in range(12):
        tick(forum, 3600)
    ref = conversation.load("led1")
    assert ref["state"] == "closing" and ref["retry_delay"] == shared.RETRY_MAX and int(thread) not in forum.bot.closed
    assert "Timed out" in ref["why"]


def test_a_waiting_build_request_reads_its_pull_request_at_most_every_30_s(forum, monkeypatch):
    thread = opened(forum)
    decisions.create("build", "led1", **{**dict(actions=["merge", "send-back", "nothing"], recommended="merge",
                                                head="a" * 40, pr="https://github.com/o/r/pull/1", cycle=1,
                                                choices=[["Merge", "Send back", "Do nothing"]]),
                                         "questions": [question("Review", ("Merge", "Send back", "Do nothing"))],
                                         "run": {"repo": "o/r", "issue": 7}})
    reads = []
    monkeypatch.setattr(decisions, "obsolete", lambda req: reads.append(req["id"]))
    forum.clock[0] = time.time() + 60
    conversation.finish("led1")
    tick(forum, 0)
    tick(forum, 2, n=10)
    assert len(reads) == 1 and int(thread) in forum.bot.closed
    tick(forum, 30)
    assert len(reads) == 2


def test_a_human_reopen_marks_the_run_resumed(forum):
    thread = closed_later(forum, by="human")
    run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, int(thread), closed=False), None))
    assert conversation.load("led1")["resumed"] is True


def test_a_human_reopen_while_a_close_retries_wins_and_stays_open(forum, monkeypatch):
    from muster import cleanup
    thread = opened(forum)
    req = ask()
    tick(forum, 0)
    real = forum.bot.close_forum_topic

    async def landed_then_timed_out(chat, thread):  # Telegram closed it; the reply was lost
        await real(chat, thread)
        raise Exception("Timed out")
    monkeypatch.setattr(forum.bot, "close_forum_topic", landed_then_timed_out)
    conversation.update("led1", resolved_at=1.0)
    conversation.finish("led1")
    tick(forum, 0)
    ref = conversation.load("led1")
    assert ref["state"] == "closing" and ref["retry_at"] > 0 and int(thread) in forum.bot.closed
    forum.bot.closed.discard(int(thread))
    run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, int(thread), closed=False), None))
    ref = conversation.load("led1")
    assert ref["state"] == "open" and ref["resumed"] is True and ref["retry_at"] == ref["retry_delay"] == 0
    assert "boot" not in decisions.load(req["id"])["presented"]  # eligible to present again
    tick(forum, 3600, n=3)  # past any close retry
    monkeypatch.setattr(cleanup, "ledger_status", lambda ledger: "done")
    assert cleanup.resolve(False) is False
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "open" and int(thread) not in forum.bot.closed
    assert [s["thread"] for s in forum.adapter.sent] == [thread, thread]  # shown again in the reopened topic


@pytest.mark.parametrize("state", ["retiring", "deleted"])
def test_a_human_reopen_never_revives_a_retired_topic(forum, state):
    thread = opened(forum)
    conversation.update("led1", state=state)
    run(telegram.topic_toggled(fh.topic_toggled(GROUP_CHAT, int(thread), closed=False), None))
    assert conversation.load("led1")["state"] == state


def test_an_archived_runs_card_subscriptions_are_removed(forum):
    thread = opened(forum, cards=["t_card"])
    assert [r["thread_id"] for r in forum.subs.rows["t_card"]] == [thread]
    forum.subs.status["led1"] = "archived"
    tick(forum, 31, n=2)
    ref = conversation.load("led1")
    assert ref["state"] == "deleted" and ref["unsubscribed"] is True and forum.subs.rows["t_card"] == []


# -- a switch from Slack back to Telegram: Slack's conversations and messages are not Telegram's ------------------

SLACK_THREAD = "1792269000.000100"


def slack_ref(state, **fields):
    return requested(platform="slack", chat_id="C_TEST", thread_id=SLACK_THREAD, state=state, **fields)


@pytest.mark.parametrize("state", ["open", "closed"])
def test_a_slack_runs_thread_is_not_a_telegram_topic_and_new_prompts_go_top_level(forum, state):
    before = slack_ref(state, closed_at=1.0 if state == "closed" else None)
    ask()
    tick(forum, 0)
    assert [s["thread"] for s in forum.adapter.sent] == [None]
    assert forum.bot.calls == [] and conversation.load("led1") == before


@pytest.mark.parametrize("state", ["pending", "creating", "closing", "retiring", "deleted"])
def test_a_slack_runs_ref_gets_no_telegram_lifecycle_step(forum, state):
    before = slack_ref(state)
    forum.subs.status["led1"] = "archived"  # would retire a Telegram topic
    tick(forum, 31, n=3)
    assert forum.bot.calls == [] and forum.subs.calls == [] and forum.adapter.notes == []
    assert conversation.load("led1") == before


def test_saved_slack_message_ids_are_neither_reply_bound_nor_edited(hermes):
    req = ask()
    decisions.update(req["id"], presented={"boot": "b", "platform": "slack", "messages": {"0": [SLACK_THREAD]},
                                           "replies": {"0": ["1792269000.000200"]}})
    req, _ = decisions.transition(req["id"], ("open",), "done", outcome=decisions.ANSWER_SENT)
    telegram.track(req)
    assert telegram.T.messages == {}
    assert run(telegram.outcome(req)) is True and telegram.T.adapter.edits == []


def test_saved_ids_with_no_platform_are_still_telegrams(hermes):
    req = ask()
    decisions.update(req["id"], presented={"boot": "b", "messages": {"0": ["901"]}})
    req, _ = decisions.transition(req["id"], ("open",), "done", outcome=decisions.ANSWER_SENT)
    telegram.track(req)
    assert telegram.T.messages == {"901": (req["id"], 0)}
    assert run(telegram.outcome(req)) is True and [m for _, m, _ in telegram.T.adapter.edits] == ["901"]


# -- the run's status message (#46) ---------------------------------------------------------------

import os  # noqa: E402

from muster import events, progress  # noqa: E402

P = "t_p1"
NOW = 1_800_000_000.0


@pytest.fixture
def clock(monkeypatch):
    t = [NOW]
    monkeypatch.setattr(shared, "now", lambda: t[0])
    monkeypatch.setattr(events, "status", lambda card: "ready")  # the board read for a run with no topic
    return t


def at(kind, t, ledger=P):
    path = progress.activity(ledger) / kind
    path.touch()
    os.utime(path, (t, t))


def launched(t=NOW - 60, received=True, ledger=P):
    directory = config.data_dir() / "intake" / ledger
    directory.mkdir(parents=True, exist_ok=True)
    progress.launched(ledger, directory, "sha")
    at("launched", t, ledger)
    if received:
        (directory / "prompt-seen.jsonl").write_text(f'{{"sha256": "sha", "at": {int(t)}}}\n')


def statuses(adapter=None):
    return [n for n in (adapter or telegram.T.adapter).notes
            if n[1].split("\n")[0] in (P, (conversation.load(P) or {}).get("name"))]  # the status message: its run, a line


def mtimes():
    return {p.name: p.stat().st_mtime for p in progress.activity(P).iterdir()}


def test_a_confirmed_start_posts_one_status_message_and_a_rescan_posts_nothing(clock):
    launched()
    run(gateway.scan())
    run(gateway.scan())
    [(chat, text, thread)] = statuses()
    assert (chat, thread) == (DM, None)
    assert text == f"{P}\nStarted {progress.hm(NOW - 60)} · no tool activity yet"
    assert telegram.T.adapter.edits == []
    assert progress.load(P)["key"] == "received"


def test_launch_without_confirmed_receipt_says_so_then_turns_into_received(clock):
    launched(received=False)
    run(gateway.scan())
    assert "waiting for the builder to start" in statuses()[0][1]
    (config.data_dir() / "intake" / P / "prompt-seen.jsonl").write_text('{"sha256": "sha", "at": 1}\n')
    run(gateway.scan())
    assert len(statuses()) == 1 and f"started {progress.hm(1)}" in telegram.T.adapter.edits[-1][2].lower()


def test_activity_times_age_at_most_every_five_minutes_and_no_edit_moves_them(clock):
    launched()
    run(gateway.scan())
    at("tool", NOW - 30)
    run(gateway.scan())  # received -> working: a new state, edited at once
    assert len(telegram.T.adapter.edits) == 1 and "last activity 0 min ago" in telegram.T.adapter.edits[0][2]
    before = mtimes()
    clock[0] = NOW + 120
    run(gateway.scan())
    assert len(telegram.T.adapter.edits) == 1  # the same state inside five minutes: nothing
    clock[0] = NOW + 300
    run(gateway.scan())
    assert len(telegram.T.adapter.edits) == 2 and "last activity 5 min ago" in telegram.T.adapter.edits[1][2]
    clock[0] = NOW + 16 * 60
    run(gateway.scan())  # quiet is its own state: at once, and never called a failure
    assert "Quiet 16 min" in telegram.T.adapter.edits[2][2]
    assert mtimes() == before and len(statuses()) == 1


def test_a_question_shows_waiting_at_once_below_nothing_and_its_answer_ends_it(clock):
    launched()
    run(gateway.scan())
    [status_id] = telegram.T.adapter.note_ids
    req = ask(ledger=P)
    at("tool", NOW)  # a parallel worker's tool: the question is still open
    run(gateway.scan())
    assert "🟡 Your decision: question · since" in telegram.T.adapter.edits[-1][2]
    assert int(sent()[-1]["mid"]) > int(status_id)  # the question lands below the status message, never above
    decisions.transition(req["id"], ("open",), "done", outcome=decisions.ANSWER_SENT)
    run(gateway.scan())
    [*_, last] = [text for _, mid, text in telegram.T.adapter.edits if mid == status_id]  # not the question's own edit
    assert "Your decision" not in last and "last activity" in last


def test_a_restart_never_posts_a_second_status_message(clock, monkeypatch):
    launched()
    run(gateway.scan())
    monkeypatch.setattr(gateway, "S", gateway.State())
    run(gateway.scan())
    assert len(statuses()) == 1


def test_a_refused_post_is_retried_after_a_minute(clock):
    launched()
    telegram.T.adapter.fail_notes = 1
    run(gateway.scan())
    run(gateway.scan())
    assert statuses() == [] and progress.load(P)["retry_at"] == NOW + progress.RETRY
    clock[0] = NOW + progress.RETRY
    run(gateway.scan())
    assert len(statuses()) == 1


def test_a_post_whose_reply_was_lost_is_never_repeated(clock):
    launched()
    telegram.T.adapter.raise_notes = 1
    run(gateway.scan())
    clock[0] = NOW + 3600
    run(gateway.scan())
    assert statuses() == [] and progress.load(P)["lost"] == {"thread": None}


def test_a_post_cut_by_a_kill_is_never_repeated(clock):
    launched()
    progress.save(P, posting={"thread": None})  # saved before the send; the gateway died before the reply
    run(gateway.scan())
    assert statuses() == [] and progress.load(P)["lost"] == {"thread": None}


def test_a_run_from_before_the_feature_gets_no_message(clock):
    progress.hooked(P, "prompt", {"hook_event_name": "PostToolUse"})  # no launched evidence: nothing tracked
    run(gateway.scan())
    assert statuses() == [] and not progress.activity(P).exists()


def test_an_archived_run_never_shown_gets_no_first_post(clock, monkeypatch):
    launched()
    monkeypatch.setattr(events, "status", lambda card: "archived")
    run(gateway.scan())
    assert statuses() == [] and progress.load(P)["final"] is True


def test_a_finished_run_is_edited_once_and_never_revived(forum, monkeypatch):
    monkeypatch.setattr(shared, "now", lambda: NOW)
    thread = opened(forum, ledger=P)
    launched()
    run(gateway.scan())
    [(_, _, where)] = statuses(forum.adapter)
    assert where == thread
    conversation.update(P, resolved_at=NOW, state="closing")
    run(gateway.scan())
    assert forum.adapter.edits[-1][2].endswith("Finished · the pull request merged")
    assert progress.load(P)["final"] is True
    edits = len(forum.adapter.edits)
    at("tool", NOW)  # a late hook
    monkeypatch.setattr(gateway, "S", gateway.State())  # and a restart
    run(gateway.scan())
    assert len(forum.adapter.edits) == edits and len(statuses(forum.adapter)) == 1


def test_a_refused_final_edit_is_retried_on_the_same_message_even_after_a_restart(forum, clock, monkeypatch):
    opened(forum, ledger=P)
    launched()
    run(gateway.scan())
    [status_id] = forum.adapter.note_ids
    conversation.update(P, resolved_at=NOW, state="closing")
    forum.adapter.fail_edits = 1
    run(gateway.scan())
    assert not progress.load(P).get("final") and progress.load(P)["retry_at"] == NOW + progress.RETRY
    assert P in progress.tracked()
    monkeypatch.setattr(gateway, "S", gateway.State())  # a restart before the retry
    clock[0] = NOW + progress.RETRY
    run(gateway.scan())
    [*_, (_, mid, text)] = forum.adapter.edits
    assert mid == status_id and text.endswith("Finished · the pull request merged")
    assert progress.load(P)["final"] is True and len(statuses(forum.adapter)) == 1


def test_a_remade_topic_gets_one_new_status_message(forum, monkeypatch):
    monkeypatch.setattr(shared, "now", lambda: NOW)
    opened(forum, ledger=P)
    launched()
    run(gateway.scan())
    new = str(forum.bot.make_topic(GROUP_CHAT, "again"))
    conversation.update(P, thread_id=new)
    run(gateway.scan())
    run(gateway.scan())
    assert [where for _, _, where in statuses(forum.adapter)][-2:] != [new, new]
    assert [where for _, _, where in statuses(forum.adapter)].count(new) == 1


TG_PLAN = "# Design v2\n\nDrop the cache.\n\n## Builder plan\n\n1. Delete cache.py.\n"


def tg_design(ledger="led1"):
    from muster import events
    events.proposals(ledger).mkdir(parents=True, exist_ok=True)
    (events.proposals(ledger) / "v2.md").write_text(TG_PLAN)
    pin = {"version": 2, "sha": __import__("hashlib").sha256(TG_PLAN.encode()).hexdigest()[:12]}
    labels = ["Approve design v2", "Send back with these revisions", "Leave pending"]
    return decisions.create("design", ledger, run={"repo": "o/r", "issue": 7, "branch": "b", "title": "Fix it"},
                            proposal=pin, review="Full review. " * 400, summary="Approve: small and right.",
                            feedback=None, actions=["approve", "send-back", "nothing"], choices=[labels],
                            questions=[{"text": "**🟡 Design v2 · Fix it**\n**Coordinator recommends approving it.**"
                                                "\n\nApprove: small and right.", "header": "Design", "multi": False,
                                        "options": [{"label": x, "description": ""} for x in labels]}])


def test_telegram_attaches_a_reviews_whole_text_once_above_a_plain_text_card(hermes, monkeypatch):
    review = "Paragraph. " * 800  # longer than one Telegram message
    labels = ["Merge PR (recommended)", "Leave PR open"]
    req = decisions.create("build", "led1", run={"repo": "o/r", "issue": 7, "branch": "b", "title": "Fix it"},
                           pr="https://github.com/o/r/pull/9", head="b" * 40, review=review.strip(),
                           summary="Right.\nDeploy on merge: unknown.", actions=["merge", "nothing"], choices=[labels],
                           questions=[{"text": "**PR #9 · Fix it**\n**Coordinator recommends: merge**\n\nRight.\n"
                                               "Deploy on merge: unknown.", "header": "Build", "multi": False,
                                       "options": [{"label": x, "description": ""} for x in labels]}])
    run(gateway.scan())
    (doc,) = telegram.T.adapter.documents
    assert doc["filename"] == f"fix-it-pr-9-review-{'b' * 7}.md" and doc["text"] == review.strip()
    assert doc["caption"] == "Full review · PR #9 · Fix it" and doc["kind"] == "document"
    assert not telegram.T.adapter.notes  # no inline dump
    (msg,) = sent()
    assert "**" not in msg["text"] and "Deploy on merge: unknown." in msg["text"]
    assert msg["text"].endswith("Full review: the Markdown files above.")
    telegram.release(req["id"])  # the first boot's prompt dies with it
    monkeypatch.setattr(gateway, "S", gateway.State())  # a restart presents the card again, never the file
    monkeypatch.setattr(gateway, "BOOT", "boot2")
    run(gateway.scan())
    assert len(telegram.T.adapter.documents) == 1 and len(sent()) == 2
    telegram.release(req["id"])


def test_telegram_attaches_a_design_and_its_review_in_the_runs_topic(hermes, forum):
    thread = opened(forum)
    req = tg_design()
    run(gateway.scan())
    docs = telegram.T.adapter.documents
    assert [(d["filename"], d["thread"]) for d in docs] == [("fix-it-design-v2-review.md", thread),
                                                             ("fix-it-design-v2.md", thread)]
    assert docs[1]["text"] == TG_PLAN and docs[0]["text"] == "Full review. " * 400
    assert sent()[-1]["thread"] == thread
    assert sent()[-1]["text"].endswith("Full review and design: the Markdown files above.")
    assert decisions.load(req["id"])["detail"] == {"platform": "telegram",
                                                   "files": ["fix-it-design-v2-review.md", "fix-it-design-v2.md"]}


def test_telegram_says_a_failed_document_and_a_restart_sends_only_the_missing_one(hermes, monkeypatch):
    req = tg_design()
    telegram.T.adapter.documents = []
    real = telegram.T.adapter._send_local_file

    async def second_fails(*a, **kw):  # the review arrives, the design is refused
        telegram.T.adapter.fail_documents = len(telegram.T.adapter.documents)
        return await real(*a, **kw)

    monkeypatch.setattr(telegram.T.adapter, "_send_local_file", second_fails)
    run(gateway.scan())
    assert [d["filename"] for d in telegram.T.adapter.documents] == ["fix-it-design-v2-review.md"]
    assert sent()[-1]["text"].endswith("⚠️ Full review and design could not be attached (Request Entity Too Large). "
                                       "Ask Coordinator in this thread for it.")
    monkeypatch.setattr(telegram.T.adapter, "_send_local_file", real)
    telegram.release(req["id"])  # the first boot's prompt dies with it
    monkeypatch.setattr(gateway, "S", gateway.State())  # a restart presents the card again
    monkeypatch.setattr(gateway, "BOOT", "boot2")
    run(gateway.scan())
    assert [d["filename"] for d in telegram.T.adapter.documents] == ["fix-it-design-v2-review.md",
                                                                     "fix-it-design-v2.md"]  # only the missing one
    assert sent()[-1]["text"].endswith("Full review and design: the Markdown files above.")
    assert "failed" not in decisions.load(req["id"])["detail"]
    telegram.release(req["id"])


def test_telegram_keeps_the_detail_message_of_a_request_made_before_documents(hermes):
    req = tg_design()
    decisions.update(req["id"], detail={"platform": "telegram", "id": "77"})
    run(gateway.scan())
    assert not telegram.T.adapter.documents and sent()[-1]["text"].endswith("Full text: the message above.")


def test_telegram_points_the_approval_at_the_review_and_a_restart_never_brings_it_back(hermes, monkeypatch):
    pin = {"version": 2, "sha": "abcdef012345"}
    q = ask(questions=[{"text": "Implement design v2?", "header": "Approval", "multi": False,
                        "options": [{"label": "Approve", "description": ""}]}], choices=[["Approve"]], proposal=pin)
    run(gateway.scan())
    (asked,) = sent()
    labels = ["Approve design v2 (recommended)", "Leave pending"]
    d = decisions.create("design", "led1", run={"repo": "o/r", "issue": 7}, proposal=pin, review="Fine.",
                         actions=["approve", "nothing"], choices=[labels],
                         questions=[{"text": "**Design v2 · Issue #7**\n\nFine.", "header": "Design", "multi": False,
                                     "options": [{"label": x, "description": ""} for x in labels]}])
    run(gateway.scan())
    assert (asked["mid"], "Design v2 ready · decide on Coordinator's review below.") in [
        (mid, text) for _, mid, text in telegram.T.adapter.edits]
    assert [m["cid"] for m in sent()] == [asked["cid"], f"mu{d['id']}q0"]
    telegram.release(q["id"])
    telegram.release(d["id"])
    monkeypatch.setattr(gateway, "S", gateway.State())  # a restart
    telegram.T = telegram.State()
    telegram.T.adapter = fh.Adapter()
    monkeypatch.setattr(gateway, "BOOT", "boot2")
    run(gateway.scan())
    assert [m["cid"] for m in sent()] == [f"mu{d['id']}q0"]  # the review again; the covered question stays retired
