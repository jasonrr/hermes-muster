"""events: hooks in a muster pane open one wait card per wait and move the ledger at most once each way."""

import argparse
import hashlib
import io
import json
import os
import time

import pytest

import muster.core as core
import muster.events as events

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
             "fail": {}, "on_create": None}

    def fake_run(argv):
        state["calls"].append(argv)
        if argv[0] == "git":
            return f"{git_dir}\n"
        verb, cards = argv[4], state["cards"]
        if verb == "show":
            return json.dumps({"task": {"id": argv[5], "status": cards[argv[5]]}})
        if verb == "notify-list":
            return json.dumps([{"chat_id": "4242", "user_id": "4242", "chat_type": "dm", "notifier_profile": "default", "delivery_mode": "notify+wake"}])
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
            return json.dumps({"id": card, "status": cards[card]})
        if verb == "notify-subscribe":
            return ""
        assert verb in ("block", "archive", "complete"), argv  # never unblock: see the contract test
        card = argv[7] if verb == "block" else argv[5]
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
    assert block[5:8] == ["--kind", "needs_input", "t_wait1"]
    assert block[8] == "Claude is waiting for your input\nReply in Herdr pane p_agent."
    create = next(c for c in board["calls"] if c[4:5] == ["create"])
    assert create[create.index("--idempotency-key") + 1].startswith("t_abc123:wait:")
    assert "ledger t_abc123" in create[create.index("--body") + 1]
    assert (board["git_dir"] / core.WAIT_KIND).read_text() == "t_wait1"
    assert capsys.readouterr().out == ""  # hook stdout can reach the agent's context


def test_a_long_question_is_kept_whole(board, monkeypatch):
    """The gateway shows the whole reason (human_notices); a cut here would end a question mid-sentence."""
    question = "The brief says no PR, but the tracker closes only on a PR. " * 8 + "Which do you want?"
    hook(monkeypatch, "notification", message=question)
    assert next(c for c in board["calls"] if c[4:5] == ["block"])[8] == f"{question}\nReply in Herdr pane p_agent."


def test_an_empty_message_still_says_what_to_do(board, monkeypatch):
    hook(monkeypatch, "notification", message="")
    assert next(c for c in board["calls"] if c[4:5] == ["block"])[8] == (
        "The agent is waiting for you.\nReply in Herdr pane p_agent.")


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
    assert next(c for c in board["calls"] if c[4:5] == ["block"])[8] == (
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
    assert "ready -> done" in capsys.readouterr().out


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


@pytest.mark.parametrize("status", ["done", "archived"])
def test_no_wait_card_once_the_ledger_is_done_or_archived(board, monkeypatch, status):
    board["cards"]["t_abc123"] = status
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
    assert block[8] == "Which repo?\nReply in Herdr pane p_agent."


def test_a_rejected_question_closes_its_wait_so_the_corrected_one_pages(board, monkeypatch):
    # PostToolUseFailure is wired to "prompt" (claude.hook_settings): a rejected AskUserQuestion
    # fires no PostToolUse, so without it the retry would find the first card open and page nothing.
    hook(monkeypatch, "notification", message=None, tool_input={"questions": [{"question": "Bad?"}]})
    assert hook(monkeypatch, "prompt") == 0
    hook(monkeypatch, "notification", message=None, tool_input={"questions": [{"question": "Which repo?"}]})
    assert board["cards"]["t_wait1"] == "archived" and board["cards"]["t_wait2"] == "blocked"
    assert [c[8] for c in board["calls"] if c[4:5] == ["block"]][-1] == "Which repo?\nReply in Herdr pane p_agent."


def test_a_full_session_pages_once_per_real_wait(board, monkeypatch):
    question = dict(message=None, hook_event_name="PreToolUse", tool_name="AskUserQuestion",
                     tool_input={"questions": [{"question": "Which repo?"}]})
    hook(monkeypatch, "notification", **question)
    hook(monkeypatch, "notification", **question)  # duplicate
    hook(monkeypatch, "prompt")  # PostToolUse after the answer
    hook(monkeypatch, "notification", message="Claude needs your permission to use Bash")
    hook(monkeypatch, "prompt")  # keypress resume
    assert done(PR) == 0
    hook(monkeypatch, "notification", message="Claude needs your permission to use Bash")  # late permission
    hook(monkeypatch, "notification", **question)  # late question
    assert verbs(board).count("create") == 2
    assert board["blocks"] == {"t_wait1": 1, "t_wait2": 1}
    assert board["cards"] == {"t_abc123": "done", "t_wait1": "archived", "t_wait2": "archived"}


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
    rec = {"pane": "p_agent", "workspace": "w_1", "path": "/wt"}
    (board["git_dir"] / core.CARD_FILE).write_text(json.dumps(core.links(record, rec)))
    git_dir, link = events.context("/wt")
    assert git_dir == board["git_dir"] and link["card"] == "t_abc123"


def test_hook_with_card_delegates_to_runs(monkeypatch):
    import muster.runs as runs
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
