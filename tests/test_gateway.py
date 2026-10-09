import asyncio
import hashlib
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from muster import config, core, decisions, gateway

from tests import fake_hermes as fh

ROOT = Path(__file__).resolve().parent.parent
DM = "4242"  # TELEGRAM_HOME_CHANNEL in the sandbox .env


@pytest.fixture(autouse=True)
def hermes(monkeypatch):
    clarify = fh.install(monkeypatch)
    from tools import approval, approval_gateway_wait
    monkeypatch.setattr(approval_gateway_wait, "TIMEOUT", 2)  # a stuck approval wait ends by itself
    monkeypatch.setattr(gateway, "S", gateway.State())
    monkeypatch.setattr(gateway, "CTX", types.SimpleNamespace(
        get_config=lambda k, d: config.settings.get(k, d), spawn_task=fh.spawn_task))
    gateway.S.configured = True
    gateway.S.adapter = fh.Adapter()
    before = set(threading.enumerate())
    LOOP.append(asyncio.new_event_loop())  # one loop per test: waiter threads hand answers back to the loop of the scan
    yield clarify
    for rid in [*gateway.S.shown, *gateway.S.approvals]:  # waiter threads block in the fakes: release them all
        gateway.release(rid)
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
    return gateway.S.adapter.sent


def status(req):
    return decisions.load(req["id"])["status"]


# -- the factory ------------------------------------------------------------------------------------

def test_factory_registers_both_guards_and_one_scan_task(monkeypatch):
    monkeypatch.setattr(gateway, "SCAN_EVERY", 0.01)
    prepared = []
    monkeypatch.setattr(core, "prepare_env", lambda: prepared.append(1))
    app, other = fh.Application(), fh.Application()

    async def go():
        gateway.telegram_factory(app, fh.Adapter())
        first = gateway.S.task
        new = fh.Adapter()
        gateway.telegram_factory(other, new)  # an app rebuild
        assert gateway.S.task is first and gateway.S.adapter is new and gateway.S.bot is other.bot
        first.cancel()

    run(go())
    assert [(g, h.pattern, h.callback) for h, g in app.handlers] == [
        (-1, r"^cl:mu", gateway.guard), (-1, r"^ea:", gateway.approval_guard), (-1, None, gateway.topic_created)]
    assert app.handlers[2][0].filters == "forum_topic_created"
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
    assert gateway.S.adapter._clarify_state == {cid: f"muster:{req['id']}:0"}  # Hermes's tap handler needs it
    for part in ("❓ o/r #7", "Which?", "• Alpha: about Alpha", "Proposal v2 abcdef12", "full text on ledger led1",
                 "reply to this message"):
        assert part in msg["text"]
    assert "1. Alpha" not in msg["text"] and "pane" not in msg["text"]  # the buttons carry the labels
    assert decisions.load(req["id"])["presented"] == {"boot": gateway.BOOT, "messages": {"0": [msg["mid"]]}}
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
    gateway.S.adapter.fail_sends = 1
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
    gateway.S.adapter.fail_sends = 1
    run(gateway.scan())
    assert req["id"] in gateway.S.retry
    decisions.transition(req["id"], ("open",), "done", outcome="Answered in the pane")
    run(gateway.scan())
    assert gateway.S.retry == {}


def test_the_backoff_doubles_up_to_a_minute():
    req = ask()
    delays = []
    for _ in range(7):
        gateway.S.adapter.fail_sends = 1
        run(gateway.scan())
        delays.append(gateway.S.retry[req["id"]][1])
        gateway.S.retry[req["id"]] = (0, delays[-1])
    assert delays == [2, 4, 8, 16, 32, 60, 60]


def test_oversize_text_is_capped_and_points_to_the_wait_card():
    ask(text="x" * 6000)
    run(gateway.scan())
    text = sent()[0]["text"]
    assert len(text) <= gateway.CAP + len("❓ ")
    assert "Full options on wait card w1" in text and "pane" not in text


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
    assert len(text) <= gateway.CAP + len("❓ ") and "d" * 100 not in text and "wait card w1" in text


