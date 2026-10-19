import json
import os
import time

from muster import config, launcher, progress

NOW = 1_800_000_000.0
L = "t_run1"


def hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def at(kind, t, text=None):
    progress.activity(L).mkdir(parents=True, exist_ok=True)
    path = progress.activity(L) / kind
    path.write_text(text or "")
    os.utime(path, (t, t))


def launched(t=NOW - 3600, sha="abc"):
    directory = config.data_dir() / "intake" / L
    directory.mkdir(parents=True, exist_ok=True)
    at("launched", t, json.dumps({"dir": str(directory), "sha256": sha}))
    return directory


def received(directory, t, sha="abc"):
    with open(directory / "prompt-seen.jsonl", "a") as out:
        out.write(json.dumps({"sha256": sha, "at": int(t), "session": "s"}) + "\n")


def state(waits=(), ref=None, archived=False, now=NOW):
    return progress.state(L, now, list(waits), ref, archived)


def test_launch_without_confirmed_receipt_says_so():
    launched(NOW - 120)
    key, text, final = state()
    assert key == "launched" and not final
    assert text == f"Dispatched {hm(NOW - 120)} · waiting for the builder to start"


def test_a_verified_review_outranks_a_missing_receipt():
    directory = launched()
    received(directory, NOW - 60, sha="other")  # no matching receipt record
    at("review", NOW - 600, "https://github.com/o/r/pull/9")
    assert state() == (f"review:{NOW - 600}", "PR #9 opened · Coordinator is reviewing · no action needed", False)


def test_a_turn_end_outranks_a_missing_receipt():
    launched()
    at("stop", NOW - 300)
    key, text, _ = state()
    assert key == "turn" and "Started" not in text  # no receipt time is made up


def test_a_prompt_that_is_not_the_brief_confirms_nothing():
    directory = launched()
    received(directory, NOW - 60, sha="other")
    at("prompt", NOW - 60)
    assert state()[0] == "launched"


def test_confirmed_receipt_without_a_tool_is_not_called_tool_activity():
    directory = launched()
    received(directory, NOW - 120)
    at("prompt", NOW - 120)
    key, text, _ = state()
    assert key == "received"
    assert text == f"Started {hm(NOW - 120)} · no tool activity yet"


def test_a_later_prompt_does_not_move_the_receipt_time():
    directory = launched()
    received(directory, NOW - 600)
    received(directory, NOW - 60, sha="later")
    at("prompt", NOW - 60)
    at("tool", NOW - 30)
    at("tool", NOW - 30 * 60)
    assert state()[1] == f"Quiet 30 min · last tool {hm(NOW - 1800)} · long tools can be quiet"
    (progress.activity(L) / "tool").unlink()
    assert state(now=NOW - 30)[1].startswith(f"Started {hm(NOW - 600)} · ")


def test_working_shows_the_last_tool_time_and_its_age():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 180)
    key, text, _ = state()
    assert key == "working"
    assert text == f"Working · last activity 3 min ago ({hm(NOW - 180)})"


def test_a_long_running_build_with_fresh_activity_is_still_working():
    directory = launched(NOW - 7200)
    received(directory, NOW - 7200)
    at("tool", NOW - 60)
    assert state()[0] == "working"


def test_silence_is_unknown_never_a_failure():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 20 * 60)
    key, text, final = state()
    assert key == "quiet" and not final and "stalled" not in text
    assert text == f"Quiet 20 min · last tool {hm(NOW - 1200)} · long tools can be quiet"


def test_no_tool_for_long_after_receipt_is_quiet_too():
    directory = launched()
    received(directory, NOW - 20 * 60)
    key, text, _ = state()
    assert key == "quiet" and text == f"Quiet 20 min · started {hm(NOW - 1200)}, no tool activity yet · long tools can be quiet"


def test_an_open_question_stays_waiting_whatever_tool_finishes():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 10)  # a parallel subagent's tool, newer than the question
    key, text, _ = state(waits=[(NOW - 300, "question"), (NOW - 100, "question"), (NOW - 50, "permission")])
    assert key == f"waiting:3:{NOW - 300}"
    assert text == f"Needs your decision: 2 questions, permission · since {hm(NOW - 300)}"


