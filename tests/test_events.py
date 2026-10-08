"""events: hooks in a muster pane open one wait card per wait and move the ledger at most once each way."""

import argparse
import hashlib
import io
import json
import os
import time

import pytest

import muster.config as config
import muster.core as core
import muster.decisions as decisions
import muster.events as events
import muster.runs as runs

LINKS = {"card": "t_abc123", "repo": "acme/app", "issue": 397, "pane": "p_agent",
         "workspace": "w_1", "worktree": "/wt"}
PR = "https://github.com/acme/app/pull/12"


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A board whose moves follow the real CLI as pinned by tests/test_kanban_contract.py."""
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    (git_dir / core.CARD_FILE).write_text(json.dumps(LINKS))
    state = {"cards": {"t_abc123": "ready"}, "blocks": {}, "keys": {}, "calls": [], "flaky": 0, "git_dir": git_dir,
             "fail": {}, "mode": "notify+wake", "on_create": None, "head": "muster/397", "comments": {}, "bodies": {}}

    def fake_run(argv):
        state["calls"].append(argv)
        if argv[0] == "git":
            return f"{git_dir}\n"
        if argv[:3] == ["gh", "pr", "view"]:
            assert argv[3].startswith("https://github.com/") and argv[4:] == ["--json", "headRefName"], argv
            if state["head"] is None:
                raise core.CommandError("gh pr view: exit 1\nHTTP 502")
            return json.dumps({"headRefName": state["head"]})
        verb, cards = argv[4], state["cards"]
        if verb == "show":  # as the real CLI (test_kanban_contract): the body and every comment, whole
            return json.dumps({"task": {"id": argv[5], "status": cards[argv[5]], "body": state["bodies"].get(argv[5])},
                               "comments": [{"author": "default", "body": b} for b in state["comments"].get(argv[5], [])]})
        if verb == "notify-list":
            return json.dumps([{"chat_id": "4242", "user_id": "4242", "chat_type": "dm", "notifier_profile": "default", "delivery_mode": state["mode"]}])
        if state["flaky"]:
            state["flaky"] -= 1
            raise core.CommandError("hermes kanban --board: exit 1\ndatabase is locked")
        if state["fail"].get(verb):
            state["fail"][verb] -= 1
            raise core.CommandError(f"hermes kanban --board: exit 1\n{verb} failed")
        if verb == "create":
            if state["on_create"]:
                state["on_create"], nested = None, state["on_create"]
                nested()
            key = argv[argv.index("--idempotency-key") + 1]
            card = state["keys"].setdefault(key, f"t_wait{len(state['keys']) + 1}")
            cards.setdefault(card, "ready")
            state["bodies"].setdefault(card, argv[argv.index("--body") + 1])
            return json.dumps({"id": card, "status": cards[card]})
        if verb == "notify-subscribe":
            state["mode"] = argv[argv.index("--delivery-mode") + 1]
            return ""
        if verb == "comment":
            assert argv[5] == "--", argv  # the text is the agent's: it may start with "--"
            state["comments"].setdefault(argv[6], []).append(argv[7])
            return ""
        assert verb in ("block", "archive", "complete"), argv  # never unblock: see the contract test
        card = argv[-2] if verb == "block" else argv[5]  # block ... [--] <card> <reason>
        allowed = {"block": ("ready",), "archive": ("ready", "blocked", "done"), "complete": ("ready", "blocked")}
        if cards[card] not in allowed[verb]:
            raise core.CommandError(f"hermes kanban --board: exit 1\ncannot {verb} {card}")
        if verb == "block":
            state["blocks"][card] = state["blocks"].get(card, 0) + 1
        cards[card] = {"block": "blocked", "archive": "archived", "complete": "done"}[verb]
        return ""
    monkeypatch.setattr(core, "run", fake_run)
    return state


def hook(monkeypatch, event, **payload):
    payload.setdefault("cwd", "/wt")
    payload.setdefault("message", "Claude is waiting for your input")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return events.hook(ns(event))


def ns(event, url=None):
    return argparse.Namespace(event=event, url=url, card=None)


def done(url):
    return events.hook(ns("done", url))


def verbs(state):
    return [c[4] for c in state["calls"] if c[:2] == ["hermes", "kanban"] and c[4] not in ("show", "notify-list")]


def test_a_wait_opens_a_subscribed_blocked_wait_card_and_leaves_the_ledger_alone(board, monkeypatch, capsys):
    assert hook(monkeypatch, "notification") == 0
    assert verbs(board) == ["create", "notify-subscribe", "block"]
    assert board["cards"] == {"t_abc123": "ready", "t_wait1": "blocked"}
    block = next(c for c in board["calls"] if c[4:5] == ["block"])
    assert block[5:9] == ["--kind", "needs_input", "--", "t_wait1"]
    assert block[9] == "Claude is waiting for your input\nReply in Herdr pane p_agent."
    create = next(c for c in board["calls"] if c[4:5] == ["create"])
    assert create[create.index("--idempotency-key") + 1].startswith("t_abc123:wait:")
    assert "ledger t_abc123" in create[create.index("--body") + 1]
    assert (board["git_dir"] / core.WAIT_KIND).read_text() == "t_wait1"
    assert capsys.readouterr().out == ""  # hook stdout can reach the agent's context


def test_a_long_question_is_kept_whole(board, monkeypatch):
    """The gateway shows the whole reason (human_notices); a cut here would end a question mid-sentence."""
    question = "The brief says no PR, but the tracker closes only on a PR. " * 8 + "Which do you want?"
    hook(monkeypatch, "notification", message=question)
    assert next(c for c in board["calls"] if c[4:5] == ["block"])[-1] == f"{question}\nReply in Herdr pane p_agent."


def test_an_empty_message_still_says_what_to_do(board, monkeypatch):
    hook(monkeypatch, "notification", message="")
    assert next(c for c in board["calls"] if c[4:5] == ["block"])[-1] == (
        "The agent is waiting for you.\nReply in Herdr pane p_agent.")


def test_a_question_that_starts_like_a_flag_is_passed_as_the_reason(board, monkeypatch):
    hook(monkeypatch, "notification", message="--kind=capability please")
    block = next(c for c in board["calls"] if c[4:5] == ["block"])
    assert block[-3:] == ["--", "t_wait1", "--kind=capability please\nReply in Herdr pane p_agent."]


def test_a_second_wait_while_one_is_open_makes_no_card(board, monkeypatch):
    hook(monkeypatch, "notification")
    hook(monkeypatch, "notification")
    assert verbs(board).count("create") == 1


def test_a_keypress_resume_archives_the_wait_via_posttooluse_and_a_later_wait_makes_a_second_card(board, monkeypatch):
    # A permission prompt approved by keypress fires no UserPromptSubmit; PostToolUse (also
    # wired to the "prompt" event) is what closes the wait when the agent resumes.
    hook(monkeypatch, "notification")
    assert hook(monkeypatch, "prompt") == 0  # what PostToolUse sends: same event as UserPromptSubmit
    assert board["cards"]["t_wait1"] == "archived"
    hook(monkeypatch, "notification")
    assert verbs(board).count("create") == 2
    assert board["cards"]["t_wait2"] == "blocked"


def test_the_human_typing_archives_the_wait_card_silently(board, monkeypatch, capsys):
    hook(monkeypatch, "notification")
    assert hook(monkeypatch, "prompt") == 0
    assert board["cards"]["t_wait1"] == "archived"
    assert not (board["git_dir"] / core.WAIT_KIND).exists()
    assert hook(monkeypatch, "prompt") == 0  # nothing open: nothing to do
    assert verbs(board).count("archive") == 1
    assert capsys.readouterr().out == ""


def test_a_long_session_blocks_every_card_at_most_once_and_never_unblocks(board, monkeypatch):
    for event in ("notification", "prompt", "notification", "prompt", "session-end", "session-end"):
        assert hook(monkeypatch, event) == 0
    assert done(PR) == 0
    assert board["cards"] == {"t_abc123": "done", "t_wait1": "archived", "t_wait2": "archived"}
    assert board["blocks"] == {"t_wait1": 1, "t_wait2": 1, "t_abc123": 1}


def test_clear_ends_a_session_but_does_not_block(board, monkeypatch):
    assert hook(monkeypatch, "session-end", reason="clear") == 0
    assert verbs(board) == []


def test_session_end_blocks_a_ready_ledger_only(board, monkeypatch):
    assert hook(monkeypatch, "session-end", reason="prompt_input_exit") == 0
    assert board["cards"]["t_abc123"] == "blocked"
    assert next(c for c in board["calls"] if c[4:5] == ["block"])[-1] == (
        "The agent's session ended before it opened a pull request.\nCheck Herdr pane p_agent.")
    board["cards"]["t_abc123"], board["calls"] = "done", []
    assert hook(monkeypatch, "session-end") == 0
    assert verbs(board) == []


def test_done_completes_with_one_readable_line_and_prints(board, monkeypatch, capsys):
    assert done(PR) == 0
    assert board["cards"]["t_abc123"] == "done"
    complete = next(c for c in board["calls"] if c[4:5] == ["complete"])
    # One line, the PR link, and no claim the work is merged or live. Issue, pane, worktree
    # stay on the card's body and its muster links comment.
    assert complete[complete.index("--summary") + 1] == f"Ready for review: {PR} (open; not merged or deployed)"
    assert "card t_abc123 -> done" in capsys.readouterr().out


def test_done_completes_a_blocked_ledger_directly_and_archives_an_open_wait(board, monkeypatch):
    hook(monkeypatch, "notification")
    board["cards"]["t_abc123"] = "blocked"
    assert done(PR) == 0
    assert board["cards"] == {"t_abc123": "done", "t_wait1": "archived"}


def test_done_on_a_ledger_in_triage_fails_loudly(board, monkeypatch, capsys):
    board["cards"]["t_abc123"] = "triage"
    assert done(PR) == 1
    assert "triage" in capsys.readouterr().err
    assert "done card t_abc123" in events.log_path().read_text()


def test_done_refuses_a_url_that_is_not_a_pull_request_of_this_repository(board, monkeypatch):
    assert done("https://github.com/someone/else/pull/1") == 2
    assert done("not a url") == 2
    assert done(None) == 2
    assert verbs(board) == []


def test_done_refuses_a_pull_request_from_another_branch(board, monkeypatch, capsys):
    board["head"] = "fix/other"
    assert done(PR) == 1
    assert f"done: {PR} is from fix/other, not muster/397" in capsys.readouterr().err
    assert board["cards"]["t_abc123"] == "ready" and verbs(board) == []
    assert "is from fix/other" in events.log_path().read_text()


def test_done_fails_when_github_cannot_name_the_head_branch(board, monkeypatch, capsys):
    board["head"] = None
    assert done(PR) == 1
    assert "HTTP 502" in capsys.readouterr().err and board["cards"]["t_abc123"] == "ready"


def test_done_checks_the_branch_the_links_file_names(board, monkeypatch):
    (board["git_dir"] / core.CARD_FILE).write_text(json.dumps({**LINKS, "branch": "work/397"}))
    board["head"] = "work/397"
    assert done(PR) == 0
    assert board["cards"]["t_abc123"] == "done"


def test_outside_a_muster_worktree_nothing_happens(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", lambda argv: calls.append(argv) or f"{tmp_path}\n")
    assert hook(monkeypatch, "notification") == 0
    assert all(c[0] == "git" for c in calls)


def test_done_outside_a_muster_worktree_errors_instead_of_silently_succeeding(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(core, "run", lambda argv: calls.append(argv) or f"{tmp_path}\n")
    monkeypatch.chdir(tmp_path)
    assert done(PR) == 1
    err = capsys.readouterr().err
    assert "not inside a muster worktree" in err and str(tmp_path) in err
    assert "not inside a muster worktree" in events.log_path().read_text()


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "5"])
def test_non_object_hook_stdin_is_treated_as_empty(board, monkeypatch, raw):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert events.hook(ns("notification")) == 0
    assert board["cards"]["t_wait1"] == "blocked"


def test_a_transient_failure_is_retried_once_onto_the_same_wait_card(board, monkeypatch):
    board["flaky"] = 1
    assert hook(monkeypatch, "notification") == 0
    assert board["cards"]["t_wait1"] == "blocked" and len(board["keys"]) == 1
    assert not events.log_path().exists()


def test_a_hook_that_keeps_failing_is_logged_and_never_fails_the_agent(board, monkeypatch):
    board["flaky"] = 5
    assert hook(monkeypatch, "notification") == 0
    assert "notification card t_abc123" in events.log_path().read_text()
    assert not (board["git_dir"] / core.WAIT_KIND).exists()


def test_a_type_error_is_logged_like_any_other_failure(board, monkeypatch, capsys):
    def broken(*args):
        raise TypeError("'NoneType' object is not subscriptable")
    monkeypatch.setattr(events, "move", broken)
    assert hook(monkeypatch, "notification") == 0
    assert done(PR) == 1
    assert events.log_path().read_text().count("not subscriptable") == 2
    assert "not subscriptable" in capsys.readouterr().err


def test_every_muster_log_line_starts_with_its_time(board, monkeypatch):
    board["flaky"] = 5
    hook(monkeypatch, "notification")
    assert __import__("re").match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d notification card", events.log_path().read_text())


def test_hook_events_point_hermes_at_the_configured_home(board, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_HOME")
    hook(monkeypatch, "prompt")
    assert os.environ["HERMES_HOME"] and "HERMES_KANBAN_HOME" not in os.environ


def test_an_unreadable_links_file_is_logged_and_never_fails_the_agent(board, monkeypatch):
    (board["git_dir"] / core.CARD_FILE).write_text("{not valid json")
    assert hook(monkeypatch, "notification") == 0
    assert events.log_path().read_text()
    assert [c for c in board["calls"] if c[0] != "git"] == []


def test_done_with_an_unreadable_links_file_exits_1_and_logs(board, monkeypatch, capsys):
    (board["git_dir"] / core.CARD_FILE).write_text("{not valid json")
    assert done(PR) == 1
    assert events.log_path().read_text()
    assert capsys.readouterr().err


def test_a_wait_card_whose_subscribe_failed_is_recorded_and_finished_by_the_next_wait(board, monkeypatch):
    board["fail"]["notify-subscribe"] = 2  # both attempts of this hook
    assert hook(monkeypatch, "notification") == 0
    assert (board["git_dir"] / core.WAIT_KIND).read_text() == "t_wait1"  # recorded, not leaked
    assert board["cards"]["t_wait1"] == "ready"
    assert hook(monkeypatch, "notification") == 0
    assert verbs(board).count("create") == 1
    assert board["cards"]["t_wait1"] == "blocked"


def test_a_wait_card_whose_block_failed_is_archived_when_the_agent_resumes(board, monkeypatch):
    board["fail"]["block"] = 2
    assert hook(monkeypatch, "notification") == 0
    assert board["cards"]["t_wait1"] == "ready"
    assert hook(monkeypatch, "prompt") == 0
    assert board["cards"]["t_wait1"] == "archived"
    assert not (board["git_dir"] / core.WAIT_KIND).exists()


def test_two_racing_notifications_open_one_wait_card(board, monkeypatch):
    # The second hook runs while the first is inside `create`: the marker must already be claimed.
    board["on_create"] = lambda: hook(monkeypatch, "notification")
    assert hook(monkeypatch, "notification") == 0
    assert verbs(board).count("create") == 1
    assert board["cards"]["t_wait1"] == "blocked"
    assert (board["git_dir"] / core.WAIT_KIND).read_text() == "t_wait1"


def test_an_empty_marker_left_by_a_killed_hook_is_cleared_when_the_agent_resumes(board, monkeypatch):
    (board["git_dir"] / core.WAIT_KIND).write_text("")
    assert hook(monkeypatch, "notification") == 0
    assert verbs(board).count("create") == 0  # looks like another hook mid-create
    assert hook(monkeypatch, "prompt") == 0
    assert hook(monkeypatch, "notification") == 0
    assert board["cards"]["t_wait1"] == "blocked"


def test_a_question_after_done_still_pages(board, monkeypatch):
    """The pull request is still in review after done; a reviewer's follow-up makes the agent ask again."""
    board["cards"]["t_abc123"] = "done"
    assert hook(monkeypatch, "notification") == 0
    assert verbs(board) == ["create", "notify-subscribe", "block"]
    assert board["cards"] == {"t_abc123": "done", "t_wait1": "blocked"}


