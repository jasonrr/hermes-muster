"""bridge: a PermissionRequest hook that waits for an answer from the channel and settles on what Claude did."""

import hashlib
import json
import os
import threading
import time

import pytest

from muster import bridge, core, decisions

ASK = {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion", "cwd": "/wt",
       "tool_input": {"questions": [
           {"question": "Which db?", "header": "DB", "multiSelect": False,
            "options": [{"label": "pg", "description": "Postgres"}, {"label": "pg", "description": "again"}]}]}}
BASH = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
LINK = {"card": "t_led", "repo": "o/r", "issue": 5, "pane": "w_1:p2", "branch": "muster/5"}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(bridge, "POLL", 0.01)
    monkeypatch.setattr(bridge, "ALIVE_EVERY", 0.02)
    monkeypatch.setattr(bridge, "BLOCKED_WAIT", 0.1)
    monkeypatch.setattr(core, "agent_at", lambda pane: {"agent_status": "blocked"})


def out(capsys):
    text = capsys.readouterr().out
    return json.loads(text) if text else None


def answer_when_open(**fields):
    """A thread standing in for the gateway: answers the first open request it sees."""
    def go():
        for _ in range(500):
            found = decisions.open_requests()
            if found:
                decisions.transition(found[0]["id"], ("open",), "answered", **fields)
                return
            time.sleep(0.01)
    thread = threading.Thread(target=go)
    thread.start()
    return thread


def only():
    return decisions.for_ledger("t_led")[0]


def sha(tool_input):
    return hashlib.sha256(json.dumps(tool_input, sort_keys=True).encode()).hexdigest()


def test_question_request_is_created_with_everything_the_plan_lists(tmp_path):
    (tmp_path / core.WAIT_KIND).write_text("t_wait")
    core.save_json(tmp_path / core.PIN_FILE, {"version": 2, "sha": "abc"})
    thread = answer_when_open(answer={"Which db?": "pg"})
    bridge.wait(tmp_path, LINK, ASK)
    thread.join()
    req = only()
    assert req["kind"] == "question" and req["ledger"] == "t_led" and req["wait"] == "t_wait"
    assert req["questions"] == [{"text": "Which db?", "header": "DB", "multi": False, "options": [
        {"label": "pg", "description": "Postgres"}, {"label": "pg", "description": "again"}]}]
    assert req["choices"] == [["pg", "pg (2)"]]
    assert req["tool"] == {"name": "AskUserQuestion", "input_sha": sha(ASK["tool_input"])}
    assert req["proposal"] == {"version": 2, "sha": "abc"} and not (tmp_path / core.PIN_FILE).exists()
    assert req["run"] == {"repo": "o/r", "branch": "muster/5", "pane": "w_1:p2", "issue": 5, "kind": "issue"}
    assert req["alive"] > 0 and req["blocked_seen"] is True  # the gateway may now read `not blocked` as answered


def test_answered_question_prints_the_updated_input_and_marks_delivery(tmp_path, capsys):
    thread = answer_when_open(answer={"Which db?": "free text"})
    assert bridge.wait(tmp_path, LINK, ASK) == 0
    thread.join()
    assert out(capsys) == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
        "behavior": "allow", "updatedInput": {**ASK["tool_input"], "answers": {"Which db?": "free text"}}}}}
    req = only()
    assert req["status"] == "answered" and req["delivered_by_hook"] is True


def test_a_marker_per_request_is_written_before_the_wait(tmp_path):
    thread = answer_when_open(answer={"Which db?": "pg"})
    bridge.wait(tmp_path, LINK, ASK)
    thread.join()
    assert [p.name for p in (tmp_path / "muster-decisions").iterdir()] == [only()["id"]]


