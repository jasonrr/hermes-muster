import asyncio
import os
import fcntl
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
    monkeypatch.setattr(gateway, "S", gateway.State())
    monkeypatch.setattr(gateway, "CTX", types.SimpleNamespace(get_config=lambda k, d: config.settings.get(k, d)))
    gateway.S.configured = True
    gateway.S.adapter = fh.Adapter()
    monkeypatch.setattr(core, "agent_at", lambda pane: {"agent_status": "blocked"})
    return clarify


def ask(text="Which?", labels=("Alpha", "Beta"), multi=False, **fields):
    q = {"text": text, "header": "H", "multi": multi,
         "options": [{"label": label, "description": f"about {label}"} for label in labels]}
    fields = {"questions": [q], "choices": [list(labels)], "tool": {"name": "AskUserQuestion", "input_sha": "x"},
              "wait": "w1", "run": {"repo": "o/r", "issue": 7, "branch": "muster/7", "pane": "p1", "kind": "issue"},
              "alive": time.time(), **fields}
    return decisions.create("question", "led1", **fields)


def permission(**fields):
    q = {"text": "Allow Bash?\n{}", "header": "Permission", "multi": False,
         "options": [{"label": "Allow once", "description": ""}, {"label": "Deny", "description": ""}]}
    return decisions.create("permission", "led1", questions=[q], choices=[["Allow once", "Deny"]],
                            tool={"name": "Bash", "input_sha": "y"}, run={"branch": "b", "pane": "p1"},
                            alive=time.time(), **fields)


def run(coro):
    return asyncio.run(coro)


async def until(check, seconds=2):
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


def sent():
    return gateway.S.adapter.sent


# -- 1. the factory ---------------------------------------------------------------------------------

def test_factory_registers_the_guard_and_one_scan_task(monkeypatch):
    monkeypatch.setattr(gateway, "SCAN_EVERY", 0.01)
    prepared = []
    monkeypatch.setattr(core, "prepare_env", lambda: prepared.append(1))
    monkeypatch.setitem(config.settings, "board", "from-ctx")
    app, other = fh.Application(), fh.Application()

    async def go():
        gateway.telegram_factory(app, fh.Adapter())
        first = gateway.S.task
        new = fh.Adapter()
        gateway.telegram_factory(other, new)  # an app rebuild
        assert gateway.S.task is first and gateway.S.adapter is new
        await until(lambda: (decisions.root() / ".gateway").exists())
        first.cancel()

    run(go())
    (handler, group), = app.handlers
    assert (group, handler.pattern, handler.block, handler.callback) == (-1, r"^cl:mu", True, gateway.guard)
    assert other.handlers and prepared == [1, 1]


def test_a_second_process_holding_the_lock_does_not_scan():
    path = decisions.root() / ".gateway.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    held = open(path, "w")
    fcntl.flock(held, fcntl.LOCK_EX)

    async def go():
        gateway.telegram_factory(fh.Application(), fh.Adapter())
        assert gateway.S.task is None and gateway.S.lock_fd is None

    run(go())
    held.close()


def test_the_factory_logs_a_group_without_a_user_id_once(monkeypatch):
    monkeypatch.setitem(config.settings, "notify_chat_id", "-100")
    monkeypatch.setitem(config.settings, "notify_user_id", "")
    monkeypatch.setattr(gateway, "SCAN_EVERY", 0.01)

    async def go():
        for _ in range(2):
            gateway.telegram_factory(fh.Application(), fh.Adapter())
        gateway.S.task.cancel()

    run(go())
    assert core.log_path("gateway").read_text().count("notify_user_id is empty") == 1


# -- 2. the scan ------------------------------------------------------------------------------------

def test_scan_touches_the_heartbeat():
    run(gateway.scan())
    assert (decisions.root() / ".gateway").exists()


def test_a_poisoned_request_does_not_stop_the_others():
    decisions.create("question", "led0")  # no questions: presenting it raises
    good = ask()
    run(gateway.scan())
    assert [m["cid"] for m in sent()] == [f"mu{good['id']}q0"]
    assert "request" in core.log_path("gateway").read_text()


# -- 3. presenting ----------------------------------------------------------------------------------