def test_no_wait_card_once_the_ledger_is_archived(board, monkeypatch):
    board["cards"]["t_abc123"] = "archived"
    assert hook(monkeypatch, "notification") == 0
    assert verbs(board) == []
    assert not (board["git_dir"] / core.WAIT_KIND).exists()


def test_a_wait_opens_while_the_ledger_is_blocked(board, monkeypatch):
    board["cards"]["t_abc123"] = "blocked"
    assert hook(monkeypatch, "notification") == 0
    assert board["cards"]["t_wait1"] == "blocked"


def test_an_askuserquestion_wait_names_the_question(board, monkeypatch):
    hook(monkeypatch, "notification", message=None, hook_event_name="PreToolUse", tool_name="AskUserQuestion",
         tool_input={"questions": [{"question": "Which repo?"}]})
    block = next(c for c in board["calls"] if c[4:5] == ["block"])
    assert block[-1] == "Which repo?\nReply in Herdr pane p_agent."


def test_a_rejected_question_closes_its_wait_so_the_corrected_one_pages(board, monkeypatch):
    # PostToolUseFailure is wired to "prompt" (claude.hook_settings): a rejected AskUserQuestion
    # fires no PostToolUse, so without it the retry would find the first card open and page nothing.
    hook(monkeypatch, "notification", message=None, tool_input={"questions": [{"question": "Bad?"}]})
    assert hook(monkeypatch, "prompt") == 0
    hook(monkeypatch, "notification", message=None, tool_input={"questions": [{"question": "Which repo?"}]})
    assert board["cards"]["t_wait1"] == "archived" and board["cards"]["t_wait2"] == "blocked"
    assert [c[-1] for c in board["calls"] if c[4:5] == ["block"]][-1] == "Which repo?\nReply in Herdr pane p_agent."