def test_once_the_request_ends_the_message_shows_activity_again():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 10)
    assert state(waits=[])[0] == "working"


def test_review_ready_until_a_send_back_arrives():
    directory = launched()
    received(directory, NOW - 3000)
    at("prompt", NOW - 900)
    at("review", NOW - 600, "https://github.com/o/r/pull/9")
    at("tool", NOW - 590)  # the done command's own PostToolUse
    at("stop", NOW - 580)
    key, text, _ = state()
    assert key == f"review:{NOW - 600}"
    assert text == "PR #9 opened · Coordinator is reviewing · no action needed"
    at("prompt", NOW - 60)  # the send-back
    at("tool", NOW - 30)
    assert state()[0] == "working"


def test_an_empty_review_file_still_reads():
    directory = launched()
    received(directory, NOW - 3000)
    at("review", NOW - 600)
    assert state()[1] == "the pull request opened · Coordinator is reviewing · no action needed"


def test_a_turn_end_with_nothing_open_says_the_agent_waits_at_its_prompt():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 400)
    at("stop", NOW - 300)
    assert state() == ("turn", f"Turn ended {hm(NOW - 300)} · the builder is waiting at its prompt, no question open",
                       False)


def test_session_end():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 400)
    at("end", NOW - 300)
    assert state() == ("ended", f"Session ended {hm(NOW - 300)} · the builder is not running", False)


def test_finished_only_once_the_work_resolved():
    launched()
    assert state(ref={"state": "closed", "closed_by": "human"})[0] == "launched"  # a human's close is not finished
    assert state(ref={"state": "closing", "resolved_at": 5}) == ("finished", "Finished · the pull request merged", True)
    at("review", NOW - 600, "https://github.com/o/r/pull/9")
    assert state(ref={"state": "closing", "resolved_at": 5})[1] == "Finished · PR #9 merged"


def test_archived_is_final():
    launched()
    assert state(archived=True) == ("archived", "Archived", True)
    assert state(ref={"state": "deleted"}) == ("archived", "Archived", True)


def test_due():
    saved = {"key": "working", "text": "a", "edited_at": NOW}
    assert not progress.due(saved, "working", "b", NOW + 299)
    assert progress.due(saved, "working", "b", NOW + 300)
    assert not progress.due(saved, "working", "a", NOW + 900)
    assert progress.due(saved, "turn", "a", NOW + 1)
    assert progress.due({}, "launched", "x", NOW)


def test_hooked_maps_hook_events_and_never_raises():
    launched()
    progress.hooked(L, "prompt", {"hook_event_name": "UserPromptSubmit"})
    progress.hooked(L, "prompt", {"hook_event_name": "PostToolUseFailure"})
    progress.hooked(L, "stop", {})
    progress.hooked(L, "session-end", {})
    progress.hooked(L, "notification", {})
    progress.hooked(L, "prompt", ["not", "a", "dict"])
    assert sorted(p.name for p in progress.activity(L).iterdir()) == ["end", "launched", "prompt", "stop", "tool"]


def test_without_a_launched_run_nothing_is_written():
    progress.hooked("t_old", "prompt", {"hook_event_name": "PostToolUse"})
    progress.touch("t_old", "review", "url")
    assert not progress.activity("t_old").exists()


def test_tracked_skips_final_runs():
    launched()
    assert progress.tracked() == [L]
    progress.save(L, final=True)
    assert progress.tracked() == []


def test_seen_at_returns_the_matching_records_time():
    directory = launched()
    received(directory, 1234)
    assert launcher.seen_at(directory, "abc") == 1234 and launcher.seen(directory, "abc") is True
    assert launcher.seen_at(directory, "zzz") is None and launcher.seen(directory, "zzz") is False


def test_a_recover_of_a_run_from_before_the_feature_starts_no_tracking():
    progress.launched("t_old", "/d", "sha", again=True)
    assert not progress.activity("t_old").exists()
    launched()
    progress.launched(L, "/d", "sha", again=True)
    assert json.loads((progress.activity(L) / "launched").read_text())["dir"] == "/d"


