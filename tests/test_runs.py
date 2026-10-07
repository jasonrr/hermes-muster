"""runs: an ad-hoc run's hooks become verified, saved, acked kanban events; a turn end is not success."""

import argparse
import fcntl
import io
import json
import time

import pytest

import muster.claude as claude
import muster.config as config
import muster.core as core
import muster.runs as runs
from tests.fake_herdr import World

CARD = "t_run1"
RUN = {"card": CARD, "repo": "o/r", "clone": "/clone", "branch": "fix/x", "base": "main", "title": "T",
       "workspace": "w_1", "worktree": "/wt", "pane": "w_1:p2", "launched": True}
PR = {"url": "https://github.com/o/r/pull/7", "state": "OPEN", "headRefOid": "abc", "baseRefName": "main",
      "isDraft": False}


@pytest.fixture
def board(tmp_path, monkeypatch):
    """hermes, git, gh and herdr as the run sees them; hermes moves follow tests/test_kanban_contract.py."""
    state = {"cards": {}, "blocks": {}, "keys": {}, "calls": [], "fail": {}, "down": False, "events": {},
             "seq": 0, "cursor": 0, "head": "abc", "dirty": "", "prs": [], "gh_fail": 0, "agent": "working", "agent_seq": 1,
             "created_at": {}, "kinds": {}}
    monkeypatch.setattr(runs, "last_event", lambda card: state["events"].get(card, 0))
    monkeypatch.setattr(core, "block_kind", lambda card: state["kinds"].get(card))

    def fake_run(argv):
        state["calls"].append(argv)
        if argv[:1] == ["git"]:
            if argv[3:] == ["rev-parse", "HEAD"]:
                return state["head"] + "\n"
            if argv[3:5] == ["status", "--porcelain"]:
                assert "--untracked-files=no" in argv, argv
                return state["dirty"]
            raise AssertionError(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            assert argv[argv.index("--head") + 1] == "fix/x" and argv[argv.index("--state") + 1] == "all"
            assert "state" in argv[argv.index("--json") + 1].split(","), argv
            if state["gh_fail"]:
                state["gh_fail"] -= 1
                raise core.CommandError("gh pr list: exit 1\nHTTP 502")
            return json.dumps(state["prs"])
        if argv[:3] == ["herdr", "agent", "get"]:
            if state["agent"] is None:
                raise core.CommandError('herdr agent get: exit 1\n{"error":{"code":"agent_not_found"}}')
            if state["agent"] == "herdr-down":
                raise core.CommandError("herdr agent get: exit 1\nsocket not found")
            return json.dumps({"result": {"agent": {"agent": "claude", "agent_status": state["agent"],
                                                    "state_change_seq": state["agent_seq"]}}})
        assert argv[:4] == ["hermes", "kanban", "--board", "muster"], argv
        verb, cards = argv[4], state["cards"]
        if state["down"]:
            raise core.CommandError("hermes kanban --board: exit 1\ndatabase is locked")
        if state["fail"].get(verb):
            state["fail"][verb] -= 1
            raise core.CommandError(f"hermes kanban --board: exit 1\n{verb} failed")
        if verb == "show":
            return json.dumps({"task": {"id": argv[5], "status": cards[argv[5]]}})
        if verb == "notify-list":
            if cards[argv[5]] == "archived" or state.get("no_subs"):
                return "[]"  # the notifier drops a card's subscriptions on archive
            return json.dumps([{"chat_id": "4242", "user_id": "4242", "chat_type": "dm", "notifier_profile": "default", "delivery_mode": "notify+wake",
                                "last_event_id": state["cursor"], "last_ping_event_id": state["cursor"]}])
        if verb == "create":
            key = argv[argv.index("--idempotency-key") + 1]
            card = state["keys"].setdefault(key, f"t_wait{len(state['keys']) + 1}")
            state["created_at"].setdefault(card, int(time.time()))
            cards.setdefault(card, "ready")
            return json.dumps({"id": card, "status": cards[card], "created_at": state["created_at"][card]})
        if verb in ("notify-subscribe", "comment"):
            return ""
        # unblock: only a recover, once, of a launch-failure block (see the contract test)
        assert verb in ("block", "archive", "complete", "unblock"), argv
        card = argv[7] if verb == "block" else argv[5]
        allowed = {"block": ("ready",), "archive": ("ready", "blocked", "done"), "complete": ("ready", "blocked"),
                   "unblock": ("blocked",)}
        if cards[card] not in allowed[verb]:
            raise core.CommandError(f"hermes kanban --board: exit 1\ncannot {verb} {card}")
        if verb == "block":
            state["blocks"][card] = state["blocks"].get(card, 0) + 1
            state["kinds"][card] = argv[6]
        if verb in ("block", "complete"):
            state["seq"] += 1
            state["events"][card] = state["seq"]
        cards[card] = {"block": "blocked", "archive": "archived", "complete": "done", "unblock": "ready"}[verb]
        return ""
    monkeypatch.setattr(core, "run", fake_run)
    return state


@pytest.fixture
def run1(board):
    core.save_json(runs.run_dir(CARD) / "run.json", RUN)
    board["cards"][CARD] = "ready"
    return runs.run_dir(CARD)


def fire(monkeypatch, event, raw=None, card=CARD, **payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw if raw is not None else json.dumps(payload)))
    return runs.hook(argparse.Namespace(card=card, event=event, url=None))