def test_a_full_session_pages_once_per_real_wait(board, monkeypatch):
    question = dict(message=None, hook_event_name="PreToolUse", tool_name="AskUserQuestion",
                     tool_input={"questions": [{"question": "Which repo?"}]})
    hook(monkeypatch, "notification", **question)
    hook(monkeypatch, "notification", **question)  # duplicate
    hook(monkeypatch, "prompt")  # PostToolUse after the answer
    hook(monkeypatch, "notification", message="Claude needs your permission to use Bash")
    hook(monkeypatch, "prompt")  # keypress resume
    assert done(PR) == 0
    hook(monkeypatch, "notification", message="Claude needs your permission to use Bash")  # in review
    hook(monkeypatch, "notification", **question)  # while that wait is open
    assert verbs(board).count("create") == 3
    assert board["blocks"] == {"t_wait1": 1, "t_wait2": 1, "t_wait3": 1}
    assert board["cards"] == {"t_abc123": "done", "t_wait1": "archived", "t_wait2": "archived", "t_wait3": "blocked"}


def test_a_run_link_without_an_issue_names_its_branch(board):
    """the run hooks reuse open_wait for an ad-hoc run, which has a branch and no issue."""
    run = {k: v for k, v in LINKS.items() if k != "issue"} | {"branch": "fix/x", "provenance": "Made by test"}
    events.open_wait(board["git_dir"], run, "why", "t_abc123:wait:1")
    create = next(c for c in board["calls"] if c[4:5] == ["create"])
    body = create[create.index("--body") + 1]
    assert "| branch fix/x |" in body and body.endswith("\nMade by test") and "issues/" not in body
    assert create[-1] == "acme/app fix/x waiting for you"