def ask(rid, *headers, pin=None, partial=None, kind="question", **fields):
    return {"id": rid, "kind": kind, "ledger": L, "status": "open", "created_at": NOW - 100,
            "questions": [{"text": h, "header": h, "options": []} for h in headers], "proposal": pin,
            "partial": partial or {}, **fields}


PIN = {"version": 2, "sha": "abcdef012345"}


def test_an_approval_under_review_alone_reads_reviewing_not_needs_your_decision():
    directory = launched()
    received(directory, NOW - 3000)
    at("recommended", NOW - 900)
    at("proposal", NOW - 600, "v2")
    reqs = [ask("a", "Approval", pin=PIN)]
    assert progress.needs(reqs) == {L: []}
    assert state(waits=progress.needs(reqs)[L]) == (f"reviewing:{NOW - 600}",
                                                    "Coordinator is reviewing design v2 · no action needed", False)


def test_a_separate_question_or_permission_still_needs_your_decision_while_a_plan_is_reviewed():
    directory = launched()
    received(directory, NOW - 3000)
    at("proposal", NOW - 600, "v2")
    reqs = [ask("a", "Approval", pin=PIN), ask("q", "Which?"), ask("p", kind="permission")]
    waits = progress.needs(reqs)[L]
    assert [w for _, w in waits] == ["question", "permission"]
    assert state(waits=waits)[1] == f"Needs your decision: question, permission · since {hm(NOW - 100)}"


def test_a_bundled_approval_counts_only_its_other_questions_while_under_review():
    at("proposal", NOW - 600, "v2")
    assert [w for _, w in progress.needs([ask("a", "Approval", "Which?", pin=PIN)])[L]] == ["question"]
    answered = ask("a", "Approval", "Which?", pin=PIN, partial={"1": ["X", [0]]})
    assert progress.needs([answered]) == {L: []}


def test_the_open_design_review_is_the_decision_not_the_approval_it_answers():
    at("proposal", NOW - 600, "v2")
    at("recommended", NOW - 300)
    reqs = [ask("a", "Approval", pin=PIN), ask("d", "Design", kind="design", pin=PIN)]
    assert [w for _, w in progress.needs(reqs)[L]] == ["design v2"]


def test_an_approval_already_reviewed_with_no_review_card_open_needs_your_decision():
    at("proposal", NOW - 600, "v2")
    at("recommended", NOW - 300)  # reviewed; the human tapped Leave pending, so no design request is open
    assert [w for _, w in progress.needs([ask("a", "Approval", pin=PIN)])[L]] == ["question"]


def test_build_and_feedback_requests_name_what_waits():
    reqs = [ask("b", "Build", kind="build", pr="https://github.com/o/r/pull/47"), ask("f", "Feedback", kind="feedback")]
    assert [w for _, w in progress.needs(reqs)[L]] == ["PR #47", "feedback to send"]


def test_delivered_feedback_reads_revision_pending_until_the_revision_is_reviewed():
    directory = launched()
    received(directory, NOW - 3000)
    at("review", NOW - 900, "https://github.com/o/r/pull/9")
    at("recommended", NOW - 800)
    assert state()[1] == "PR #9 open · Coordinator reviewed it"
    at("prompt", NOW - 600)
    at("revision", NOW - 600)
    at("tool", NOW - 120)
    assert state() == (f"revision:{NOW - 600}", f"Feedback delivered {hm(NOW - 600)} · revision pending · last "
                                                f"activity 2 min ago ({hm(NOW - 120)})", False)
    at("review", NOW - 60, "https://github.com/o/r/pull/9")  # the revision, verified
    assert state()[1] == "PR #9 opened · Coordinator is reviewing · no action needed"


def test_a_decision_outlives_the_builders_session():
    directory = launched()
    received(directory, NOW - 3000)
    at("end", NOW - 300)
    assert state(waits=[(NOW - 100, "PR #9")])[1] == f"Needs your decision: PR #9 · since {hm(NOW - 100)}"