def recover(card, resend=False, adopt=False):
    return runs.recover(card, resend, adopt)


def calls(state, verb):
    return [c for c in state["calls"] if c[:2] == ["hermes", "kanban"] and c[4] == verb]


def block_text(state, card):
    return next(c[8] for c in calls(state, "block") if c[7] == card)


def files(run_dir, box):
    return sorted(p.name for p in (run_dir / box).glob("*.json"))


def test_blocked_an_ask_opens_one_subscribed_wait_card_and_waits_for_the_gateway_ack(board, run1, monkeypatch, capsys):
    assert fire(monkeypatch, "notification", message="Claude needs your permission") == 0
    assert board["cards"] == {CARD: "ready", "t_wait1": "blocked"}
    assert block_text(board, "t_wait1") == "Claude needs your permission\nReply in Herdr pane w_1:p2."
    assert [c[5] for c in calls(board, "notify-subscribe")] == ["t_wait1"]
    body = calls(board, "create")[0]
    assert "| branch fix/x |" in body[body.index("--body") + 1] and core.PROVENANCE in body[body.index("--body") + 1]
    assert files(run1, "outbox") == [] and len(files(run1, "sent")) == 1
    runs.drain(CARD)  # the gateway has not pinged yet
    assert len(files(run1, "sent")) == 1
    board["cursor"] = board["seq"]
    runs.drain(CARD)
    assert files(run1, "sent") == []
    assert capsys.readouterr().out == ""


def test_a_turn_end_without_a_pull_request_is_not_success_and_pages_no_one(board, run1, monkeypatch):
    """A turn also ends while background work runs (issue #15): only the flush's idle page follows."""
    fire(monkeypatch, "stop", last_assistant_message="I think I am done.")
    assert board["cards"] == {CARD: "ready"} and calls(board, "complete") == []
    assert calls(board, "block") == [] and files(run1, "outbox") == [] and files(run1, "sent") == []


@pytest.mark.parametrize("change", [
    {"head": "def"}, {"dirty": " M a.py\n"}, {"prs": [PR | {"isDraft": True}]},
    {"prs": [PR | {"baseRefName": "dev"}]}, {"prs": [PR, PR | {"url": "https://github.com/o/r/pull/8"}]},
    {"prs": [PR | {"state": "CLOSED"}]},
], ids=["unpushed", "dirty", "draft", "wrong-base", "two-prs", "closed-unmerged"])
def test_each_unfinished_pr_shape_is_not_success(board, run1, monkeypatch, change):
    board.update({"prs": [PR]} | change)
    fire(monkeypatch, "stop")
    assert board["cards"] == {CARD: "ready"} and calls(board, "complete") == []


def test_a_merged_pr_whose_head_is_head_is_finished(board, run1, monkeypatch):
    board["prs"] = [PR | {"state": "MERGED"}, PR | {"state": "CLOSED", "url": "https://github.com/o/r/pull/6"}]
    fire(monkeypatch, "stop")
    assert board["cards"][CARD] == "done"
    # Merged is what gh said; deployed or live is not, so the summary does not claim it.
    assert calls(board, "complete")[0][7] == f"Merged: {PR['url']} (not checked live)"


def test_a_verified_pr_completes_the_ledger_with_run_pr_pane_links(board, run1, monkeypatch):
    fire(monkeypatch, "notification", message="Claude needs your permission")
    board["prs"] = [PR]
    fire(monkeypatch, "stop")
    assert board["cards"] == {CARD: "done", "t_wait1": "archived"}
    [complete] = calls(board, "complete")
    assert complete[5:7] == [CARD, "--summary"]
    assert complete[7] == f"Ready for review: {PR['url']} (open; not merged or deployed)"
    assert not (run1 / "muster-wait").exists()