def test_a_wait_card_takes_the_human_title_from_its_links(board):
    """The gateway's ping leads with this title; links files from before "title" fall back (tests above)."""
    events.open_wait(board["git_dir"], LINKS | {"title": "Series research"}, "Which repo?", "t_abc123:wait:1")
    assert next(c for c in board["calls"] if c[4:5] == ["create"])[-1] == "Series research"


def test_an_intake_wait_card_keeps_its_issue_title_and_body(board, monkeypatch):
    hook(monkeypatch, "notification")
    create = next(c for c in board["calls"] if c[4:5] == ["create"])
    assert create[-1] == "acme/app#397 waiting for you"
    assert create[create.index("--body") + 1] == (
        "Waiting for you. ledger t_abc123 | issue https://github.com/acme/app/issues/397"
        f" | pane p_agent | worktree /wt\n{core.PROVENANCE}")


def test_a_submitted_prompt_leaves_its_sha256_for_the_launcher_and_nothing_else(board, monkeypatch, tmp_path):
    launch_dir = tmp_path / "intake" / "t_abc123"
    (board["git_dir"] / core.CARD_FILE).write_text(json.dumps({**LINKS, "launch_dir": str(launch_dir)}))
    hook(monkeypatch, "prompt", prompt="Execute the authorized task ...", session_id="s1")
    [seen] = [json.loads(line) for line in (launch_dir / "prompt-seen.jsonl").read_text().splitlines()]
    assert seen["sha256"] == hashlib.sha256(b"Execute the authorized task ...").hexdigest()
    assert seen["session"] == "s1" and "prompt" not in seen  # a hash only: the text stays in the record
    hook(monkeypatch, "prompt", tool_name="Bash")  # a PostToolUse carries no prompt: no evidence line
    assert len((launch_dir / "prompt-seen.jsonl").read_text().splitlines()) == 1
    assert board["cards"] == {"t_abc123": "ready"}


def test_a_stop_without_a_card_does_nothing_at_all(board, monkeypatch):
    # Claude fires Stop at every turn end; an issue run has nothing to do with it.
    assert hook(monkeypatch, "stop") == 0
    assert board["calls"] == []


def test_core_and_events_agree_on_card_file(board, tmp_path):
    """The marker core writes at launch is the one events reads."""
    record = {"card": "t_abc123", "repo": "acme/app", "issue": 397, "title": "T"}
    rec = {"pane": "p_agent", "workspace": "w_1", "path": "/wt", "base": "main", "branch": "muster/397"}
    (board["git_dir"] / core.CARD_FILE).write_text(json.dumps(core.links(record, rec)))
    git_dir, link = events.context("/wt")
    assert git_dir == board["git_dir"] and link["card"] == "t_abc123" and link["branch"] == "muster/397"


def test_hook_with_card_delegates_to_runs(monkeypatch):
    seen = []
    monkeypatch.setattr(runs, "hook", lambda args: seen.append(args.card) or 0)
    assert events.hook(argparse.Namespace(event="stop", url=None, card="t_run1")) == 0
    assert seen == ["t_run1"]


def test_a_stale_empty_wait_marker_is_replaced_and_a_fresh_one_is_left(board, monkeypatch):
    marker = board["git_dir"] / core.WAIT_KIND
    marker.write_text("")
    assert hook(monkeypatch, "notification") == 0
    assert verbs(board) == []  # fresh: another hook is mid-create
    old = time.time() - 121
    os.utime(marker, (old, old))
    assert hook(monkeypatch, "notification") == 0
    assert board["cards"]["t_wait1"] == "blocked" and marker.read_text() == "t_wait1"


def test_done_compares_the_repository_case_insensitively(board, monkeypatch):
    assert done(f"https://github.com/{LINKS['repo'].upper()}/pull/1") == 0


def test_an_issue_run_session_end_that_keeps_failing_is_queued_and_the_flush_blocks_the_card(board, monkeypatch):
    board["fail"]["block"] = 3
    assert hook(monkeypatch, "session-end") == 0
    assert board["cards"]["t_abc123"] == "ready"
    [queued] = runs.pending("t_abc123")
    assert json.loads(queued.read_text())["event"] == "session-end"
    assert runs.flush() == 0
    assert board["cards"]["t_abc123"] == "blocked" and board["blocks"]["t_abc123"] == 1
    assert runs.pending("t_abc123") == []


def test_a_hook_that_lands_queues_nothing(board, monkeypatch):
    assert hook(monkeypatch, "session-end") == 0
    assert board["cards"]["t_abc123"] == "blocked"
    assert runs.pending("t_abc123") == []


def test_an_issue_run_event_is_saved_before_any_kanban_move(board, monkeypatch):
    """A hook killed mid-move must leave its event for the flush."""
    seen = []
    real = core.run
    monkeypatch.setattr(core, "run", lambda argv: (seen.append(len(runs.pending("t_abc123"))) if argv[4:5] == ["block"]
                                                    else None) or real(argv))
    assert hook(monkeypatch, "session-end") == 0
    assert seen == [1]
    assert runs.pending("t_abc123") == []


def test_done_that_cannot_land_is_queued_exits_1_and_the_flush_completes_it(board, monkeypatch, capsys):
    board["fail"]["complete"] = 2
    assert done(PR) == 1
    assert "queued for the flush" in capsys.readouterr().err
    assert board["cards"]["t_abc123"] == "ready"
    runs.flush()
    assert board["cards"]["t_abc123"] == "done" and runs.pending("t_abc123") == []