def test_a_build_review_shows_its_actions_on_the_buttons():
    labels = ["Merge (squash) (recommended)", "Send back", "Do nothing"]
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
    assert len(button) == gateway.LABEL_MAX and button == long[:gateway.LABEL_MAX - 1] + "…"
    assert f"• {long}\n" in msg["text"] and "• Beta: about Beta" in msg["text"]


def test_a_long_label_stays_whole_in_oversize_text():
    long = "Use the shared config loader for every entry point"
    ask(text="x" * 6000, labels=(long, "B"), questions=[{"text": "x" * 6000, "header": "", "multi": False, "options": [
        {"label": long, "description": "d" * 500}, {"label": "B", "description": "e" * 500}]}])
    run(gateway.scan())
    text = sent()[0]["text"]
    assert f"• {long}" in text and "d" * 100 not in text and len(text) <= gateway.CAP + len("❓ ")


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
    gateway.S.bot = app.bot

    async def go():
        await gateway.guard(fh.update(4242, 4242, data=f"cl:mu{req['id']}q0:other"), None)
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
    assert (DM, mid, "Received ✓: typed words") not in gateway.S.adapter.edits  # the prompt's own message is edited
    assert gateway.S.adapter.edits[-1][2] == "Received ✓: typed words"


def test_ask_for_text_without_a_bot_or_on_foreign_data_sends_nothing():
    app = fh.Application()
    run(gateway.ask_for_text(fh.Query(4242, 4242, "cl:mu00q0:other")))  # no bot yet
    gateway.S.bot = app.bot
    run(gateway.ask_for_text(fh.Query(4242, 4242, "cl:other")))
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
    assert gateway.S.adapter.edits == []  # Hermes edits the message on a tap, not muster


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
    assert entry.response == hermes.CANCELLED and gateway.S.shown == {}
    assert hermes._entries == {} or f"mu{req['id']}q0" not in hermes._entries


def test_a_cancelled_waiter_answers_nothing(hermes):
    req = ask()
    run(gateway.scan())
    gateway.release(req["id"])
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
        assert gateway.S.adapter.edits[-1] == (DM, mid, f"Received ✓: {text}")


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
    assert gateway.S.adapter.edits == []


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
    assert hermes._entries[f"mu{req['id']}q0"].response is None and gateway.S.adapter.edits == []


def test_dispatch_with_nothing_presented_passes_through():
    assert dispatch("words") is None


def test_dispatch_errors_pass_through(monkeypatch):
    async def boom(event, gateway):
        raise RuntimeError("x")

    monkeypatch.setattr(gateway, "_dispatch", boom)
    assert dispatch("words") is None


# -- the guards -------------------------------------------------------------------------------------

def tap(user, chat, data="cl:mu0q0:0", fn=None):
    update = fh.update(user, chat, data=data)
    try:
        run((fn or gateway.guard)(update, None))
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
    assert gateway.authorized(55, -100123) is False


def test_a_guard_error_fails_closed(monkeypatch):
    monkeypatch.setattr(gateway, "authorized", lambda u, c: 1 / 0)
    assert tap(4242, 4242) == ("stopped", ["Not authorized"])
    assert "guard error" in core.log_path("gateway").read_text()


def test_a_refused_tap_on_other_sends_no_reply_prompt():
    app = fh.Application()
    gateway.S.bot = app.bot
    assert tap(999, 4242, data="cl:mu0aq0:other")[0] == "stopped"
    assert app.bot.sent == []