def test_an_open_request_is_presented_once(hermes):
    req = ask(proposal={"version": 2, "sha": "abcdef123456"})
    run(gateway.scan())
    run(gateway.scan())
    (msg,) = sent()
    assert (msg["chat"], msg["cid"], msg["session"], msg["choices"]) == (
        DM, f"mu{req['id']}q0", f"muster:{req['id']}", ["Alpha", "Beta"])
    for part in ("o/r #7", "Which?", "1. Alpha - about Alpha", "Proposal v2 abcdef12", "full text on ledger led1",
                 "Herdr pane p1 (optional)"):
        assert part in msg["text"]
    saved = decisions.load(req["id"])
    assert saved["presented"] == {"boot": gateway.BOOT, "messages": {"0": [msg["mid"]]}}
    assert f"mu{req['id']}q0" in hermes._entries


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
    assert not sent() and "presented" not in decisions.load(req["id"])
    run(gateway.scan())  # inside the backoff: no new attempt
    assert not sent() and gateway.S.retry[req["id"]][1] == 2
    gateway.S.retry[req["id"]] = (0, 2)
    run(gateway.scan())
    assert len(sent()) == 1 and decisions.load(req["id"])["presented"]["boot"] == gateway.BOOT
    assert gateway.S.retry == {}


def test_the_heartbeat_stops_while_sends_keep_failing(hermes):
    ask()
    beat = decisions.root() / ".gateway"
    gateway.S.adapter.fail_sends = 99
    run(gateway.scan())
    first = beat.stat().st_mtime
    gateway.S.failing_since -= gateway.HEALTHY_FOR + 1
    os.utime(beat, (0, 0))
    run(gateway.scan())
    assert beat.stat().st_mtime == 0 and first  # wait cards fall back to Hermes's own ping
    gateway.S.adapter.fail_sends = 0
    for rid in list(gateway.S.retry):
        gateway.S.retry[rid] = (0, 2)
    run(gateway.scan())  # a send works again
    assert gateway.S.failing_since is None
    run(gateway.scan())
    assert beat.stat().st_mtime > 0


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


def test_long_descriptions_are_cut_first():
    ask(labels=("A", "B"), questions=[{"text": "Which?", "header": "", "multi": False, "options": [
        {"label": "A", "description": "d" * 2000}, {"label": "B", "description": "e" * 2000}]}])
    run(gateway.scan())
    text = sent()[0]["text"]
    assert len(text) <= gateway.CAP and "1. A" in text and "d" * 100 not in text and "wait card w1" in text


# -- 5. answers -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,multi,expect", [
    ("2", False, "Beta"), (" 1 ", False, "Alpha"), ("beta", False, "Beta"), ("ALPHA", False, "Alpha"),
    ("9", False, "9"), ("0", False, "0"), ("1,2", False, "1,2"), ("1,2", True, "Alpha, Beta"),
    ("2 , 1", True, "Beta, Alpha"), ("1,9", True, "1,9"), ("something else", False, "something else"),
    ("  Spaced words ", False, "  Spaced words "),
])
def test_typed_text_maps_back_by_index(text, multi, expect):
    req = ask(multi=multi)
    req["questions"][0]["multi"] = multi
    assert gateway.shape(req, 0, text)[0] == expect


@pytest.mark.parametrize("tapped", ["2", "3", "5"])
def test_a_tapped_numeric_label_is_that_option_not_an_index(tapped):
    # Hermes resolves a tap with the label itself: "3" is the option labelled 3, not the third option
    req = ask(labels=("2", "3", "5"))
    assert gateway.shape(req, 0, tapped)[0] == tapped




def test_a_tap_answers_the_request_and_marks_it_received(hermes, monkeypatch):
    called = []
    monkeypatch.setattr(gateway, "on_answered", called.append)
    req = ask()
    rid = req["id"]

    async def go():
        await gateway.scan()
        assert hermes.resolve_gateway_clarify(f"mu{rid}q0", "Beta")  # what Hermes's callback does
        await until(lambda: decisions.load(rid)["status"] == "answered")

    run(go())
    got = decisions.load(rid)
    assert got["answer"] == {"Which?": "Beta"} and [c["id"] for c in called] == [rid]
    assert (DM, sent()[0]["mid"], "Received ✓") in gateway.S.adapter.edits