def test_a_queued_event_of_an_archived_ledger_is_dropped(board, monkeypatch):
    board["fail"]["block"] = 2
    hook(monkeypatch, "session-end")
    board["cards"]["t_abc123"] = "archived"
    runs.flush()
    assert runs.pending("t_abc123") == [] and board["blocks"] == {}


def test_a_queued_wait_is_delivered_before_the_prompt_that_closes_it(board, monkeypatch):
    board["fail"]["block"] = 2
    hook(monkeypatch, "notification")
    hook(monkeypatch, "prompt")  # drains the wait first, then closes it
    assert board["cards"]["t_wait1"] == "archived" and runs.pending("t_abc123") == []


def test_a_tool_use_with_nothing_open_writes_nothing(board, monkeypatch):
    assert hook(monkeypatch, "prompt") == 0
    assert not runs.run_dir("t_abc123").exists()


def test_tool_uses_behind_a_stuck_queue_add_one_prompt_at_most(board, monkeypatch, capsys):
    board["cards"]["t_abc123"] = "triage"
    assert done(PR) == 1  # the approved wedge: done on a triage ledger waits for a person
    hook(monkeypatch, "notification")
    for _ in range(5):
        hook(monkeypatch, "prompt")
    assert [p.name.split("-", 1)[1] for p in runs.pending("t_abc123")] == ["done.json", "notification.json", "prompt.json"]


def test_done_behind_a_failing_event_names_that_failure(board, monkeypatch, capsys):
    board["fail"]["block"] = 4
    hook(monkeypatch, "session-end")
    assert done(PR) == 1
    assert "block failed (queued for the flush)" in capsys.readouterr().err


QUESTION = {"question": "Approve this design, as described above?", "header": "Approval", "multiSelect": False,
            "options": [{"label": "Approve (Recommended)", "description": "Build it as proposed."},
                        {"label": "Change something", "description": "Say what to change.", "preview": "a\nb"}]}


def propose(tmp_path, text, name="design.md"):
    path = tmp_path / name
    path.write_text(text)
    return events.hook(ns("propose", str(path)))


def ask(monkeypatch, *questions):
    return hook(monkeypatch, "notification", message="", tool_name="AskUserQuestion",
                tool_input={"questions": list(questions or [QUESTION])})


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def test_a_proposal_is_one_ledger_comment_and_the_next_ask_carries_it_and_every_option(board, monkeypatch, tmp_path, capsys):
    text = "## Approach\nA propose hook.\n## Non-goals\nNo auto-approval."
    assert propose(tmp_path, text) == 0
    head = f"Proposal v1 {sha(text)}"
    assert capsys.readouterr().out == f"proposal: {head} on ledger t_abc123\n"
    assert board["comments"]["t_abc123"] == [f"{head}\n\n{text}"]
    assert ask(monkeypatch) == 0
    shown = json.loads(core.kanban("show", "t_wait1", "--json"))["task"]["body"]
    assert "ledger t_abc123 | issue https://github.com/acme/app/issues/397 | pane p_agent" in shown  # provenance kept
    assert f"# {head} (also on ledger t_abc123" in shown and shown.endswith(text)
    assert "## Approve this design, as described above?" in shown
    assert "- Approve (Recommended): Build it as proposed." in shown and "\n    a\n    b" in shown
    reason = next(c for c in board["calls"] if c[4:5] == ["block"])[-1]
    assert reason == (f"Approve this design, as described above?\n{head}: full text on this card and ledger t_abc123.\n"
                      "Reply in Herdr pane p_agent.")  # the ping stays short: the text is on the cards


def test_a_revision_is_a_new_version_the_old_one_stays_and_the_next_ask_names_the_new_one(board, monkeypatch, tmp_path):
    first, second = "plan one", "plan two"
    propose(tmp_path, first)
    ask(monkeypatch)
    hook(monkeypatch, "prompt")
    assert propose(tmp_path, first) == 0  # unchanged: still v1, and no second comment
    assert propose(tmp_path, second) == 0
    assert [c.split("\n")[0] for c in board["comments"]["t_abc123"]] == [
        f"Proposal v1 {sha(first)}", f"Proposal v2 {sha(second)}"]
    ask(monkeypatch)
    body = board["bodies"]["t_wait2"]
    assert f"Proposal v2 {sha(second)}" in body and "Proposal v1" not in body and body.endswith(second)
    assert propose(tmp_path, first) == 0  # a revert is a new revision to review, not v1 again
    assert board["comments"]["t_abc123"][-1] == f"Proposal v3 {sha(first)}\n\n{first}"
    assert sorted(p.name for p in events.proposals("t_abc123").glob("v*.md")) == ["v1.md", "v2.md", "v3.md"]


def test_a_redelivered_proposal_posts_no_second_comment(board, tmp_path):
    propose(tmp_path, "the plan")
    entry = {"version": 1, "sha": sha("the plan")}
    runs.enqueue("t_abc123", "proposal", "Proposal v1", proposal=entry, git_dir=str(board["git_dir"]), link=LINKS)
    runs.drain("t_abc123")  # a hook killed after the comment, before its entry was removed
    assert len(board["comments"]["t_abc123"]) == 1 and not runs.pending("t_abc123")


@pytest.mark.parametrize("content", [None, "", " \n", "x" * (events.PROPOSAL_MAX + 1)])
def test_propose_refuses_a_missing_empty_or_oversized_file_before_saving_anything(board, tmp_path, capsys, content):
    path = tmp_path / "design.md"
    if content is not None:
        path.write_text(content)
    assert events.hook(ns("propose", str(path))) == 1
    assert capsys.readouterr().err.startswith("propose card t_abc123: ")
    assert verbs(board) == [] and not events.proposals("t_abc123").exists()


def test_propose_redacts_a_github_token(board, tmp_path):
    propose(tmp_path, "use ghp_abcdefghijklmnop to push")
    assert board["comments"]["t_abc123"][0].endswith("use [redacted] to push")


def test_propose_on_an_archived_ledger_says_it_was_not_posted(board, tmp_path, capsys):
    board["cards"]["t_abc123"] = "archived"
    assert propose(tmp_path, "the plan") == 1
    assert "archived: not posted" in capsys.readouterr().err and "t_abc123" not in board["comments"]


def test_a_wait_reopened_past_a_stale_claim_still_carries_the_question_and_proposal(board, monkeypatch, tmp_path):
    """The retry for a hook killed at its timeout: the pin and the questions must survive it."""
    propose(tmp_path, "the plan")
    marker = board["git_dir"] / core.WAIT_KIND
    marker.write_text("")
    os.utime(marker, (1, 1))
    ask(monkeypatch)
    body = board["bodies"]["t_wait1"]
    assert "Proposal v1" in body and "- Approve (Recommended)" in body and body.endswith("the plan")