def test_duplicate_delivery_completes_once(board, run1, monkeypatch):
    board["prs"] = [PR]
    fire(monkeypatch, "stop")
    fire(monkeypatch, "stop")
    fire(monkeypatch, "session-end", reason="logout")
    runs.drain(CARD)
    assert len(calls(board, "complete")) == 1 and calls(board, "block") == []
    assert files(run1, "outbox") == []


def test_a_kill_after_the_move_before_the_record_redelivers_without_a_second_move(board, run1, monkeypatch):
    board["prs"] = [PR]
    real, killed = core.save_json, []

    def save_json(path, data):
        if path.parent.name == "sent" and not killed:
            killed.append(path)
            raise KeyboardInterrupt  # the process dies between the kanban move and its record
        real(path, data)
    monkeypatch.setattr(core, "save_json", save_json)
    runs.enqueue(CARD, "stop", "")
    with pytest.raises(KeyboardInterrupt):
        runs.drain(CARD)
    assert board["cards"][CARD] == "done" and len(files(run1, "outbox")) == 1
    runs.drain(CARD)
    assert len(calls(board, "complete")) == 1
    assert files(run1, "outbox") == [] and len(files(run1, "sent")) == 1  # the completion's ack is still owed
    board["cursor"] = board["seq"]
    runs.drain(CARD)
    assert files(run1, "sent") == []


def test_unknown_fails_closed_and_is_retried(board, run1, monkeypatch):
    board.update(prs=[PR], gh_fail=1)
    fire(monkeypatch, "stop")
    assert board["cards"] == {CARD: "ready"} and calls(board, "complete") == []
    [saved] = (run1 / "outbox").glob("*.json")
    assert "HTTP 502" in json.loads(saved.read_text())["error"]
    runs.drain(CARD)
    assert board["cards"][CARD] == "done"


def test_crash_restart_a_saved_event_survives_a_kanban_outage(board, run1, monkeypatch):
    board["down"] = True
    assert fire(monkeypatch, "notification", message="Claude needs your permission") == 0
    assert len(files(run1, "outbox")) == 1 and calls(board, "create") == []
    board["down"] = False
    runs.drain(CARD)  # a fresh process: everything it needs is on disk
    assert board["cards"]["t_wait1"] == "blocked" and files(run1, "outbox") == []


def test_a_prompt_with_nothing_open_writes_nothing(board, run1, monkeypatch):
    assert fire(monkeypatch, "prompt") == 0
    assert board["calls"] == [] and not (run1 / "outbox").exists()


def test_a_prompt_closes_the_open_wait_silently_and_the_archive_acks_it(board, run1, monkeypatch):
    fire(monkeypatch, "notification", message="Claude needs your permission")
    fire(monkeypatch, "prompt")
    assert board["cards"]["t_wait1"] == "archived" and not (run1 / "muster-wait").exists()
    assert files(run1, "sent") == [] and board["blocks"] == {"t_wait1": 1}


def test_session_end_without_a_pr_blocks_the_ledger_once_and_clear_does_nothing(board, run1, monkeypatch):
    fire(monkeypatch, "session-end", reason="clear")
    assert board["calls"] == []
    fire(monkeypatch, "notification", message="Claude needs your permission")
    fire(monkeypatch, "session-end", reason="logout")
    fire(monkeypatch, "session-end", reason="logout")
    assert board["cards"] == {CARD: "blocked", "t_wait1": "archived"}
    assert board["blocks"][CARD] == 1
    assert block_text(board, CARD) == ("The agent's session ended without a finished pull request: there is no open pull request yet.\n"
                                      "Check Herdr pane w_1:p2.")


def test_an_empty_wait_marker_left_by_a_killed_hook_does_not_swallow_the_next_wait(board, run1, monkeypatch):
    (run1 / "muster-wait").write_text("")  # claimed, then killed before the card id was recorded
    fire(monkeypatch, "notification", message="Claude needs your permission")
    assert board["cards"]["t_wait1"] == "blocked" and (run1 / "muster-wait").read_text() == "t_wait1"


def test_the_hook_never_raises_or_prints(board, run1, monkeypatch, capsys):
    assert fire(monkeypatch, "stop", raw="not json") == 0
    assert fire(monkeypatch, "stop", raw="[1]", card="t_nope") == 0
    monkeypatch.setattr(runs, "drain", lambda card: 1 / 0)
    assert fire(monkeypatch, "notification") == 0
    assert capsys.readouterr().out == ""
    log = runs.log_path().read_text()
    assert "t_nope hook stop: no run t_nope" in log and "division by zero" in log