def test_every_question_must_be_answered_and_a_double_answer_counts_once(hermes, monkeypatch):
    called = []
    monkeypatch.setattr(gateway, "on_answered", called.append)
    two = [{"text": f"Q{i}", "header": "", "multi": False, "options": [{"label": "Yes", "description": ""}]}
           for i in range(2)]
    req = ask(questions=two, choices=[["Yes"], ["Yes"]])
    rid = req["id"]

    async def go():
        await gateway.scan()
        assert len(sent()) == 2
        await gateway.answered(rid, 0, "Yes")
        assert decisions.load(rid)["status"] == "open"
        await gateway.answered(rid, 1, "free")
        assert decisions.load(rid)["answer"] == {"Q0": "Yes", "Q1": "free"}
        await gateway.answered(rid, 1, "again")
        await gateway.answered(rid, 0, "again")

    run(go())
    assert len(called) == 1


def test_permission_answers():
    async def one(text):
        req = permission()
        await gateway.scan()
        await gateway.answered(req["id"], 0, text)
        return decisions.load(req["id"])["answer"]

    assert run(one("Allow once")) == {"decision": "allow"}
    assert run(one("1")) == {"decision": "allow"}
    assert run(one("Deny")) == {"decision": "deny"}
    assert run(one("not now, please")) == {"decision": "deny", "message": "not now, please"}


def test_build_answers_carry_the_action(monkeypatch):
    monkeypatch.setattr(gateway, "on_answered", lambda req: None)
    q = {"text": "Merge?", "header": "", "multi": False,
         "options": [{"label": "Merge (squash)", "description": ""}, {"label": "Do nothing", "description": ""}]}
    req = decisions.create("build", "led1", questions=[q], choices=[["Merge (squash)", "Do nothing"]],
                           actions=["merge", "nothing"], run={"branch": "b"})

    async def go():
        await gateway.answered(req["id"], 0, "2")

    run(go())
    assert decisions.load(req["id"])["answer"] == {"action": "nothing"}


def test_a_leaving_request_releases_its_clarifies(hermes):
    req = ask()
    run(gateway.scan())
    entry = hermes._entries[f"mu{req['id']}q0"]
    decisions.transition(req["id"], ("open",), "done", outcome="Answered in the pane")
    run(gateway.scan())
    assert entry.response == gateway.STALE
    assert gateway.S.cids == {}


# -- 6. settling the messages -----------------------------------------------------------------------

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


def test_a_request_that_ended_while_the_gateway_was_down_is_edited_on_the_first_scan():
    req = ask()
    run(gateway.scan())
    decisions.transition(req["id"], ("open",), "stale", outcome="The agent session ended")
    gateway.S = gateway.State()  # a restarted gateway
    gateway.S.adapter, gateway.S.configured = fh.Adapter(), True
    run(gateway.scan())
    assert gateway.S.adapter.edits == [(DM, "101", "The agent session ended")]


# -- 7. the guard -----------------------------------------------------------------------------------

def tap(user, chat):
    update = fh.update(user, chat)
    try:
        run(gateway.guard(update, None))
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


# -- 8. typed replies -------------------------------------------------------------------------------

def dispatch(text, user=4242, chat=4242, reply=None, platform="telegram"):
    return run(gateway.on_dispatch(event=fh.event(text, user, chat, reply, platform), gateway=None))


def presented(hermes, **fields):
    req = ask(**fields)
    run(gateway.scan())
    return req, sent()[-1]["mid"]


def test_a_reply_resolves_the_pending_question(hermes):
    req, mid = presented(hermes)

    async def go():
        got = await gateway.on_dispatch(event=fh.event("beta", "4242", "4242", reply=mid))
        await until(lambda: decisions.load(req["id"])["status"] == "answered")
        return got

    assert run(go()) == {"action": "skip"}
    assert decisions.load(req["id"])["answer"] == {"Which?": "Beta"}


def test_a_reply_to_an_already_resolved_question_says_so(hermes):
    req, mid = presented(hermes)
    hermes.resolve_gateway_clarify(f"mu{req['id']}q0", "Alpha")
    assert dispatch("beta", reply=mid) == {"action": "skip"}
    assert gateway.S.adapter.edits[-1] == (DM, mid, "already handled: answer received")