def test_the_approval_guard_covers_muster_cards_and_leaves_hermess_own():
    gateway.S.adapter._approval_state[1] = f"muster:abc"
    gateway.S.adapter._approval_state[2] = "agent:main:telegram"
    assert tap(999, 4242, "ea:once:1", gateway.approval_guard) == ("stopped", ["Not authorized"])
    assert tap(4242, 4242, "ea:once:1", gateway.approval_guard) == ("passed", [])
    assert tap(999, 4242, "ea:once:2", gateway.approval_guard) == ("passed", [])  # Hermes's own card
    assert tap(999, 4242, "ea:once:77", gateway.approval_guard) == ("passed", [])  # unknown id
    assert tap(999, 4242, "ea:junk", gateway.approval_guard) == ("passed", [])


# -- settling the messages --------------------------------------------------------------------------

def test_a_finished_request_edits_its_newest_message_to_the_outcome():
    req = ask()
    run(gateway.scan())
    decisions.transition(req["id"], ("open",), "done", outcome="Sent to Claude")
    run(gateway.scan())
    assert (DM, sent()[0]["mid"], "Sent to Claude") in gateway.S.adapter.edits
    assert decisions.load(req["id"])["edited"] is True
    n = len(gateway.S.adapter.edits)
    run(gateway.scan())
    assert len(gateway.S.adapter.edits) == n


def test_reply_prompts_are_edited_to_the_outcome_too(hermes):
    req, mid = presented(hermes)
    decisions.update(req["id"], presented={**decisions.load(req["id"])["presented"], "replies": {"0": ["901"]}})
    decisions.transition(req["id"], ("open",), "done", outcome="Sent to Claude")
    run(gateway.scan())
    assert sorted(m for _, m, _ in gateway.S.adapter.edits) == sorted([mid, "901"])
    assert {t for _, _, t in gateway.S.adapter.edits} == {"Sent to Claude"}


def test_a_failed_edit_is_retried_once_then_dropped():
    req = ask()
    run(gateway.scan())
    decisions.transition(req["id"], ("open",), "done", outcome="Sent to Claude")
    gateway.S.adapter.fail_edits = 2
    run(gateway.scan())
    assert not decisions.load(req["id"]).get("edited") and req["id"] in gateway.S.watch
    run(gateway.scan())  # the retry also fails: dropped
    assert decisions.load(req["id"])["edited"] is True and req["id"] not in gateway.S.watch
    assert gateway.S.adapter.edits == []


def test_a_request_that_ended_while_the_gateway_was_down_is_edited_on_the_first_scan(hermes):
    req = ask()
    run(gateway.scan())
    gateway.release(req["id"])
    decisions.transition(req["id"], ("open",), "stale", outcome="The agent session ended")
    old = gateway.S
    gateway.S = gateway.State()  # a restarted gateway
    gateway.S.adapter, gateway.S.configured = fh.Adapter(), True
    run(gateway.scan())
    assert gateway.S.adapter.edits == [(DM, old.adapter.sent[0]["mid"], "The agent session ended")]
    assert decisions.load(req["id"])["edited"] is True


def test_representing_edits_the_earlier_messages_to_superseded(hermes):
    req = ask(presented={"boot": "old", "messages": {"0": ["7", "8"]}})
    run(gateway.scan())
    (msg,) = sent()
    assert gateway.S.adapter.edits == [(DM, "7", "Superseded: see the newer message"),
                                       (DM, "8", "Superseded: see the newer message")]
    assert decisions.load(req["id"])["presented"]["messages"] == {"0": ["7", "8", msg["mid"]]}


def test_a_failed_partial_send_supersedes_what_this_boot_sent():
    two = [question(f"Q{i}") for i in range(2)]
    ask(questions=two, choices=[["Alpha", "Beta"]] * 2)
    adapter, real = gateway.S.adapter, gateway.S.adapter._send_prompt

    async def second_fails(*args, **kw):
        if adapter.sent:
            adapter.fail_sends = 1
        return await real(*args, **kw)

    adapter._send_prompt = second_fails
    run(gateway.scan())
    (first,) = sent()
    assert (DM, first["mid"], "Superseded: see the newer message") in adapter.edits