def test_an_unacked_event_is_logged_once_after_15_minutes(board, run1, monkeypatch):
    fire(monkeypatch, "notification", message="Claude needs your permission")
    [path] = (run1 / "sent").glob("*.json")
    path.write_text(json.dumps(json.loads(path.read_text()) | {"moved_at": int(time.time()) - 16 * 60}))
    runs.drain(CARD)
    runs.drain(CARD)
    assert runs.log_path().read_text().count("has not acked card t_wait1 after 15 min") == 1


def test_a_held_lock_leaves_the_event_for_the_flush(board, run1, monkeypatch):
    with open(run1 / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert fire(monkeypatch, "notification", message="m") == 0
    assert len(files(run1, "outbox")) == 1 and calls(board, "create") == []
    runs.flush()
    assert board["cards"]["t_wait1"] == "blocked" and files(run1, "outbox") == []


def test_a_prompt_behind_an_undelivered_wait_is_delivered_after_it(board, run1, monkeypatch):
    board["fail"]["create"] = 2
    fire(monkeypatch, "notification", message="Claude needs your permission")
    fire(monkeypatch, "prompt")  # the wait fails again: the prompt waits behind it, not run first
    assert len(files(run1, "outbox")) == 2 and board["cards"] == {CARD: "ready"}
    runs.drain(CARD)
    assert board["cards"]["t_wait1"] == "archived" and not (run1 / "muster-wait").exists()
    assert files(run1, "outbox") == []


# flush: the backstop for a hook that never ran, and the page for an agent that stopped unfinished


def test_missed_callback_an_idle_pane_with_a_finished_pr_completes_on_flush(board, run1):
    board.update(agent="idle", prs=[PR])
    runs.flush()
    assert board["cards"][CARD] == "done"
    assert json.loads((run1 / "run.json").read_text()).get("closed") is None  # its ack is still owed
    board["cursor"] = board["seq"]
    runs.flush()
    assert json.loads((run1 / "run.json").read_text())["closed"] is True
    board["calls"].clear()
    runs.flush()
    assert board["calls"] == []  # a closed run costs nothing


def test_an_idle_pane_without_a_pr_pages_once_after_ten_minutes(board, run1, monkeypatch):
    board["agent"] = "idle"
    runs.flush()
    runs.flush()
    assert board["cards"] == {CARD: "ready"} and calls(board, "block") == []
    idle = run1 / "idle.json"
    idle.write_text(json.dumps(json.loads(idle.read_text()) | {"since": int(time.time()) - 11 * 60}))
    runs.flush()
    runs.flush()
    assert board["cards"] == {CARD: "ready", "t_wait1": "blocked"} and board["blocks"] == {"t_wait1": 1}
    assert block_text(board, "t_wait1") == ("The agent has been idle for 10 minutes and is not finished: "
                                            "there is no open pull request yet.\nReply in Herdr pane w_1:p2.")


def test_an_agent_that_moves_restarts_the_idle_clock(board, run1):
    board["agent"] = "idle"
    runs.flush()
    idle = run1 / "idle.json"
    idle.write_text(json.dumps(json.loads(idle.read_text()) | {"since": int(time.time()) - 11 * 60}))
    board["agent_seq"] = 2  # it worked and went idle again between flushes
    runs.flush()
    assert calls(board, "block") == [] and json.loads(idle.read_text())["seq"] == 2
    board["agent"] = "working"
    runs.flush()
    assert not idle.exists()


def test_missed_callback_a_gone_pane_blocks_the_ledger(board, run1):
    board["agent"] = None
    runs.flush()
    assert board["cards"] == {CARD: "blocked"}
    assert block_text(board, CARD) == ("The agent's session ended without a finished pull request: there is no open pull request yet.\n"
                                      "Check Herdr pane w_1:p2.")


def test_missed_callback_a_dialog_opens_a_wait(board, run1):
    board["agent"] = "blocked"
    runs.flush()
    runs.flush()
    assert board["blocks"] == {"t_wait1": 1}
    assert block_text(board, "t_wait1") == "The agent is waiting on a dialog.\nReply in Herdr pane w_1:p2."


@pytest.mark.parametrize("setup", ["working", "open-wait", "herdr-down", "not-launched", "ledger-blocked"])
def test_nothing_is_enqueued_when_there_is_nothing_to_catch(board, run1, setup):
    board["agent"] = "idle"
    if setup == "working":
        board["agent"] = "working"
    elif setup == "open-wait":
        (run1 / "muster-wait").write_text("t_wait9")
    elif setup == "herdr-down":
        board["agent"] = "herdr-down"
    elif setup == "not-launched":
        core.save_json(run1 / "run.json", RUN | {"launched": False})
    else:
        board["cards"][CARD] = "blocked"
    runs.flush()
    assert files(run1, "outbox") == [] and [c for c in board["calls"] if c[:1] == ["gh"]] == []
    assert board["cards"][CARD] in ("ready", "blocked") and len(board["cards"]) == 1


def test_flush_retries_a_failed_event(board, run1, monkeypatch):
    board.update(prs=[PR], gh_fail=1)
    fire(monkeypatch, "stop")
    assert board["cards"][CARD] == "ready"
    runs.flush()
    assert board["cards"][CARD] == "done"


# launch: the run is registered and subscribed before any worktree or pane exists


@pytest.fixture
def clone(board, tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("Fix the thing.\n")

    base_run = core.run
    world = board["world"] = World(tmp_path)
    world.on_submit = lambda text: None

    def fake_run(argv):
        if argv[:2] == ["git", "-C"] and argv[3:] == ["remote", "get-url", "origin"]:
            board["calls"].append(argv)
            return "git@github.com:o/r.git\n"
        if argv[0] == "herdr" or (argv[0] == "git" and argv[3:] != ["rev-parse", "HEAD"]
                                  and "--untracked-files=no" not in argv):
            board["calls"].append(argv)
            return world.handle(argv)
        return base_run(argv)
    monkeypatch.setattr(core, "run", fake_run)
    return {"brief": brief, "path": tmp_path / "clone"}


def launch(clone, branch="fix/x"):
    return runs.launch_run(clone["path"], branch, "Fix the thing", clone["brief"])


def test_launch_registers_and_subscribes_before_any_herdr_call(board, clone):
    run = launch(clone)
    card = run["card"]
    verbs = [c[4] if c[:2] == ["hermes", "kanban"] else " ".join(c[:3]) for c in board["calls"]]
    first_herdr = next(i for i, v in enumerate(verbs) if v.startswith("herdr"))
    assert verbs.index("create") < verbs.index("notify-subscribe") < verbs.index("notify-list") < first_herdr
    create = calls(board, "create")[0]
    assert create[create.index("--idempotency-key") + 1] == "run:o/r:fix/x" and create[-1] == "Fix the thing"
    assert core.PROVENANCE in create[create.index("--body") + 1]
    saved = json.loads((runs.run_dir(card) / "run.json").read_text())
    worktree = str(core.worktree_path(clone["path"].resolve(), "fix/x"))
    assert saved == run and {k: v for k, v in run.items() if k != "launch"} == {
        "card": card, "repo": "o/r", "clone": str(clone["path"].resolve()), "branch": "fix/x", "base": "main",
        "title": "Fix the thing", "workspace": "w1", "worktree": worktree, "pane": "w1:p2", "launched": True,
        "state": "working"}
    assert run["launch"]["name"] == "run-x" and run["launch"]["prompt"]["state"] == "working"
    [comment] = calls(board, "comment")
    assert comment[5] == card and comment[6].startswith(core.LINKS_PREFIX)
    assert board["cards"][card] == "ready"


def test_launch_without_base_uses_origin_head_and_base_wins_when_given(board, clone):
    board["world"].origin_head = "master"
    run = launch(clone)
    assert run["base"] == run["launch"]["base"] == "master"
    create = next(c for c in board["calls"] if c[:3] == ["herdr", "worktree", "create"])
    assert create[create.index("--base") + 1] == "origin/master"
    other = runs.launch_run(clone["path"], "fix/y", "Fix another", clone["brief"], base="develop")
    assert other["base"] == "develop"


def test_launch_settings_hook_the_run_card_including_stop_and_session_end(board, clone):
    card = launch(clone)["card"]
    hooks = json.loads((runs.run_dir(card) / "settings.json").read_text())["hooks"]
    command = {name: entry[0]["hooks"][0]["command"] for name, entry in hooks.items()}
    assert command["Stop"].endswith(f"hook --card {card} stop")
    assert command["SessionEnd"].endswith(f"hook --card {card} session-end")
    assert command["PostToolUse"].endswith(f"hook --card {card} prompt")
    assert hooks["Notification"][0]["matcher"] == claude.ASK_NOTIFICATIONS
    assert hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion"
    assert {entry[0]["hooks"][0]["timeout"] for entry in hooks.values()} == {30}
    start = next(c for c in board["calls"] if c[:3] == ["herdr", "agent", "start"])
    assert start[start.index("--settings") + 1] == str(runs.run_dir(card) / "settings.json")


def test_launch_brief_is_the_callers_text_plus_the_run_footer(board, clone):
    card = launch(clone)["card"]
    brief = (runs.run_dir(card) / "brief.md").read_text()
    assert brief.startswith("Fix the thing.\n")
    assert f"## This run: {card}" in brief and "`fix/x`" in brief and "You never report it yourself" in brief


def test_launch_opens_a_trusted_worktree_and_starts_claude_in_auto_mode(board, clone):
    launch(clone)
    create = next(c for c in board["calls"] if c[:3] == ["herdr", "worktree", "create"])
    assert "--trust-repository" in create
    start = next(c for c in board["calls"] if c[:3] == ["herdr", "agent", "start"])
    assert start[start.index("--permission-mode") + 1] == "auto" and start[-2] == "--settings"
    assert "bypassPermissions" not in start and "--dangerously-skip-permissions" not in start


def test_launch_first_prompt_is_native_not_pasted_or_a_read_of_its_file(board, clone):
    card = launch(clone)["card"]
    [start] = [c for c in board["calls"] if c[:3] == ["herdr", "agent", "start"]]
    [submit] = [c for c in board["calls"] if c[:3] == ["herdr", "agent", "prompt"]]
    brief = (runs.run_dir(card) / "brief.md").read_text()
    assert "Execute the authorized task" not in " ".join(start)  # start carries no task
    assert not any(ord(c) < 32 or ord(c) == 127 for c in submit[4])
    assert submit[4].startswith("Execute the authorized task")
    assert json.loads(submit[4].split("Task brief (JSON): ", 1)[1]) == brief
    assert board["calls"].index(start) < board["calls"].index(submit)


def test_a_failed_subscribe_blocks_the_card_and_opens_no_worktree(board, clone):
    board["fail"]["notify-subscribe"] = 1
    with pytest.raises(core.LaunchError, match="launch failed at subscribe"):
        launch(clone)
    assert board["cards"] == {"t_wait1": "blocked"}
    assert [c for c in board["calls"] if c[:1] == ["herdr"]] == []


def test_a_second_launch_on_the_same_branch_is_refused(board, clone):
    launch(clone)
    board["created_at"]["t_wait1"] = 0
    board["calls"].clear()
    with pytest.raises(core.LaunchError, match="already has run t_wait1"):
        launch(clone)
    assert [c for c in board["calls"] if c[:1] == ["herdr"]] == []


@pytest.mark.parametrize("branch", ["main", "feature/x", "fix/", "fix/X y"])
def test_a_bad_branch_is_refused_before_any_card(board, clone, branch):
    with pytest.raises(core.LaunchError, match="is not <feat"):
        launch(clone, branch)
    assert board["calls"] == []


def test_the_launch_entry_exits_one_on_a_refused_launch(board, clone, monkeypatch, capsys):
    monkeypatch.setattr(config, "require", lambda: None)
    args = argparse.Namespace(cwd="/c", branch="main", title="t", brief="/b", base="main", model=None)
    assert runs.launch(args) == 1
    assert "is not <feat" in capsys.readouterr().err


def test_the_launch_entry_prints_the_run_and_defaults_the_model(board, clone, monkeypatch, capsys):
    monkeypatch.setattr(config, "require", lambda: None)
    monkeypatch.setitem(config.settings, "agent_model", "sonnet")
    args = argparse.Namespace(cwd=str(clone["path"]), branch="fix/x", title="T", brief=str(clone["brief"]),
                              base="main", model=None)
    assert runs.launch(args) == 0
    assert json.loads(capsys.readouterr().out)["launch"]["model"] == "sonnet"


def test_a_done_hook_on_a_run_is_refused_without_reading_stdin_or_queueing(board, run1, capsys):
    assert runs.hook(argparse.Namespace(card=CARD, event="done", url=None)) == 1
    assert "pull request" in capsys.readouterr().err and not (run1 / "outbox").exists()


def test_launch_carries_the_pane_env_and_recover_refreshes_it(board, clone, monkeypatch, tmp_path):
    board["world"].start = "none"
    with pytest.raises(core.LaunchError):
        launch(clone)
    assert runs.load("t_wait1")["launch"]["env"] == core.pane_env()
    assert any(e.startswith("GH_CONFIG_DIR=") for e in core.pane_env())
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "moved"))
    board["world"].start = "ready"
    assert recover("t_wait1") == 0
    assert "HERMES_KANBAN_HOME=" + str(tmp_path / "moved") in runs.load("t_wait1")["launch"]["env"]