def test_a_long_preview_is_cut_and_says_so(board, monkeypatch):
    big = dict(QUESTION, header="Pick", options=[{"label": "A", "description": "d", "preview": "p" * (events.PREVIEW_MAX + 50)}])
    ask(monkeypatch, big)
    assert "p" * (events.PREVIEW_MAX + 1) not in board["bodies"]["t_wait1"]
    assert "[preview cut at 2 KB; the whole of it is in the pane]" in board["bodies"]["t_wait1"]


def test_propose_without_a_file_prints_its_usage(board, capsys):
    assert events.hook(ns("propose")) == 1
    assert "usage: hermes muster hook propose <file>" in capsys.readouterr().err


def denied(capsys):
    """The PreToolUse decision a denied approval request prints, or None when the ask was let through."""
    out = capsys.readouterr().out
    if not out:
        return None
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse" and decision["permissionDecision"] == "deny"
    return decision["permissionDecisionReason"]


def test_an_approval_request_without_a_saved_proposal_is_denied_before_its_dialog(board, monkeypatch, capsys):
    assert ask(monkeypatch) == 0
    why = denied(capsys)
    assert why.startswith("No proposal is saved on the ledger card") and "muster hook propose <file>" in why
    assert "t_wait1" not in board["cards"] and not runs.pending("t_abc123")  # nothing queued, nothing pinged


def test_only_a_question_headed_approval_takes_the_proposal_and_it_is_used_once(board, monkeypatch, tmp_path, capsys):
    propose(tmp_path, "the plan")
    ask(monkeypatch, dict(QUESTION, header="Path", question="Which file?"))
    hook(monkeypatch, "prompt")
    hook(monkeypatch, "notification", message="Claude needs your permission to use Bash")
    hook(monkeypatch, "prompt")
    capsys.readouterr()
    ask(monkeypatch)  # the approval request: the question and the permission prompt left it armed
    assert denied(capsys) is None
    hook(monkeypatch, "prompt")
    assert ["Proposal" in board["bodies"][f"t_wait{n}"] for n in (1, 2, 3)] == [False, False, True]
    assert ["Proposal" in c[-1] for c in board["calls"] if c[4:5] == ["block"]] == [False, False, True]
    ask(monkeypatch)  # carried once: approving again needs a proposal saved again
    assert denied(capsys).startswith("No proposal is saved") and "t_wait4" not in board["cards"]


def test_an_approval_request_while_another_wait_is_open_is_denied_and_its_proposal_kept(board, monkeypatch, tmp_path, capsys):
    hook(monkeypatch, "notification", message="Claude needs your permission to use Bash")  # t_wait1, unanswered
    propose(tmp_path, "the plan")
    capsys.readouterr()
    ask(monkeypatch)
    why = denied(capsys)
    assert why.startswith("Wait card t_wait1 for an earlier question is still open")
    assert f"Proposal v1 {sha('the plan')} stays saved" in why
    assert "t_wait2" not in board["cards"] and events.proposals("t_abc123").joinpath("armed").is_file()
    hook(monkeypatch, "prompt")  # the human answers the open wait
    ask(monkeypatch)
    assert denied(capsys) is None and board["bodies"]["t_wait2"].endswith("the plan")
    assert not events.proposals("t_abc123").joinpath("armed").exists()  # consumed: a card carries it


def test_a_pinned_request_that_finds_a_wait_open_at_delivery_keeps_its_proposal_armed(board, tmp_path):
    """Past the gate, a wait opened in between: open_wait does not open a second card, and the pin is not lost."""
    propose(tmp_path, "the plan")
    pin = {"version": 1, "sha": sha("the plan")}
    events.open_wait(board["git_dir"], LINKS, "Permission?", "k1")  # t_wait1 blocked
    line = events.open_wait(board["git_dir"], LINKS, "Approve?", "k2", [QUESTION], pin)
    assert line == "notification: wait card t_wait1 already open"
    assert json.loads(events.proposals("t_abc123").joinpath("armed").read_text()) == pin


def test_a_proposal_not_yet_on_the_ledger_is_not_armed_and_the_flush_posts_it_once(board, monkeypatch, tmp_path, capsys):
    board["fail"]["comment"] = 2
    assert propose(tmp_path, "the plan") == 1
    assert "comment failed (queued for the flush)" in capsys.readouterr().err
    board["fail"]["comment"] = 2  # the gate's drain tries twice more
    ask(monkeypatch)
    assert denied(capsys).startswith("No proposal is saved") and "t_wait1" not in board["cards"]
    runs.flush()
    assert len(board["comments"]["t_abc123"]) == 1
    assert propose(tmp_path, "the plan") == 0  # finds its comment, posts nothing, arms it
    capsys.readouterr()
    ask(monkeypatch)
    assert denied(capsys) is None and len(board["comments"]["t_abc123"]) == 1
    assert board["cards"]["t_wait1"] == "blocked" and "Proposal v1" in board["bodies"]["t_wait1"]


def test_the_answered_approval_requests_posttooluse_closes_its_wait_and_prints_nothing(board, monkeypatch, tmp_path, capsys):
    """PostToolUse carries the ask's own payload as a `prompt`: the gate must not take it for a new request."""
    propose(tmp_path, "the plan")
    ask(monkeypatch)
    capsys.readouterr()
    hook(monkeypatch, "prompt", message="", tool_name="AskUserQuestion", tool_input={"questions": [QUESTION]})
    assert capsys.readouterr().out == "" and board["cards"]["t_wait1"] == "archived"


def test_an_approval_request_behind_a_question_not_yet_on_the_board_is_denied(board, monkeypatch, tmp_path, capsys):
    propose(tmp_path, "the plan")
    board["fail"]["create"] = 99  # the board is down for wait cards
    hook(monkeypatch, "notification", message="Which file?")
    capsys.readouterr()
    ask(monkeypatch)
    assert denied(capsys).startswith("An earlier question is not on the board yet")
    assert [p.name.split("-", 1)[1] for p in runs.pending("t_abc123")] == ["notification.json"]
    assert events.proposals("t_abc123").joinpath("armed").is_file()