# -- restart ----------------------------------------------------------------------------------------

def test_a_request_from_an_earlier_boot_is_represented(hermes):
    req = ask(presented={"boot": "old", "messages": {"0": ["7"]}})
    run(gateway.scan())
    (msg,) = sent()
    assert gateway.S.adapter.edits == [(DM, "7", "Superseded: see the newer message")]
    assert decisions.load(req["id"])["presented"] == {"boot": gateway.BOOT, "messages": {"0": ["7", msg["mid"]]}}


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
    assert (got["status"], got["outcome"]) == ("stale", "The agent is no longer waiting")
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
    await until(lambda: any(m.get("command") for m in gateway.S.adapter.sent))
    return gateway.S.adapter.sent[-1]


def test_a_permission_prompt_is_hermess_approval_card():
    async def go():
        req = permission()
        card = await card_for(req)
        assert card["command"] == "touch x" and card["session"] == f"muster:{req['id']}"
        assert card["permanent"] is False and card["session_button"] is False  # Allow once and Deny only
        assert card["text"].startswith("b: Create x.") and "reply to this message" in card["text"]
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
        assert gateway.S.adapter.edits[-1] == (DM, card["mid"], "Received ✓: please use make")
    run(go())


def test_a_reply_to_the_card_from_an_unauthorized_user_does_nothing():
    async def go():
        req = permission()
        card = await card_for(req)
        got = await gateway.on_dispatch(event=fh.event("no", 999, DM, reply=card["mid"]), gateway=fh.Gateway())
        assert got is None and status(req) == "open" and gateway.S.approvals == {req["id"]}
    run(go())


def test_a_failed_card_send_is_retried_with_backoff():
    async def go():
        req = permission()
        gateway.S.adapter.fail_sends = 1
        await gateway.scan()
        await until(lambda: req["id"] in gateway.S.retry)
        assert status(req) == "open" and req["id"] not in gateway.S.approvals
        gateway.S.retry[req["id"]] = (0, 2)
        await gateway.scan()
        await until(lambda: req["id"] not in gateway.S.retry and gateway.S.approvals)
        assert len(sent()) == 1
    run(go())


def test_an_ended_request_withdraws_its_card():
    async def go():
        req = permission()
        await card_for(req)
        decisions.transition(req["id"], ("open",), "stale", outcome="Answered in the pane")
        await gateway.scan()
        assert gateway.S.approvals == set()
        await asyncio.sleep(0.2)
        assert status(req) == "stale" and "answer" not in decisions.load(req["id"])  # a withdrawn wait answers nothing
    run(go())


def test_a_silent_hook_stales_a_permission_request_and_withdraws_it():
    async def go():
        req = permission()
        await card_for(req)
        decisions.update(req["id"], alive=time.time() - 40)
        await gateway.scan()
        assert status(req) == "stale" and gateway.S.approvals == set()
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
        def register_cli_command(self, **kw):
            pass

        def register_skill(self, name, path):
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
        assert gw.CTX is ctx and gw.S.task is None  # nothing ran at register time
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


def test_an_answered_build_or_feedback_request_is_executed_on_the_pool_with_this_boot(monkeypatch):
    calls = []
    monkeypatch.setattr(decisions, "execute", lambda rid, boot="": calls.append((rid, boot, threading.current_thread())))
    q = tap_request("question", "open")
    b, f = tap_request("build"), tap_request("feedback")

    async def go():
        for rid in (q, b, f):
            gateway.on_answered(decisions.load(rid))
        await until(lambda: len(calls) == 2)

    run(go())
    assert sorted(c[0] for c in calls) == sorted([b, f]) and all(c[1] == gateway.BOOT for c in calls)
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
    assert decisions.load(req["id"])["outcome"] == "No action. PR open, not merged."