@pytest.mark.parametrize("gap", ["no-event-in-the-db", "subscription-gone-on-a-live-card"])
def test_an_ack_that_cannot_be_read_is_not_an_ack(board, run1, monkeypatch, gap):
    fire(monkeypatch, "notification", message="Claude needs your permission")
    board["cursor"] = 99
    if gap == "no-event-in-the-db":
        board["events"].clear()
    else:
        board["no_subs"] = True
    runs.drain(CARD)
    assert len(files(run1, "sent")) == 1


def test_a_failed_links_comment_leaves_the_running_pane_unblocked(board, clone):
    board["fail"]["comment"] = 1
    run = launch(clone)
    assert run["launched"] and board["cards"][run["card"]] == "ready"
    assert "links comment failed" in runs.log_path().read_text()


def test_an_unverifiable_stop_does_not_hold_up_a_later_ask(board, run1, monkeypatch):
    board["gh_fail"] = 99
    fire(monkeypatch, "stop")
    fire(monkeypatch, "notification", message="Claude needs your permission")
    assert board["cards"]["t_wait1"] == "blocked"
    assert [json.loads(p.read_text())["event"] for p in (run1 / "outbox").glob("*.json")] == ["stop"]


def test_a_pane_that_no_longer_runs_claude_ends_the_session(board, run1, monkeypatch):
    real = core.run

    def run(argv):
        if argv[:3] == ["herdr", "agent", "get"]:
            return json.dumps({"result": {"agent": {"agent_status": "unknown"}}})
        return real(argv)
    monkeypatch.setattr(core, "run", run)
    runs.flush()
    assert board["cards"] == {CARD: "blocked"}