def test_an_approval_request_whose_outbox_save_fails_is_denied_and_keeps_its_proposal(board, monkeypatch, tmp_path, capsys):
    """The gate passed, but the durable entry was not saved: no card would track the dialog, so deny it."""
    propose(tmp_path, "the plan")
    capsys.readouterr()
    real = runs.enqueue

    def full_disk(card, event, *a, **k):
        if event == "notification":
            raise OSError(28, "No space left on device")
        return real(card, event, *a, **k)
    monkeypatch.setattr(runs, "enqueue", full_disk)
    assert ask(monkeypatch) == 0
    why = denied(capsys)  # a deny decision, not a silent 0 that lets the dialog show
    assert why.startswith("muster could not save this approval request ([Errno 28] No space left on device)")
    assert f"Proposal v1 {sha('the plan')} stays saved" in why
    assert "t_wait1" not in board["cards"] and not runs.pending("t_abc123")
    assert json.loads(events.proposals("t_abc123").joinpath("armed").read_text()) == {"version": 1, "sha": sha("the plan")}
    monkeypatch.setattr(runs, "enqueue", real)
    ask(monkeypatch)  # the retry
    assert denied(capsys) is None and board["bodies"]["t_wait1"].endswith("the plan")


def test_a_plain_question_whose_outbox_save_fails_is_still_never_denied(board, monkeypatch, capsys):
    monkeypatch.setattr(runs, "enqueue", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert ask(monkeypatch, dict(QUESTION, header="Path")) == 0
    assert capsys.readouterr().out == ""  # only an approval request is gated; a hook never blocks other asks


# -- the PermissionRequest bridge -------------------------------------------------------------------

ASK = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": "Which?", "options": []}]}}


def test_permission_goes_to_the_bridge_before_any_board_call(board, monkeypatch):
    import muster.bridge as bridge
    seen = []
    monkeypatch.setattr(bridge, "wait", lambda directory, link, payload: seen.append((directory, link, payload)) or 0)
    assert hook(monkeypatch, "permission", hook_event_name="PermissionRequest", **ASK) == 0
    assert seen[0][0] == board["git_dir"] and seen[0][1] == LINKS and seen[0][2]["tool_name"] == "AskUserQuestion"
    assert verbs(board) == []


def test_permission_outside_a_muster_worktree_prints_nothing(board, monkeypatch, capsys):
    monkeypatch.setattr(core, "run", lambda argv: (_ for _ in ()).throw(core.CommandError("not a repo")))
    assert hook(monkeypatch, "permission", **ASK) == 0
    assert capsys.readouterr().out == ""


def test_prompt_settles_before_its_early_return_and_session_end_stales(board, monkeypatch):
    import muster.bridge as bridge
    calls = []
    monkeypatch.setattr(bridge, "settle", lambda directory, payload: calls.append(("settle", directory, payload["x"])))
    monkeypatch.setattr(bridge, "session_end", lambda directory: calls.append(("end", directory)))
    hook(monkeypatch, "prompt", x=1)  # nothing open: the early return
    hook(monkeypatch, "session-end")
    assert calls == [("settle", board["git_dir"], 1), ("end", board["git_dir"])]


def test_an_ask_leaves_its_pin_for_the_bridge_and_another_notification_does_not(board, monkeypatch):
    git_dir = board["git_dir"]
    core.save_json(events.proposals("t_abc123") / "armed", {"version": 1, "sha": "aaa"})
    hook(monkeypatch, "notification", **ASK)  # not an approval: no pin
    assert not (git_dir / core.PIN_FILE).exists()
    core.save_json(git_dir / core.PIN_FILE, {"version": 9, "sha": "old"})
    hook(monkeypatch, "notification", message="x")  # not an ask: the file is left alone
    assert json.loads((git_dir / core.PIN_FILE).read_text())["version"] == 9
    hook(monkeypatch, "notification", **ASK)  # an ask that is no approval request pins nothing: the old pin goes
    assert not (git_dir / core.PIN_FILE).exists()


def test_an_approval_ask_writes_the_armed_pin(board, monkeypatch):
    core.save_json(events.proposals("t_abc123") / "armed", {"version": 3, "sha": "bbb"})
    approval = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": "Ok?", "header": "Approval", "options": []}]}}
    hook(monkeypatch, "notification", **approval)
    assert json.loads((board["git_dir"] / core.PIN_FILE).read_text()) == {"version": 3, "sha": "bbb"}


# --- wait mode: wake only when muster itself can deliver the page (#17 task 4) ---

def bridge_ready(board, link=LINKS, gateway_age=0, hook_key="PermissionRequest"):
    """Everything wake mode needs: the pane's settings carry the hook, the gateway touched its heartbeat."""
    if "issue" in link:
        settings = core.intake_dir() / link["card"] / core.SETTINGS_FILE
    else:
        settings = board["git_dir"] / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {hook_key: []}}))
    beat = decisions.root() / ".gateway"
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.touch()
    os.utime(beat, (time.time() - gateway_age,) * 2)


def wait_mode(board):
    """The wait card's subscription: "none" when muster pages the human itself."""
    return board["mode"] if any(c[4] == "notify-subscribe" for c in board["calls"] if len(c) > 4) else "none"


@pytest.mark.parametrize("ad_hoc", [False, True])
def test_a_bridged_wait_with_everything_in_place_takes_no_subscription(board, ad_hoc):
    link = {k: v for k, v in LINKS.items() if k != "issue"} | {"branch": "fix/x"} if ad_hoc else LINKS
    bridge_ready(board, link)
    events.open_wait(board["git_dir"], link, "why", "k1", bridged=True)
    assert wait_mode(board) == "none"


def test_an_ask_is_paged_by_muster_without_the_bridged_flag(board):
    bridge_ready(board)
    events.open_wait(board["git_dir"], LINKS, "why", "k1", [QUESTION])
    assert wait_mode(board) == "none"


@pytest.mark.parametrize("spoil", ["not bridged", "platform", "no hook", "no settings", "stale gateway", "no gateway"])
def test_each_missing_condition_keeps_notify_wake(board, monkeypatch, spoil):
    bridge_ready(board, gateway_age=60 if spoil == "stale gateway" else 0,
                 hook_key="Notification" if spoil == "no hook" else "PermissionRequest")
    if spoil == "platform":
        monkeypatch.setitem(config.settings, "notify_platform", "slack")
    if spoil == "no settings":
        (core.intake_dir() / LINKS["card"] / core.SETTINGS_FILE).unlink()
    if spoil == "no gateway":
        (decisions.root() / ".gateway").unlink()
    events.open_wait(board["git_dir"], LINKS, "why", "k1", bridged=spoil != "not bridged")
    assert wait_mode(board) == "notify+wake"