def test_build_free_text_becomes_a_feedback_request_that_the_next_scan_presents(hermes):
    q = {"text": "R", "header": "Build", "multi": False, "options": [{"label": "Do nothing", "description": ""}]}
    req = decisions.create("build", "led1", run={"repo": "o/r", "branch": "b", "pane": "p1"}, actions=["nothing"],
                           choices=[["Do nothing"]], questions=[q], head="h" * 40, base="main", pr="u", cycle=1,
                           feedback="Fix it.")

    async def go():
        await gateway.answered(req["id"], 0, "please also add docs")
        await until(lambda: decisions.load(req["id"])["status"] == "done")
        await gateway.scan()

    run(go())
    (fb,) = decisions.for_ledger("led1", "feedback")
    assert fb["feedback"] == "Fix it.\n\nAdditional instructions from the human:\nplease also add docs"
    assert fb["choices"] == [["Send as written", "Don't send"]]
    assert [[b for b, _ in m["buttons"]] for m in sent()] == [["Send as written", "Don't send", fh.OTHER]]


def test_a_scan_retires_an_open_retry_whose_send_arrived_late_and_presents_nothing(tmp_path):
    core.prompt_seen(tmp_path, {"prompt": "the revision"})
    rid = tap_request("feedback", "open", prompt_sha=hashlib.sha256(b"the revision").hexdigest(),
                      run={"branch": "b", "evidence_dir": str(tmp_path)})
    waiting = tap_request("feedback", "open", prompt_sha="0" * 64, run={"branch": "b", "evidence_dir": str(tmp_path)})
    decisions.update(rid, presented={"boot": gateway.BOOT})  # already shown this boot: reconcile still runs

    async def go():
        await asyncio.gather(gateway.scan(), gateway.scan())

    run(go())
    assert decisions.load(rid)["status"] == "done" and decisions.load(rid)["outcome"] == "Sent ✓"
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

    async def go():
        await gateway.scan()
        await gateway.scan()
        await until(lambda: rec and ex)

    run(go())
    assert rec == [old] and ex == [pending] and mine not in rec + ex and asked not in rec + ex


# -- run topics (#24) -----------------------------------------------------------------------------

from muster import conversation  # noqa: E402

GROUP_CHAT = "-100123"


@pytest.fixture
def forum(monkeypatch):
    """Topics on in a forum group; the bot may manage topics; one clock for topic timing."""
    for key, value in (("notify_topics", True), ("notify_chat_id", GROUP_CHAT), ("notify_user_id", "42")):
        monkeypatch.setitem(config.settings, key, value)
    bot = fh.Bot()
    gateway.S.bot = gateway.S.adapter.bot = bot
    subs = fh.Subs()
    monkeypatch.setattr(core, "kanban", subs)
    clock = [1000.0]
    monkeypatch.setattr(gateway, "now", lambda: clock[0])
    monkeypatch.setattr(gateway, "clock", lambda: clock[0])
    return types.SimpleNamespace(bot=bot, subs=subs, clock=clock, adapter=gateway.S.adapter)


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
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, 41, "r#7 Fix it"), None))
    assert conversation.load("led1")["state"] == "open" and conversation.load("led1")["thread_id"] == "41"
    tick(forum, 11, n=3)
    assert list(forum.bot.topics) == [41]  # no second create
    # a late notice of another topic of that name: a duplicate, said so and closed
    forum.bot.make_topic(GROUP_CHAT, "r#7 Fix it")
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, 42, "r#7 Fix it"), None))
    assert (GROUP_CHAT, 'Duplicate topic, not used; this run is in the topic "r#7 Fix it".', "42") in forum.adapter.notes
    assert 42 in forum.bot.closed and 41 not in forum.bot.closed
    assert conversation.load("led1")["thread_id"] == "41"


