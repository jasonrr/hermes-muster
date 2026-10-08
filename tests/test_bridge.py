"""bridge: a PermissionRequest hook that waits for an answer from the channel and settles on what Claude did."""

import json
import os
import signal
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
    bridge.TERMINATED.clear()


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
    assert req["tool"] == {"name": "AskUserQuestion"} and "card" not in req
    assert req["proposal"] == {"version": 2, "sha": "abc"} and not (tmp_path / core.PIN_FILE).exists()
    assert req["run"] == {"repo": "o/r", "branch": "muster/5", "pane": "w_1:p2", "issue": 5, "kind": "issue"}
    assert req["alive"] > 0


def test_answered_question_prints_the_updated_input_and_is_done(tmp_path, capsys):
    thread = answer_when_open(answer={"Which db?": "free text"})
    assert bridge.wait(tmp_path, LINK, ASK) == 0
    thread.join()
    assert out(capsys) == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
        "behavior": "allow", "updatedInput": {**ASK["tool_input"], "answers": {"Which db?": "free text"}}}}}
    assert (only()["status"], only()["outcome"]) == ("done", "Delivered ✓")


def test_the_hook_leaves_no_marker_files(tmp_path):
    thread = answer_when_open(answer={"Which db?": "pg"})
    bridge.wait(tmp_path, LINK, ASK)
    thread.join()
    assert not (tmp_path / "muster-decisions").exists()


def test_sigterm_from_claude_means_the_pane_answered(tmp_path, capsys):
    # Claude sends the hook SIGTERM when the pane's dialog is answered first (seen live, 2.1.295)
    def term():
        while not decisions.open_requests():
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)
    thread = threading.Thread(target=term)
    thread.start()
    bridge.wait(tmp_path, LINK, BASH)
    thread.join()
    req = only()
    assert out(capsys) is None and req["status"] == "stale" and req["outcome"] == "Answered in the pane"


def test_the_pane_is_read_from_the_launch_for_an_adhoc_run(tmp_path):
    run = {"card": "t_led", "repo": "o/r", "branch": "fix/x", "launch": {"pane": "w_9:p1"}}
    thread = answer_when_open(answer={"Which db?": "pg"})
    bridge.wait(tmp_path, run, ASK)
    thread.join()
    assert only()["run"]["pane"] == "w_9:p1" and only()["run"]["kind"] == "adhoc"


def test_a_permission_request_carries_the_card_and_no_questions(tmp_path, capsys):
    payload = {**BASH, "tool_input": {"command": "echo ghp_abcdef123456", "description": "Say hi"}}
    thread = answer_when_open(answer={"decision": "allow"})
    bridge.wait(tmp_path, LINK, payload)
    thread.join()
    req = only()
    assert req["kind"] == "permission" and req["tool"] == {"name": "Bash"}
    assert "questions" not in req and "choices" not in req
    assert req["card"] == {"command": "echo [redacted]", "why": "Say hi"}
    assert out(capsys) == {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                  "decision": {"behavior": "allow"}}}


def test_approval_card_for_each_kind_of_tool():
    assert bridge.approval_card("Bash", {"command": "ls"}) == {"command": "ls", "why": "Claude wants to use Bash"}
    edit = bridge.approval_card("Edit", {"file_path": "/a", "token": "ghp_abcdef123456"}, sub="Explore")
    assert "/a" in edit["command"] and "ghp_abcdef123456" not in edit["command"]
    assert edit["why"] == "Claude wants to use Edit (the Explore subagent)"


def test_an_allow_never_carries_a_permission_update():
    # Allow session was dropped: a session rule did not stop the next prompt (seen live), so allow is once only
    req = {"kind": "permission", "tool": {"name": "Bash"}, "answer": {"decision": "allow", "scope": "session"}}
    assert bridge.decision(req, {"command": "ls"}) == {"behavior": "allow"}


def test_allow_once_is_done_as_allowed(tmp_path):
    thread = answer_when_open(answer={"decision": "allow"})
    bridge.wait(tmp_path, LINK, BASH)
    thread.join()
    assert (only()["status"], only()["outcome"]) == ("done", "Allowed ✓")