def test_a_reply_to_a_request_that_is_not_open_says_the_outcome(hermes):
    req, mid = presented(hermes)
    decisions.transition(req["id"], ("open",), "done", outcome="Delivered ✓")
    assert dispatch("x", reply=mid) == {"action": "skip"}
    assert gateway.S.adapter.edits[-1] == (DM, mid, "already handled: Delivered ✓")


def test_a_reply_to_something_else_passes_through(hermes):
    presented(hermes)
    assert dispatch("hello", reply="55555") is None


def test_no_reply_goes_to_the_only_question_awaiting_text(hermes):
    req, _ = presented(hermes)
    assert dispatch("some prose") is None  # not awaiting text yet
    hermes.mark_awaiting_text(f"mu{req['id']}q0")

    async def go():
        got = await gateway.on_dispatch(event=fh.event("my words", "4242", "4242"))
        await until(lambda: decisions.load(req["id"])["status"] == "answered")
        return got

    assert run(go()) == {"action": "skip"}
    assert decisions.load(req["id"])["answer"] == {"Which?": "my words"}


def test_two_questions_awaiting_text_never_guess(hermes):
    a, _ = presented(hermes)
    b, _ = presented(hermes)
    hermes.mark_awaiting_text(f"mu{a['id']}q0")
    hermes.mark_awaiting_text(f"mu{b['id']}q0")
    assert dispatch("words") is None


def test_dispatch_ignores_other_platforms_and_other_people(hermes):
    req, mid = presented(hermes)
    hermes.mark_awaiting_text(f"mu{req['id']}q0")
    assert dispatch("words", platform="slack") is None
    assert dispatch("words", user=999) is None
    assert dispatch("words", chat=999, reply=mid) is None
    assert dispatch("   ") is None
    assert hermes._entries[f"mu{req['id']}q0"].response is None


def test_dispatch_with_nothing_presented_passes_through():
    assert dispatch("words") is None


def test_dispatch_errors_pass_through(monkeypatch):
    async def boom(event):
        raise RuntimeError("x")

    monkeypatch.setattr(gateway, "_dispatch", boom)
    assert dispatch("words") is None


# -- 9. restart -------------------------------------------------------------------------------------

def test_a_request_from_an_earlier_boot_is_represented(hermes):
    req = ask(presented={"boot": "old", "messages": {"0": ["7"]}})
    run(gateway.scan())
    (msg,) = sent()
    assert (DM, "7", "Superseded: see the newer message") in gateway.S.adapter.edits
    assert decisions.load(req["id"])["presented"] == {"boot": gateway.BOOT, "messages": {"0": ["7", msg["mid"]]}}


def test_a_reply_to_a_superseded_message_still_binds(hermes):
    req = ask(presented={"boot": "old", "messages": {"0": ["7"]}})
    run(gateway.scan())

    async def go():
        got = await gateway.on_dispatch(event=fh.event("Alpha", "4242", "4242", reply="7"))
        await until(lambda: decisions.load(req["id"])["status"] == "answered")
        return got

    assert run(go()) == {"action": "skip"}
    assert decisions.load(req["id"])["answer"] == {"Which?": "Alpha"}


def test_a_reply_to_an_old_message_before_re_presenting_passes_through():
    req = ask(presented={"boot": "old", "messages": {"0": ["7"]}})

    async def go():  # one scan that fails to present, so the old id is known but nothing is registered
        gateway.S.adapter.fail_sends = 1
        await gateway.scan()
        return await gateway.on_dispatch(event=fh.event("Alpha", "4242", "4242", reply="7"))

    assert run(go()) is None
    assert decisions.load(req["id"])["status"] == "open"


# -- 10. liveness -----------------------------------------------------------------------------------

def test_a_silent_hook_stales_the_request():
    req = ask(alive=time.time() - 31)
    run(gateway.scan())
    got = decisions.load(req["id"])
    assert (got["status"], got["outcome"]) == ("stale", "The agent is no longer waiting")
    assert not sent()


def test_a_recent_heartbeat_keeps_it():
    req = ask(alive=time.time() - 20)
    run(gateway.scan())
    assert decisions.load(req["id"])["status"] == "open" and len(sent()) == 1


