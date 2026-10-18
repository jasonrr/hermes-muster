import json
import os
import time

from muster import config, core, progress

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
    assert text == f"🚀 Launched {hm(NOW - 120)} · brief receipt not confirmed yet."


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
    assert text == f"Brief received {hm(NOW - 120)} · no tool activity observed yet"


def test_a_later_prompt_does_not_move_the_receipt_time():
    directory = launched()
    received(directory, NOW - 600)
    received(directory, NOW - 60, sha="later")
    at("prompt", NOW - 60)
    at("tool", NOW - 30)
    assert state()[1].startswith(f"Brief received {hm(NOW - 600)} · ")


def test_working_shows_the_last_tool_time_and_its_age():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 180)
    key, text, _ = state()
    assert key == "working"
    assert text.endswith(f"last tool activity 3 min ago ({hm(NOW - 180)}, as of {hm(NOW)})")


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
    assert text.endswith(f"no new activity observed for 20 min (last tool {hm(NOW - 1200)}). Long tools can be "
                         f"quiet; muster takes no action.")


def test_no_tool_for_long_after_receipt_is_quiet_too():
    directory = launched()
    received(directory, NOW - 20 * 60)
    key, text, _ = state()
    assert key == "quiet" and text.endswith("no tool activity observed yet (20 min)")


def test_an_open_question_stays_waiting_whatever_tool_finishes():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 10)  # a parallel subagent's tool, newer than the question
    key, text, _ = state(waits=[NOW - 300, NOW - 100])
    assert key == f"waiting:2:{NOW - 300}"
    assert text.startswith(f"✋ Waiting for you since {hm(NOW - 300)} (2 open) · last tool activity 0 min ago")


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
    assert text == f"🔍 Ready for review: https://github.com/o/r/pull/9 (since {hm(NOW - 600)})"
    at("prompt", NOW - 60)  # the send-back
    at("tool", NOW - 30)
    assert state()[0] == "working"


def test_an_empty_review_file_still_reads():
    directory = launched()
    received(directory, NOW - 3000)
    at("review", NOW - 600)
    assert state()[1] == f"🔍 Ready for review (since {hm(NOW - 600)})"


def test_a_turn_end_with_nothing_open_says_the_agent_waits_at_its_prompt():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 400)
    at("stop", NOW - 300)
    assert state() == ("turn", f"💬 Turn ended at {hm(NOW - 300)}: the agent waits at its prompt (no question open).",
                       False)


def test_session_end():
    directory = launched()
    received(directory, NOW - 3000)
    at("tool", NOW - 400)
    at("end", NOW - 300)
    assert state() == ("ended", f"⛔ Session ended at {hm(NOW - 300)}.", False)


def test_finished_only_once_the_work_resolved():
    launched()
    assert state(ref={"state": "closed", "closed_by": "human"})[0] == "launched"  # a human's close is not finished
    assert state(ref={"state": "closing", "resolved_at": 5}) == ("finished", "✅ Finished: pull request merged.", True)


def test_archived_is_final():
    launched()
    assert state(archived=True) == ("archived", "Archived.", True)
    assert state(ref={"state": "deleted"}) == ("archived", "Archived.", True)


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
    assert core.seen_at(directory, "abc") == 1234 and core.seen(directory, "abc") is True
    assert core.seen_at(directory, "zzz") is None and core.seen(directory, "zzz") is False