def test_a_wait_without_a_bridged_question_stays_notify_wake_with_everything_in_place(board):
    """idle, reconcile and every other wait call open_wait without bridged."""
    bridge_ready(board)
    events.open_wait(board["git_dir"], LINKS, "idle", "k1")
    assert wait_mode(board) == "notify+wake"


def test_a_permission_prompt_hook_reaches_open_wait_as_bridged(board, monkeypatch):
    bridge_ready(board)
    assert hook(monkeypatch, "notification", notification_type="permission_prompt") == 0
    assert wait_mode(board) == "none"


def test_only_a_question_or_a_permission_prompt_is_bridged():
    assert events.is_bridged("notification", {"notification_type": "elicitation_dialog"}) is False
    assert events.is_bridged("notification", {"tool_name": "AskUserQuestion"}) is True
    assert events.is_bridged("idle", {"tool_name": "AskUserQuestion"}) is False


# -- re-review after a send-back (#17 task 7) ------------------------------------------------------

REVIEWED, REVISED = "a" * 40, "b" * 40


def verified(monkeypatch, tmp_path, board, pr=None, why=None):
    """An issue run that is `done`, sent back once at REVIEWED; runs.verify answers from `answer`."""
    board["cards"]["t_abc123"] = "done"
    board["comments"]["t_abc123"] = [core.LINKS_PREFIX + json.dumps({
        "repo": "acme/app", "issue": 397, "branch": "muster/397", "base": "main", "worktree": str(tmp_path),
        "pane": "p_agent", "title": "Fix it"})]
    rid = decisions.create("feedback", "t_abc123", head=REVIEWED, cycle=1)["id"]
    decisions.transition(rid, ("open",), "done", outcome="Sent ✓")
    answer = {"pr": pr or {"url": PR, "headRefOid": REVISED}, "why": why, "calls": []}

    def verify(run):
        answer["calls"].append(run)
        return (None, answer["why"]) if answer["why"] else (answer["pr"], None)
    monkeypatch.setattr(runs, "verify", verify)
    return answer


def test_an_issue_run_done_after_a_send_back_makes_one_review_card(board, monkeypatch, tmp_path, capsys):
    answer = verified(monkeypatch, tmp_path, board)
    open_build = decisions.create("build", "t_abc123", head=REVIEWED)["id"]
    for _ in range(3):  # a repeated done
        assert done(PR) == 0
    runs.drain("t_abc123")
    assert board["cards"] == {"t_abc123": "done", "t_wait1": "done"}
    assert len(board["keys"]) == 1 and verbs(board).count("complete") == 1 and verbs(board).count("notify-subscribe") == 1
    create = next(c for c in board["calls"] if c[4:5] == ["create"])
    assert create[create.index("--idempotency-key") + 1] == f"review:t_abc123:{REVISED}"
    assert create[-1] == "Fix it: revised, ready for re-review"
    assert answer["calls"][0]["worktree"] == str(tmp_path) and answer["calls"][0]["branch"] == "muster/397"
    assert decisions.load(open_build)["status"] == "stale"
    assert board["comments"]["t_abc123"][0].startswith(core.LINKS_PREFIX) and len(board["comments"]["t_abc123"]) == 1
    assert capsys.readouterr().out.count("done: card t_abc123 -> done") == 3


def test_a_second_cycle_makes_a_second_card(board, monkeypatch, tmp_path):
    answer = verified(monkeypatch, tmp_path, board)
    assert done(PR) == 0
    rid = decisions.create("feedback", "t_abc123", head=REVISED, cycle=2)["id"]
    decisions.transition(rid, ("open",), "done", outcome="Sent ✓")
    answer["pr"] = {"url": PR, "headRefOid": "c" * 40}
    assert done(PR) == 0
    assert board["cards"] == {"t_abc123": "done", "t_wait1": "done", "t_wait2": "done"}


def test_a_replayed_done_entry_makes_no_second_card(board, monkeypatch, tmp_path):
    verified(monkeypatch, tmp_path, board)
    entry = {"event": "done", "detail": PR, "key": "k", "git_dir": str(board["git_dir"]), "link": LINKS}
    events.replay(entry)
    events.replay(entry)
    assert len(board["keys"]) == 1 and verbs(board).count("complete") == 1 and verbs(board).count("notify-subscribe") == 1


def test_done_without_a_send_back_neither_verifies_nor_makes_a_card(board, monkeypatch, tmp_path):
    answer = verified(monkeypatch, tmp_path, board)
    for req in decisions.for_ledger("t_abc123"):
        decisions.update(req["id"], outcome="Not sent")
    assert done(PR) == 0
    assert answer["calls"] == [] and verbs(board).count("create") == 0


def test_the_reviewed_head_again_tells_the_agent_there_are_no_new_commits(board, monkeypatch, tmp_path, capsys):
    verified(monkeypatch, tmp_path, board, pr={"url": PR, "headRefOid": REVIEWED})
    assert done(PR) == 1 and verbs(board).count("create") == 0
    assert "no new commits since the reviewed head" in capsys.readouterr().err


def test_a_revision_that_does_not_verify_fails_done_with_the_reason_and_leaves_the_queue_clear(
        board, monkeypatch, tmp_path, capsys):
    answer = verified(monkeypatch, tmp_path, board, why="some changes are not committed")
    assert done(PR) == 1
    err = capsys.readouterr().err
    assert "some changes are not committed" in err and "queued for the flush" not in err
    assert board["cards"] == {"t_abc123": "done"}
    # the flush after the report drops the entry instead of retrying it for ever
    runs.drain("t_abc123")
    assert runs.pending("t_abc123") == []
    # the agent fixes it and reruns done
    answer["why"] = None
    assert done(PR) == 0
    assert board["cards"] == {"t_abc123": "done", "t_wait1": "done"} and runs.pending("t_abc123") == []


def test_an_unreported_failure_does_not_hold_a_later_wait(board, monkeypatch, tmp_path):
    verified(monkeypatch, tmp_path, board, why="some changes are not committed")
    assert done(PR) == 1
    assert hook(monkeypatch, "notification") == 0  # drains the flagged entry first, then opens the wait
    assert board["cards"]["t_wait1"] == "blocked" and runs.pending("t_abc123") == []
