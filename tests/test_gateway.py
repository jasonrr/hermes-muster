import asyncio
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


def ask(text="Which?", labels=("Alpha", "Beta"), multi=False, **fields):
    fields = {"questions": [question(text, labels, multi)], "choices": [list(labels)], "tool": {"name": "AskUserQuestion"},
              "wait": "w1", "run": {"repo": "o/r", "issue": 7, "branch": "muster/7", "pane": "p1", "kind": "issue"},
              "alive": time.time(), **fields}
    return decisions.create("question", "led1", **fields)


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
        (-1, r"^cl:mu", gateway.guard), (-1, r"^ea:", gateway.approval_guard)]
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
    assert (msg["chat"], msg["cid"], msg["session"], msg["choices"]) == (
        DM, f"mu{req['id']}q0", f"muster:{req['id']}:0", ["Alpha", "Beta"])
    for part in ("o/r #7", "Which?", "• Alpha: about Alpha", "Proposal v2 abcdef12", "full text on ledger led1",
                 "Herdr pane p1 (optional)", "reply to this message"):
        assert part in msg["text"]
    assert "1. Alpha" not in msg["text"]  # Hermes numbers the options under the text, matching its buttons
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
    assert len(text) <= gateway.CAP
    assert "Full options on wait card w1" in text and "Herdr pane p1 (optional)" in text


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
    assert len(text) <= gateway.CAP and "d" * 100 not in text and "wait card w1" in text


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
    decisions.transition(req["id"], ("open",), "done", outcome="Delivered ✓")
    run(gateway.scan())
    assert (DM, sent()[0]["mid"], "Delivered ✓") in gateway.S.adapter.edits
    assert decisions.load(req["id"])["edited"] is True
    n = len(gateway.S.adapter.edits)
    run(gateway.scan())
    assert len(gateway.S.adapter.edits) == n


def test_reply_prompts_are_edited_to_the_outcome_too(hermes):
    req, mid = presented(hermes)
    decisions.update(req["id"], presented={**decisions.load(req["id"])["presented"], "replies": {"0": ["901"]}})
    decisions.transition(req["id"], ("open",), "done", outcome="Delivered ✓")
    run(gateway.scan())
    assert sorted(m for _, m, _ in gateway.S.adapter.edits) == sorted([mid, "901"])
    assert {t for _, _, t in gateway.S.adapter.edits} == {"Delivered ✓"}


def test_a_failed_edit_is_retried_once_then_dropped():
    req = ask()
    run(gateway.scan())
    decisions.transition(req["id"], ("open",), "done", outcome="Delivered ✓")
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
    adapter, real = gateway.S.adapter, gateway.S.adapter.send_clarify

    async def second_fails(*args, **kw):
        if adapter.sent:
            adapter.fail_sends = 1
        return await real(*args, **kw)

    adapter.send_clarify = second_fails
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
    req = decisions.create(kind, "led1", questions=[{"text": "Q", "header": "", "multi": False, "options": []}],
                           choices=[["A"]], actions=["nothing"], run={"branch": "b"}, **fields)
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
    assert [m["choices"] for m in sent()] == [["Send as written", "Don't send"]]


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