def test_a_pane_that_never_blocks_stales_the_request_with_no_output(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(core, "agent_at", lambda pane: {"agent_status": "working"})
    bridge.wait(tmp_path, LINK, ASK)
    assert out(capsys) is None
    req = only()
    assert req["status"] == "stale" and "before muster could ask" in req["outcome"] and "blocked_seen" not in req


def test_the_blocked_check_retries(tmp_path, monkeypatch):
    seen = iter(["working", "working", "blocked"])
    monkeypatch.setattr(core, "agent_at", lambda pane: {"agent_status": next(seen)})
    thread = answer_when_open(answer={"Which db?": "pg"})
    bridge.wait(tmp_path, LINK, ASK)
    thread.join()
    assert only()["status"] == "answered"


def test_the_pane_is_read_from_the_launch_for_an_adhoc_run(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(core, "agent_at", lambda pane: asked.append(pane) or {"agent_status": "blocked"})
    run = {"card": "t_led", "repo": "o/r", "branch": "fix/x", "launch": {"pane": "w_9:p1"}}
    thread = answer_when_open(answer={"Which db?": "pg"})
    bridge.wait(tmp_path, run, ASK)
    thread.join()
    assert asked[0] == "w_9:p1" and only()["run"]["pane"] == "w_9:p1" and only()["run"]["kind"] == "adhoc"


def test_a_permission_prompt_is_one_synthetic_question_with_redacted_input(tmp_path, capsys):
    payload = {**BASH, "tool_input": {"command": "echo ghp_abcdef123456"}}
    thread = answer_when_open(answer={"decision": "allow"})
    bridge.wait(tmp_path, LINK, payload)
    thread.join()
    req = only()
    assert req["kind"] == "permission" and req["choices"] == [["Allow once", "Deny"]]
    text = req["questions"][0]["text"]
    assert text.startswith("Allow Bash?") and "ghp_abcdef123456" not in text and "echo" in text
    assert out(capsys) == {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                  "decision": {"behavior": "allow"}}}


def test_deny_with_typed_text_carries_the_message(tmp_path, capsys):
    thread = answer_when_open(answer={"decision": "deny", "message": "use make"})
    bridge.wait(tmp_path, LINK, BASH)
    thread.join()
    assert out(capsys)["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "use make"}


def test_plain_deny_has_no_message(tmp_path, capsys):
    thread = answer_when_open(answer={"decision": "deny"})
    bridge.wait(tmp_path, LINK, BASH)
    thread.join()
    assert out(capsys)["hookSpecificOutput"]["decision"] == {"behavior": "deny"}


def test_a_long_input_offers_only_deny(tmp_path):
    payload = {**BASH, "tool_input": {"command": "x" * 4000}}
    thread = answer_when_open(answer={"decision": "deny"})
    bridge.wait(tmp_path, LINK, payload)
    thread.join()
    req = only()
    assert req["choices"] == [["Deny"]] and "too long" in req["questions"][0]["text"]
    assert "xxxx" not in req["questions"][0]["text"]


@pytest.mark.parametrize("status", ["stale", "done", "failed"])
def test_a_request_that_leaves_open_ends_the_loop_with_no_output(tmp_path, capsys, status):
    def go():
        for _ in range(500):
            if decisions.open_requests():
                decisions.transition(decisions.open_requests()[0]["id"], ("open",), status)
                return
            time.sleep(0.01)
    thread = threading.Thread(target=go)
    thread.start()
    bridge.wait(tmp_path, LINK, ASK)
    thread.join()
    assert out(capsys) is None


def test_a_missing_record_ends_the_loop(tmp_path, capsys):
    def go():
        for _ in range(500):
            found = decisions.open_requests()
            if found:
                (decisions.root() / f"{found[0]['id']}.json").unlink()
                return
            time.sleep(0.01)
    thread = threading.Thread(target=go)
    thread.start()
    bridge.wait(tmp_path, LINK, ASK)
    thread.join()
    assert out(capsys) is None


def test_a_changed_parent_ends_the_loop_silently(tmp_path, capsys, monkeypatch):
    calls = iter([100, 100, 1])
    monkeypatch.setattr(os, "getppid", lambda: next(calls, 1))
    bridge.wait(tmp_path, LINK, ASK)
    assert out(capsys) is None and only()["status"] == "open"


def test_the_deadline_stales_an_open_request(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(bridge, "DEADLINE", 0.05)
    bridge.wait(tmp_path, LINK, ASK)
    req = only()
    assert out(capsys) is None and req["status"] == "stale" and "expired after 24 h" in req["outcome"]


def test_a_permission_prompt_waits_only_ten_minutes_then_leaves_it_to_the_pane(tmp_path, capsys, monkeypatch):
    assert bridge.PERMISSION_DEADLINE == 600
    monkeypatch.setattr(bridge, "PERMISSION_DEADLINE", 0.05)  # DEADLINE (questions) stays 24 h
    bridge.wait(tmp_path, LINK, BASH)
    req = only()
    assert out(capsys) is None and req["status"] == "stale" and "expired after 10 min" in req["outcome"]


def test_an_answer_that_wins_the_deadline_race_is_used(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(bridge, "DEADLINE", 0)
    real = decisions.transition

    def lose(rid, frm, to, **fields):  # the click lands just before the hook takes the lock
        if to == "stale":
            real(rid, ("open",), "answered", answer={"Which db?": "late"})
        return real(rid, frm, to, **fields)
    monkeypatch.setattr(decisions, "transition", lose)
    bridge.wait(tmp_path, LINK, ASK)
    assert out(capsys)["hookSpecificOutput"]["decision"]["updatedInput"]["answers"] == {"Which db?": "late"}


def test_any_error_prints_nothing_logs_and_stales(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(core, "agent_at", lambda pane: (_ for _ in ()).throw(core.CommandError("herdr down")))
    assert bridge.wait(tmp_path, LINK, ASK) == 0
    assert out(capsys) is None and only()["status"] == "stale"
    assert "herdr down" in core.log_path("bridge").read_text()


def test_an_error_before_the_request_exists_prints_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(decisions, "create", lambda *a, **k: 1 / 0)
    assert bridge.wait(tmp_path, LINK, ASK) == 0 and out(capsys) is None


def test_an_unreadable_answer_prints_nothing(tmp_path, capsys):
    thread = answer_when_open(answer="garbage")
    bridge.wait(tmp_path, LINK, ASK)
    thread.join()
    assert out(capsys) is None


# -- settle ---------------------------------------------------------------------------------------

def post(base, event="PostToolUse", **extra):
    return {"hook_event_name": event, "tool_name": base["tool_name"], "tool_input": base["tool_input"], **extra}


def marked(tmp_path, base, **fields):
    """A request as the hook leaves it: created, marked; `fields` set its state."""
    req = decisions.create("question" if base["tool_name"] == "AskUserQuestion" else "permission", "t_led",
                           tool={"name": base["tool_name"], "input_sha": sha(base["tool_input"])})
    (tmp_path / "muster-decisions").mkdir(exist_ok=True)
    (tmp_path / "muster-decisions" / req["id"]).write_text("")
    if fields:
        decisions.transition(req["id"], ("open",), fields.pop("status", "answered"), **fields)
    return req["id"]


def finished(rid):
    req = decisions.load(rid)
    return req["status"], req["outcome"]


def test_settle_delivered_when_the_responses_match(tmp_path):
    rid = marked(tmp_path, ASK, answer={"Which db?": "pg"}, delivered_by_hook=True)
    bridge.settle(tmp_path, post(ASK, tool_response={"answers": {"Which  db?": " pg "}}))
    assert finished(rid) == ("done", "Delivered ✓")
    assert not (tmp_path / "muster-decisions" / rid).exists()


def test_settle_answered_in_the_pane_when_they_differ(tmp_path):
    rid = marked(tmp_path, ASK, answer={"Which db?": "pg"}, delivered_by_hook=True)
    bridge.settle(tmp_path, post(ASK, tool_response={"answers": {"Which db?": "mysql"}}))
    assert finished(rid) == ("done", "Answered in the pane: mysql")


def test_settle_cannot_confirm_unreadable_answers(tmp_path):
    rid = marked(tmp_path, ASK, answer={"Which db?": "pg"}, delivered_by_hook=True)
    bridge.settle(tmp_path, post(ASK, tool_response="opaque"))
    assert finished(rid) == ("done", "Answered; muster could not confirm where")


def test_settle_an_open_request_was_answered_in_the_pane(tmp_path):
    rid = marked(tmp_path, ASK)
    bridge.settle(tmp_path, post(ASK, tool_response={"answers": {"Which db?": "pg"}}))
    assert finished(rid) == ("done", "Answered in the pane")


def test_settle_permission_outcomes(tmp_path):
    allowed = marked(tmp_path, BASH, answer={"decision": "allow"}, delivered_by_hook=True)
    bridge.settle(tmp_path, post(BASH))
    assert finished(allowed) == ("done", "Allowed ✓")
    denied = marked(tmp_path, BASH, answer={"decision": "deny"}, delivered_by_hook=True)
    bridge.settle(tmp_path, post(BASH))
    assert finished(denied) == ("done", "Allowed in the pane")
    failed = marked(tmp_path, BASH, answer={"decision": "allow"}, delivered_by_hook=True)
    bridge.settle(tmp_path, post(BASH, "PostToolUseFailure"))
    assert finished(failed) == ("done", "Finished; muster could not confirm the decision")


def test_settle_ignores_a_different_tool_or_input(tmp_path):
    rid = marked(tmp_path, BASH, answer={"decision": "allow"}, delivered_by_hook=True)
    bridge.settle(tmp_path, post({"tool_name": "Edit", "tool_input": BASH["tool_input"]}))
    bridge.settle(tmp_path, post({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    assert decisions.load(rid)["status"] == "answered"
    assert (tmp_path / "muster-decisions" / rid).exists()


def test_settle_ignores_other_events(tmp_path):
    rid = marked(tmp_path, ASK)
    bridge.settle(tmp_path, {**post(ASK), "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    bridge.settle(tmp_path, {k: v for k, v in post(ASK).items() if k != "hook_event_name"})
    assert decisions.load(rid)["status"] == "open"


def test_settle_matches_an_ask_whose_input_gained_the_answers(tmp_path):
    rid = marked(tmp_path, ASK, answer={"Which db?": "pg"}, delivered_by_hook=True)
    shown = {**ASK, "tool_input": {**ASK["tool_input"], "answers": {"Which db?": "pg"}}}
    bridge.settle(tmp_path, post(shown, tool_response={"answers": {"Which db?": "pg"}}))
    assert finished(rid) == ("done", "Delivered ✓")


def test_settle_with_no_markers_is_a_no_op(tmp_path):
    bridge.settle(tmp_path, post(BASH))


def test_settle_racing_the_hook_gives_one_outcome_and_no_output(tmp_path, capsys):
    """The pane answers while the hook loops: settle finishes the request, the hook sees it and prints nothing."""
    def pane():
        for _ in range(500):
            if (tmp_path / "muster-decisions").exists() and decisions.open_requests():
                bridge.settle(tmp_path, post(ASK, tool_response={"answers": {"Which db?": "pane"}}))
                return
            time.sleep(0.01)
    thread = threading.Thread(target=pane)
    thread.start()
    bridge.wait(tmp_path, LINK, ASK)
    thread.join()
    assert out(capsys) is None
    assert finished(only()["id"]) == ("done", "Answered in the pane")


def test_settle_never_raises(tmp_path, monkeypatch):
    rid = marked(tmp_path, BASH)
    monkeypatch.setattr(decisions, "load", lambda r: 1 / 0)
    bridge.settle(tmp_path, post(BASH))
    assert rid


# -- session end ----------------------------------------------------------------------------------

def test_session_end_stales_open_and_answered_requests_and_removes_the_markers(tmp_path):
    a = marked(tmp_path, ASK)
    b = marked(tmp_path, BASH, answer={"decision": "allow"})
    bridge.session_end(tmp_path)
    assert decisions.load(a)["status"] == decisions.load(b)["status"] == "stale"
    assert not list((tmp_path / "muster-decisions").glob("*"))


def test_session_end_with_nothing_is_a_no_op(tmp_path):
    bridge.session_end(tmp_path)