def test_an_answered_request_with_a_silent_hook_is_stale_unless_the_hook_delivered_it():
    a, b = ask(alive=time.time() - 40), ask(alive=time.time() - 40)
    for r in (a, b):
        decisions.transition(r["id"], ("open",), "answered", answer={"Which?": "Alpha"})
    decisions.update(b["id"], delivered_by_hook=True)
    run(gateway.scan())
    assert decisions.load(a["id"])["status"] == "stale"
    assert decisions.load(b["id"])["status"] == "answered"  # settle will finish it


def test_a_permission_request_whose_pane_is_not_blocked_is_answered_in_the_pane(monkeypatch):
    req = permission()
    monkeypatch.setattr(core, "agent_at", lambda pane: {"agent_status": "idle"})
    run(gateway.scan())
    got = decisions.load(req["id"])
    assert (got["status"], got["outcome"]) == ("stale", "Answered in the pane")
    assert not sent()


def test_a_missing_agent_also_stales_a_permission_request(monkeypatch):
    req = permission()
    monkeypatch.setattr(core, "agent_at", lambda pane: None)
    run(gateway.scan())
    assert decisions.load(req["id"])["status"] == "stale"


def test_herdr_is_looked_at_most_every_ten_seconds(monkeypatch):
    req = permission()
    looks = []
    monkeypatch.setattr(core, "agent_at", lambda pane: looks.append(pane) or {"agent_status": "blocked"})
    for _ in range(3):
        run(gateway.scan())
    assert looks == ["p1"] and decisions.load(req["id"])["status"] == "open"
    gateway.S.pane_checked[req["id"]] -= 11
    run(gateway.scan())
    assert len(looks) == 2


def test_a_pane_working_again_means_the_dialog_was_answered_there(monkeypatch):
    # a deny in the pane fires no PostToolUse: the pane leaving `blocked` is the only sign
    req = permission()
    monkeypatch.setattr(core, "agent_at", lambda pane: {"agent_status": "working"})
    run(gateway.scan())
    got = decisions.load(req["id"])
    assert (got["status"], got["outcome"]) == ("stale", "Answered in the pane")


def test_an_answered_permission_is_not_staled_while_the_hook_picks_it_up(monkeypatch):
    req = permission()
    decisions.transition(req["id"], ("open",), "answered", answer={"decision": "allow"})
    monkeypatch.setattr(core, "agent_at", lambda pane: {"agent_status": "working"})
    run(gateway.scan())
    assert decisions.load(req["id"])["status"] == "answered"


def test_a_herdr_error_is_not_an_answer(monkeypatch):
    req = permission()

    def boom(pane):
        raise core.CommandError("herdr down")

    monkeypatch.setattr(core, "agent_at", boom)
    run(gateway.scan())
    assert decisions.load(req["id"])["status"] == "open"


def test_a_question_request_is_not_checked_against_herdr(monkeypatch):
    ask()
    monkeypatch.setattr(core, "agent_at", lambda pane: pytest.fail("looked"))
    run(gateway.scan())


# -- 11. registration -------------------------------------------------------------------------------

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
        assert gw.CTX is ctx and gw.S.task is None and gw.S.lock_fd is None  # nothing ran at register time
    finally:
        for key in [k for k in sys.modules if k.startswith("hermes_plugins.muster_gw.")]:
            sys.modules.pop(key, None)


def test_plugin_yaml_discloses_the_gateway_pieces():
    text = (ROOT / "plugin.yaml").read_text()
    assert "Telegram callback guard" in text and "pre_gateway_dispatch" in text and "approver's click" in text


def test_a_delivered_answer_claude_never_confirmed_is_closed_after_an_hour():
    old, recent = ask(alive=time.time() - 3601), ask(alive=time.time() - 60)
    for r in (old, recent):
        decisions.transition(r["id"], ("open",), "answered", answer={"Which?": "Alpha"}, delivered_by_hook=True)
    run(gateway.scan())
    got = decisions.load(old["id"])
    assert (got["status"], got["outcome"]) == ("done", "Answered; muster could not confirm where")
    assert decisions.load(recent["id"])["status"] == "answered"


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