def test_a_notice_from_someone_else_or_another_chat_adopts_nothing(forum):
    forum.adapter.creates = ["lost"]
    requested()
    tick(forum, 0)
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, 41, "r#7 Fix it", sender=42), None))  # the human made it
    run(gateway.topic_created(fh.topic_notice("-100999", 41, "r#7 Fix it"), None))
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
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, 41, "r#7 Fix it"), None))
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
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, 41, "r#7 Fix it"), None))
    assert conversation.load("led1")["state"] == "fallback" and 41 in forum.bot.closed
    assert (GROUP_CHAT, "Duplicate topic, not used; this run is in the main chat.", "41") in forum.adapter.notes


def test_a_finished_run_closes_its_topic_once_and_keeps_it(forum):
    thread = opened(forum)
    conversation.finish("led1")
    tick(forum, 0, n=3)
    assert conversation.load("led1")["state"] == "closed" and int(thread) in forum.bot.closed
    assert int(thread) in forum.bot.topics  # never deleted
    assert [n for n in forum.adapter.notes if "finished" in n[1]] == [
        (GROUP_CHAT, "Run finished; worktree removed. History kept.", thread)]


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
    tick(forum, 0)  # the present's probe finds the topic gone: nothing is sent to General
    assert forum.adapter.sent == [] and conversation.load("led1")["state"] == "creating"
    tick(forum, 0, n=3)  # made, then the cards and the request move
    ref = conversation.load("led1")
    new = ref["thread_id"]
    assert ref["state"] == "open" and new != old and ref["previous"] == [old] and ref["repaired"] == new
    assert [s["thread"] for s in forum.adapter.sent] == [new] and forum.adapter.sent[0]["cid"] == f"mu{req['id']}q0"
    assert forum.subs.deliver("led1") == [new] and forum.subs.deliver("w1") == [new]
    assert (GROUP_CHAT, "The previous topic was deleted; this run continues here.", new) in forum.adapter.notes


def test_a_deleted_topic_then_a_coordinator_wake_lands_in_the_new_topic(forum):
    old = opened(forum, cards=["led1"])
    tick(forum, 0)  # first probe: alive
    forum.bot.deleted.add(int(old))
    tick(forum, 10)
    assert conversation.load("led1")["state"] == "open"  # the next probe is due 30 s after the last
    tick(forum, 21)
    assert conversation.load("led1")["state"] == "creating"
    tick(forum, 0, n=2)
    new = conversation.load("led1")["thread_id"]
    # the ledger completes (hook done): Hermes's notifier pings and wakes the coordinator per subscription
    assert new != old and forum.subs.deliver("led1") == [new]


def test_a_recreate_that_fails_moves_the_run_to_the_main_chat(forum):
    old = opened(forum, cards=["led1"])
    forum.bot.deleted.add(int(old))
    forum.bot.forum = False  # Topics turned off meanwhile
    tick(forum, 0)
    tick(forum, 0, n=3)
    ref = conversation.load("led1")
    assert ref["state"] == "fallback" and ref["repaired"] == "main"
    assert forum.subs.deliver("led1") == [""]


def test_after_a_restart_the_topic_is_reused(forum):
    thread = opened(forum)
    ask()
    gateway.S = gateway.State()
    gateway.S.configured, gateway.S.adapter, gateway.S.bot = True, forum.adapter, forum.bot
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
        run(gateway.guard(update, None))
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
    decisions.create("feedback", "led1", actions=["send", "cancel"], choices=[["Send as written", "Don't send"]],
                     questions=[question("Fix X", ("Send as written", "Don't send"))], run={"repo": "o/r", "issue": 7})

    async def go():
        await gateway.scan()
        await until(lambda: len(forum.adapter.sent) == 3)

    run(go())
    assert sorted(s["thread"] for s in forum.adapter.sent) == [thread] * 3


def test_with_topics_off_nothing_carries_a_thread(hermes):
    ask()
    run(gateway.scan())
    assert gateway.S.adapter.sent[0]["thread"] is None and not (config.data_dir() / "runs").exists()