def test_queued_prompts_collapse_to_one(board, run1, monkeypatch):
    board["down"] = True
    fire(monkeypatch, "notification", message="m")
    for _ in range(5):
        fire(monkeypatch, "prompt")
    assert [p.name.split("-", 1)[1] for p in runs.pending(CARD)] == ["notification.json", "prompt.json"]


def test_a_killed_launch_still_blocks_its_card(board, clone, monkeypatch):
    real = core.run

    def run(argv):
        if argv[:3] == ["herdr", "agent", "start"]:
            raise KeyboardInterrupt
        return real(argv)
    monkeypatch.setattr(core, "run", run)
    with pytest.raises(KeyboardInterrupt):
        launch(clone)
    assert board["cards"] == {"t_wait1": "blocked"}
    assert block_text(board, "t_wait1") == ("The coding agent did not start (the agent step failed). This is setup "
                                            "trouble, not a product decision.\nDetails: KeyboardInterrupt")


def test_a_second_launch_in_the_same_second_is_refused(board, clone):
    launch(clone)  # the fake hands the same card back with the same created_at
    with pytest.raises(core.LaunchError, match="already has run t_wait1"):
        launch(clone)


# recover: resume a failed launch from its record, never sending the brief twice on a guess


def test_an_unconfirmed_brief_blocks_as_unknown_and_recover_needs_a_person_to_resend(board, clone, capsys):
    world = board["world"]
    world.prompt = "lost"
    with pytest.raises(core.LaunchError, match="launch failed at prompt"):
        launch(clone)
    text = block_text(board, "t_wait1")
    assert text.startswith("The coding agent started, but its first prompt may not have arrived. Nothing was resent.")
    assert "did not start" not in text and len(world.submitted) == 1
    assert recover("t_wait1") == 1
    assert "--resend" in capsys.readouterr().err and len(world.submitted) == 1
    assert board["cards"]["t_wait1"] == "blocked" and board["blocks"]["t_wait1"] == 1  # commented, not re-blocked
    assert calls(board, "comment")[-1][6].startswith("Recovery failed again.")
    world.prompt = "working"
    assert recover("t_wait1", resend=True) == 0
    assert len(world.submitted) == 2 and len(world.agents) == 1
    assert board["cards"]["t_wait1"] == "ready" and len(calls(board, "unblock")) == 1
    run = runs.load("t_wait1")
    assert run["launched"] and run["launch"]["prompt"]["state"] == "working" and "resend" not in run["launch"]
    assert recover("t_wait1") == 0  # delivered: nothing more is sent
    assert len(world.submitted) == 2