def test_deny_with_typed_text_carries_the_message(tmp_path, capsys):
    thread = answer_when_open(answer={"decision": "deny", "message": "use make"})
    bridge.wait(tmp_path, LINK, BASH)
    thread.join()
    assert out(capsys)["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "use make"}
    assert (only()["status"], only()["outcome"]) == ("done", "Denied ✓: use make")


def test_plain_deny_has_no_message(tmp_path, capsys):
    thread = answer_when_open(answer={"decision": "deny"})
    bridge.wait(tmp_path, LINK, BASH)
    thread.join()
    assert out(capsys)["hookSpecificOutput"]["decision"] == {"behavior": "deny"}
    assert only()["outcome"] == "Denied ✓"


def test_hermess_timeout_deny_is_reported_as_no_answer_in_time(tmp_path, capsys):
    thread = answer_when_open(answer={"decision": "deny", "timeout": True, "message": "No answer in time."})
    bridge.wait(tmp_path, LINK, BASH)
    thread.join()
    assert out(capsys)["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "No answer in time."}
    assert only()["outcome"] == "No answer in time: denied"


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
    assert out(capsys) is None and req["status"] == "stale" and "No answer in 24 h" in req["outcome"]


def test_a_permission_prompt_waits_only_ten_minutes_then_is_denied(tmp_path, capsys, monkeypatch):
    assert bridge.PERMISSION_DEADLINE == 600
    monkeypatch.setattr(bridge, "PERMISSION_DEADLINE", 0.05)  # DEADLINE (questions) stays 24 h
    bridge.wait(tmp_path, LINK, BASH)
    req = only()
    decision = out(capsys)["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny" and "10 minutes" in decision["message"]
    assert req["status"] == "stale" and req["outcome"] == "No answer in 10 min: denied"


def test_a_subagent_prompt_is_sent_when_the_gateway_is_up(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(decisions, "gateway_up", lambda: True)
    thread = answer_when_open(answer={"decision": "allow"})
    bridge.wait(tmp_path, LINK, {**BASH, "agent_id": "a1", "agent_type": "general-purpose"})
    thread.join()
    req = only()
    assert out(capsys)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert req["subagent"] == "general-purpose"
    assert req["card"]["why"] == "Claude wants to use Bash (the general-purpose subagent)"


def test_a_subagent_prompt_with_the_gateway_down_goes_straight_to_the_pane(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(decisions, "gateway_up", lambda: False)
    assert bridge.wait(tmp_path, LINK, {**BASH, "agent_id": "a1", "agent_type": "Explore"}) == 0
    assert out(capsys) is None and decisions.for_ledger("t_led") == []
    assert "gateway down" in core.log_path("bridge").read_text()


def test_a_main_agent_prompt_is_held_whatever_the_gateway_state(tmp_path, monkeypatch):
    monkeypatch.setattr(decisions, "gateway_up", lambda: False)  # the pane can still answer it meanwhile
    thread = answer_when_open(answer={"decision": "deny"})
    bridge.wait(tmp_path, LINK, BASH)
    thread.join()
    assert only()["outcome"] == "Denied ✓"


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
    monkeypatch.setattr(bridge, "poll", lambda rid, tool_input: (_ for _ in ()).throw(core.CommandError("disk full")))
    assert bridge.wait(tmp_path, LINK, ASK) == 0
    assert out(capsys) is None and only()["status"] == "stale"
    assert "disk full" in core.log_path("bridge").read_text()


def test_an_error_before_the_request_exists_prints_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(decisions, "create", lambda *a, **k: 1 / 0)
    assert bridge.wait(tmp_path, LINK, ASK) == 0 and out(capsys) is None


def test_an_unreadable_answer_prints_nothing(tmp_path, capsys):
    thread = answer_when_open(answer="garbage")
    bridge.wait(tmp_path, LINK, ASK)
    thread.join()
    assert out(capsys) is None