def test_an_unknown_probe_error_changes_nothing_and_is_logged(forum, monkeypatch):
    opened(forum)

    async def flaky(chat_id, message_thread_id):
        raise Exception("Timed out")
    monkeypatch.setattr(forum.bot, "reopen_forum_topic", flaky)
    ask()
    tick(forum, 0)
    assert conversation.load("led1")["state"] == "open" and len(forum.adapter.sent) == 1  # sent to its topic
    assert "probe: Timed out" in core.log_path("gateway").read_text()


def test_a_prompt_rerouted_to_general_is_not_answered_there_and_moves_to_a_new_topic(forum, monkeypatch):
    old = opened(forum, cards=["led1"])
    forum.bot.deleted.add(int(old))
    real = forum.bot.reopen_forum_topic

    async def flaky(chat_id, message_thread_id):  # the probe cannot tell, so the send goes out
        raise Exception("Timed out")
    monkeypatch.setattr(forum.bot, "reopen_forum_topic", flaky)
    req = ask()
    tick(forum, 0)
    assert [s["thread"] for s in forum.adapter.sent] == [None]  # Hermes resent it to General
    monkeypatch.setattr(forum.bot, "reopen_forum_topic", real)
    update = fh.update(42, GROUP_CHAT, data=f"cl:mu{req['id']}q0:0", thread=None)
    with pytest.raises(fh.ApplicationHandlerStop):
        run(gateway.guard(update, None))
    assert update.callback_query.answers == ["Moved: answer in the run's topic"] and status(req) == "open"
    tick(forum, 0, n=4)
    new = conversation.load("led1")["thread_id"]
    assert new != old and [s["thread"] for s in forum.adapter.sent] == [None, new]


def test_while_a_deleted_topic_is_remade_no_tap_is_taken_anywhere(forum):
    old = opened(forum, cards=["led1"])
    req = ask()
    tick(forum, 0)
    forum.bot.deleted.add(int(old))
    tick(forum, 31)
    assert conversation.load("led1")["state"] == "creating"
    for thread in (None, int(old)):
        update = fh.update(42, GROUP_CHAT, data=f"cl:mu{req['id']}q0:0", thread=thread)
        with pytest.raises(fh.ApplicationHandlerStop):
            run(gateway.guard(update, None))
    assert status(req) == "open"


def test_a_recreate_that_falls_back_presents_the_prompt_once_in_the_main_chat(forum):
    old = opened(forum, cards=["led1"])
    forum.bot.deleted.add(int(old))
    forum.bot.forum = False
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
        run(gateway.approval_guard(update, None))
    assert update.callback_query.answers == ["Moved: answer in the run's topic"] and status(req) == "open"


def test_someone_elses_tap_cannot_make_a_prompt_present_again(forum):
    opened(forum)
    req = ask()
    tick(forum, 0)
    update = fh.update(99, GROUP_CHAT, data=f"cl:mu{req['id']}q0:0", thread=None)
    with pytest.raises(fh.ApplicationHandlerStop):
        run(gateway.guard(update, None))
    assert update.callback_query.answers == ["Not authorized"]
    assert req["id"] in gateway.S.shown and decisions.load(req["id"])["presented"].get("boot") == gateway.BOOT


def test_a_card_whose_subscription_does_not_move_is_retried_before_the_repair_is_done(forum):
    old = opened(forum, cards=["led1", "w1"])
    forum.bot.deleted.add(int(old))
    forum.subs.fail["led1"] = 1  # the ledger: the card the coordinator's review wake comes from
    tick(forum, 31)  # the probe finds it gone
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
    tick(forum, 31)
    tick(forum, 0, n=2)
    ref = conversation.load("led1")
    assert ref["migrating"] == ["led1"] and ref.get("repaired") is None