def test_recover_resumes_a_launch_whose_agent_never_became_ready(board, clone):
    world = board["world"]
    world.start = "blocked"
    with pytest.raises(core.LaunchError):
        launch(clone)
    assert "did not become ready" in block_text(board, "t_wait1") and world.submitted == []
    world.agents["w1:p2"]["agent_status"] = "idle"
    assert recover("t_wait1") == 0
    assert len(world.submitted) == 1 and len(calls(board, "unblock")) == 1
    assert len([c for c in board["calls"] if c[:3] == ["herdr", "agent", "start"]]) == 1


def test_recover_refuses_a_run_with_a_pull_request(board, clone, capsys):
    board["world"].start = "none"
    with pytest.raises(core.LaunchError):
        launch(clone)
    board["prs"] = [PR]
    assert recover("t_wait1") == 1
    assert "already open or merged" in capsys.readouterr().err
    assert board["cards"]["t_wait1"] == "blocked" and not calls(board, "unblock")


def test_recover_refuses_while_another_recover_holds_the_run(board, clone, capsys):
    board["world"].start = "none"
    with pytest.raises(core.LaunchError):
        launch(clone)
    before = len(board["calls"])
    with core.launch_lock(runs.run_dir("t_wait1")):
        assert recover("t_wait1") == 1
    assert "another launch or recover" in capsys.readouterr().err
    assert board["calls"][before:] == []


def test_recover_refuses_a_session_that_ended_after_its_launch(board, run1, capsys):
    core.kanban("block", "--kind", "needs_input", CARD, "The agent's session ended")
    assert recover(CARD) == 1
    assert "only a ready card or one blocked by a launch failure" in capsys.readouterr().err


def test_a_run_from_before_launch_records_needs_adopt_and_reuses_its_worktree(board, clone, capsys):
    world = board["world"]
    path, _ = world.worktree(clone["path"].resolve(), "fix/x", path=clone["path"].parent / "old-wt")
    old = {"card": "t_old", "repo": "o/r", "clone": str(clone["path"].resolve()), "branch": "fix/x", "base": "main",
           "title": "Old run", "workspace": world.worktrees[path]["workspace"], "worktree": path, "pane": "w9:p9"}
    core.save_json(runs.run_dir("t_old") / "run.json", old)
    (runs.run_dir("t_old") / "brief.md").write_text("Old brief.\n\n## This run: t_old\nfooter\n")
    board["cards"]["t_old"], board["kinds"]["t_old"] = "blocked", "capability"
    world.dirty[path] = ["wip.py"]
    assert recover("t_old") == 1
    assert "predates launch records" in capsys.readouterr().err and world.submitted == []
    assert recover("t_old", adopt=True) == 0
    assert not [c for c in board["calls"] if c[:3] == ["herdr", "worktree", "create"]]
    brief = json.loads(world.submitted[0].split("(JSON): ", 1)[1])
    assert brief.startswith("Old brief.\n") and "1 uncommitted or untracked files" in brief
    assert runs.load("t_old")["worktree"] == path and board["cards"]["t_old"] == "ready"


def test_a_long_or_dotted_branch_gets_a_valid_agent_name(board, clone):
    run = launch(clone, "fix/v1.2-a-really-long-branch-name-for-the-agent")
    assert core.AGENT_NAME.fullmatch(run["launch"]["name"])


def test_the_run_hook_records_the_submitted_prompt_for_the_launcher(board, run1, monkeypatch):
    fire(monkeypatch, "prompt", prompt="the brief", session_id="s1")
    assert core.seen(run1, __import__("hashlib").sha256(b"the brief").hexdigest())
    assert board["calls"] == []  # nothing open, nothing queued: still no kanban call


def test_core_recover_dispatches_to_runs(board, run1, monkeypatch):
    monkeypatch.setattr(config, "require", lambda: None)
    monkeypatch.setattr(core, "board_exists", lambda: None)
    seen = []
    monkeypatch.setattr(runs, "recover", lambda card, resend, adopt: seen.append((card, resend, adopt)) or 0)
    monkeypatch.setattr(core, "recover_card", lambda card, resend, adopt: seen.append(("core", card)) or 0)
    assert core.recover(argparse.Namespace(card=CARD, resend=True, adopt=False)) == 0
    assert core.recover(argparse.Namespace(card="t_issue", resend=False, adopt=False)) == 0
    assert seen == [(CARD, True, False), ("core", "t_issue")]