def test_topic_notices_bind_to_the_run_not_its_readable_title(forum):
    a_name = conversation.name("o/r", "Fix it", "ledA", issue=7)
    b_name = conversation.name("o/r", "Fix it", "ledB", issue=7)
    forum.adapter.creates = ["lost", "lost"]
    requested("ledA", a_name)
    requested("ledB", b_name)
    tick(forum, 0)
    a_thread, b_thread = (t for t, n in forum.bot.topics.items() if n == a_name), \
        (t for t, n in forum.bot.topics.items() if n == b_name)
    a_thread, b_thread = next(a_thread), next(b_thread)
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, b_thread, b_name), None))  # B's notice first
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, a_thread, a_name), None))
    assert conversation.load("ledA")["thread_id"] == str(a_thread)
    assert conversation.load("ledB")["thread_id"] == str(b_thread)
    # delayed duplicates: a repeat of A's own notice changes nothing; another topic named for A is closed
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, a_thread, a_name), None))
    extra = forum.bot.make_topic(GROUP_CHAT, a_name)
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, extra, a_name), None))
    assert forum.bot.closed == {extra}
    assert conversation.load("ledA")["thread_id"] == str(a_thread)
    assert conversation.load("ledB")["thread_id"] == str(b_thread)


def test_a_notice_whose_name_matches_two_runs_is_ignored(forum):
    forum.adapter.creates = ["lost", "lost"]
    requested("ledA", "same")
    requested("ledB", "same")  # cannot happen with ledger-tagged names; refused if it ever does
    tick(forum, 0)
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, 41, "same"), None))
    assert conversation.load("ledA")["state"] == conversation.load("ledB")["state"] == "creating"
    assert not forum.bot.closed and "ambiguous, ignored" in core.log_path("gateway").read_text()


def test_a_late_notice_of_the_deleted_previous_topic_is_never_adopted(forum):
    old = opened(forum, cards=["led1"])
    name = conversation.load("led1")["name"]
    forum.bot.deleted.add(int(old))
    forum.adapter.creates = ["lost"]
    tick(forum, 31)  # gone
    tick(forum, 0)  # the recreate's reply is lost
    new = max(forum.bot.topics)
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, int(old), name), None))  # the old topic's notice, late
    ref = conversation.load("led1")
    assert ref["state"] == "creating" and ref["thread_id"] is None and not forum.bot.closed
    run(gateway.topic_created(fh.topic_notice(GROUP_CHAT, new, name), None))
    assert conversation.load("led1")["thread_id"] == str(new) and not forum.bot.closed
    assert run(gateway.settle("led1", old)) is None and conversation.load("led1")["thread_id"] == str(new)


def test_a_card_that_never_moves_does_not_stop_the_deletion_probe(forum):
    old = opened(forum, cards=["led1", "w1"])
    forum.bot.deleted.add(int(old))
    forum.subs.fail["w1"] = 10 ** 6
    tick(forum, 31)
    tick(forum, 0, n=2)
    first = conversation.load("led1")["thread_id"]
    assert conversation.load("led1")["migrating"] == ["w1"]
    forum.bot.deleted.add(int(first))  # the new topic is deleted too
    tick(forum, 31)
    ref = conversation.load("led1")
    assert ref["state"] == "creating" and ref["previous"] == [old, first] and ref["migrate_at"] == 0
    tick(forum, 0, n=2)
    assert conversation.load("led1")["moved"] == conversation.load("led1")["thread_id"]  # prompts move at once


def test_a_card_deleted_from_the_board_counts_as_moved(forum):
    old = opened(forum, cards=["led1", "w1"])
    forum.bot.deleted.add(int(old))
    forum.subs.missing.add("w1")
    tick(forum, 31)
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
    tick(forum, 31)
    tick(forum, 0, n=3)
    ref = conversation.load("led1")
    assert ref["state"] == "fallback" and ref["migrating"] == ["led1"] and ref.get("repaired") is None

