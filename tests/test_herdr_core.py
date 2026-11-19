import re
import json
import shlex

import pytest

import muster.core as core
import muster.hosts as hosts
import muster.hosts.herdr_core as herdr_core
from muster.hosts import NotReady
from tests.fake_herdr_core import HcWorld


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = HcWorld(tmp_path)

    def run(argv, env=None):
        out = world.handle(argv)
        if out is None:
            raise AssertionError(argv)
        return out
    monkeypatch.setattr(core, "run", run)
    monkeypatch.setattr(herdr_core, "sleep", lambda s: None)
    world.clone = tmp_path / "clone"
    world.clone.mkdir()
    return world


def jump(monkeypatch, *times):
    """herdr_core.clock returns these, then the last one forever (tests/test_orca.py's jump, for this module)."""
    seq = list(times)
    monkeypatch.setattr(herdr_core, "clock", lambda: seq.pop(0) if len(seq) > 1 else seq[0])


def calls(w, *verb):
    return [c for c in w.calls if isinstance(c, list) and c[:len(verb)] == list(verb)]


def checkout(w, branch="muster/7"):
    """A new checkout through herdr_core itself; its path is also its workspace."""
    return herdr_core.create_worktree(w.clone, branch, "main", herdr_core.worktree_path(w.clone, branch), "l")["path"]


def test_worktrees_are_open_at_their_path(w):
    p = str(w.tmp / "wt" / "b")
    w.worktree(w.clone, "b", p)
    found = {wt["path"]: wt for wt in herdr_core.worktrees(w.clone)}
    assert found[p] == {"path": p, "branch": "b", "open_workspace_id": p, "is_linked_worktree": True}
    assert found[str(w.clone)]["is_linked_worktree"] is False


def test_create_makes_a_new_branch_from_origin(w):
    path = str(w.tmp / "wt" / "feat-x")
    made = herdr_core.create_worktree(w.clone, "feat/x", "main", path, "l")
    assert made == {"workspace": path, "path": path}
    assert ["git", "-C", str(w.clone), "worktree", "add", "--no-track", "-b", "feat/x", path, "origin/main"] in calls(w, "git")


def test_create_checks_out_an_existing_branch_as_it_is(w):
    w.branches[(str(w.clone), "fix/x")] = "sha_x"
    path = str(w.tmp / "wt" / "fix-x")
    herdr_core.create_worktree(w.clone, "fix/x", "main", path, "l")
    assert ["git", "-C", str(w.clone), "worktree", "add", path, "fix/x"] in calls(w, "git")


def test_the_fake_answers_like_the_live_check(w):
    run = ["hc", "run", "--bg", "--no-parent", "--outlive-parent", "--keep", "--name", "a", "--cwd", "/x", "--json"]
    address = json.loads(core.run(run))["address"]
    with pytest.raises(core.CommandError, match="name_taken"):
        core.run(run)
    assert "foreground" not in json.loads(core.run(["hc", "get", "a", "--json"]))
    w.hc_claude(address, "blocked")
    with pytest.raises(core.CommandError, match="blocked"):
        core.run(["hc", "send", "--wait", "--timeout", "10", "--json", "--", "a", "x"])
    assert "labels" not in json.loads(core.run(["hc", "ls", "--json"]))["terminals"][0]


def labelled(w, name):
    return next(t for t in w.hc.values() if t["name"] == name)["labels"]


def test_open_tab_starts_a_labelled_shell_then_labels_it_into_a_new_space(w):
    path = checkout(w)
    assert herdr_core.open_tab(path, path, "run", [], "run-x") == "run-x"
    run = calls(w, "hc", "run")[0]
    assert run[run.index("--name") + 1] == "run-x"
    assert run[run.index("--cwd") + 1] == path
    assert f"muster.path={path}" in run
    assert not any(a.startswith("herdr.space") for a in run)
    (sid,) = w.spaces
    assert calls(w, "hc", "label")[-1] == ["hc", "label", "run-x", f"herdr.space={sid}", "--json"]
    assert w.spaces[sid]["folder"] == path


def test_open_tab_reuses_the_space_already_open_on_the_folder(w):
    path = checkout(w)
    w.spaces["sp9"] = {"name": "x", "folder": path}
    herdr_core.open_tab(path, path, "run", [], "run-x")
    assert not calls(w, "herdr-app", "do", "space.new")
    assert calls(w, "hc", "label")[-1][3] == "herdr.space=sp9"


def test_open_tab_opens_a_window_when_none_is_up(w):
    path = checkout(w)
    w.window = False
    herdr_core.open_tab(path, path, "run", [], "run-x")
    assert ["open", "-g", "-a", "Herdr"] in calls(w, "open")
    assert "herdr.space" in labelled(w, "run-x")


def test_open_tab_runs_hidden_when_no_window_comes_up(w, monkeypatch):
    path = checkout(w)
    w.window = w.opens = False
    jump(monkeypatch, 0, 31)
    assert herdr_core.open_tab(path, path, "run", [], "run-x") == "run-x"
    assert "herdr.space" not in labelled(w, "run-x")
    assert "runs hidden" in core.log_path("hosts").read_text()


def test_open_tab_reuses_a_live_terminal_and_labels_it_into_a_space_later(w, monkeypatch):
    path = checkout(w)
    w.window = w.opens = False
    jump(monkeypatch, 0, 31)
    herdr_core.open_tab(path, path, "run", [], "run-x")
    w.window = True
    herdr_core.open_tab(path, path, "run", [], "run-x")
    assert len(calls(w, "hc", "run")) == 1
    (sid,) = w.spaces
    assert len([c for c in calls(w, "hc", "label") if c[3] == f"herdr.space={sid}"]) == 1


def test_open_tab_refuses_a_name_live_in_another_workspace(w):
    path = checkout(w)
    w.term("run-x", "/other", {"muster.path": "/other"})
    with pytest.raises(core.CommandError):
        herdr_core.open_tab(path, path, "run", [], "run-x")


def test_agent_maps_hc_status_to_herdrs_shape(w):
    path, a = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    got = herdr_core.agent("run-x")
    assert got["agent"] == "claude"
    assert got["agent_status"] == "done"
    assert got["workspace_id"] == path
    assert got["pane_id"] == "run-x"
    assert got["state_change_seq"] == w.hc[a]["status_ms"]
    w.hc[a]["status"]["state"] = "error"
    assert herdr_core.agent("run-x")["agent_status"] == "hc-error"


def test_agent_is_none_for_a_shell_a_finished_terminal_a_stale_status_and_after_a_core_restart(w):
    w.term("sh-x", "/x")
    assert herdr_core.agent("sh-x") is None
    w.hc_live(w.clone, "muster/1", "run-x")
    core.run(["hc", "kill", "run-x", "--json"])
    assert herdr_core.agent("run-x") is None
    _, a = w.hc_live(w.clone, "muster/2", "run-y")
    w.hc[a]["foreground"] = None
    assert herdr_core.agent("run-y") is None
    w.hc_live(w.clone, "muster/3", "run-z")
    w.hc_restart()
    assert herdr_core.agent("run-z") is None


def test_start_agent_types_env_and_claude_then_returns_when_ready(w):
    path = checkout(w)
    herdr_core.open_tab(path, path, "run", [], "run-x")
    herdr_core.start_agent("run-x", "claude", "run-x", ["--model", "opus"], ["A=1"])
    assert w.typed == ["export A=1; claude --model opus"]


def test_start_agent_returns_at_a_trust_dialog(w):
    path = checkout(w)
    w.hc_start = "blocked"
    herdr_core.open_tab(path, path, "run", [], "run-x")
    herdr_core.start_agent("run-x", "claude", "run-x", [], [])
    assert herdr_core.agent("run-x")["agent_status"] == "blocked"


def test_start_agent_waits_for_a_booting_claude_without_typing(w):
    path = checkout(w)
    w.hc_late = 2
    address = herdr_core.open_tab(path, path, "run", [], "run-x")
    w.hc_claude(next(t["address"] for t in w.hc.values() if t["name"] == address), "idle")
    herdr_core.start_agent("run-x", "claude", "run-x", [], [])
    assert w.typed == []


def test_start_agent_types_nothing_into_a_pane_running_something_else(w):
    path = checkout(w)
    herdr_core.open_tab(path, path, "run", [], "run-x")
    next(t for t in w.hc.values() if t["name"] == "run-x")["foreground"] = {"argv": ["vim"], "pid": 1}
    with pytest.raises(core.CommandError):
        herdr_core.start_agent("run-x", "claude", "run-x", [], [])
    assert w.typed == []


def test_start_agent_times_out(w, monkeypatch):
    path = checkout(w)
    w.hc_start = "none"
    herdr_core.open_tab(path, path, "run", [], "run-x")
    jump(monkeypatch, 0, 121)
    with pytest.raises(core.CommandError):
        herdr_core.start_agent("run-x", "claude", "run-x", [], [])


def test_prompt_delivers_with_turn_proof(w):
    w.hc_live(w.clone, "muster/7", "run-x")
    assert herdr_core.prompt("run-x", "the brief") == "working"
    assert w.submitted == ["the brief"]
    assert w.calls[-1] == ["hc", "send", "--wait", "--timeout", "10", "--json", "--", "run-x", "the brief"]


def test_prompt_on_a_dialog_raises_blocked_and_sends_nothing(w):
    w.hc_live(w.clone, "muster/7", "run-x", state="blocked")
    with pytest.raises(hosts.Blocked):
        herdr_core.prompt("run-x", "the brief")
    assert w.submitted == []


def test_prompt_without_turn_proof_is_unproven(w):
    w.hc_live(w.clone, "muster/7", "run-x")
    w.hc_send = "not_started"
    with pytest.raises(core.CommandError) as err:
        herdr_core.prompt("run-x", "the brief")
    assert not isinstance(err.value, hosts.Blocked)


def test_prompt_to_a_bare_shell_or_a_stale_status_types_nothing(w):
    w.term("sh-x", "/x")
    with pytest.raises(core.CommandError):
        herdr_core.prompt("sh-x", "the brief")
    _, a = w.hc_live(w.clone, "muster/7", "run-x")
    w.hc[a]["foreground"] = None
    with pytest.raises(core.CommandError):
        herdr_core.prompt("run-x", "the brief")
    assert w.typed == w.submitted == []


def test_panes_lists_muster_and_other_terminals_in_the_checkout(w):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x")
    w.term("sub", path + "/sub")
    w.term(None, "/elsewhere")
    found = {p["pane_id"]: p for p in herdr_core.panes(path)}
    assert set(found) == {"run-x", "sub"}
    assert found["run-x"]["agent"] == "claude"
    assert found["sub"]["agent"] is None


def test_panes_does_not_count_a_stale_status_as_an_agent(w):
    path, a = w.hc_live(w.clone, "muster/7", "run-x")
    w.hc[a]["foreground"] = None
    assert [p["agent"] for p in herdr_core.panes(path)] == [None]


def test_a_missing_herdr_app_binary_runs_hidden(w, monkeypatch):
    path = checkout(w)
    real = core.run

    def run(argv, env=None):
        if argv[0] in ("herdr-app", "open"):
            raise FileNotFoundError(argv[0])
        return real(argv, env)
    monkeypatch.setattr(core, "run", run)
    assert herdr_core.open_tab(path, path, "run", [], "run-x") == "run-x"
    assert "herdr.space" not in w.find("run-x")["labels"]


def test_quiet_passes_an_idle_agent_and_the_spaces_shell(w):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    herdr_core.space(path)
    seen = herdr_core.quiet(path)
    assert len(seen) == 2
    json.dumps(seen)


def test_quiet_is_the_same_twice_when_nothing_changed(w):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    assert herdr_core.quiet(path) == herdr_core.quiet(path)


@pytest.mark.parametrize("state", ["working", "blocked"])
def test_quiet_refuses_a_working_agent_or_a_dialog(w, state):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x", state=state)
    with pytest.raises(NotReady):
        herdr_core.quiet(path)


def test_quiet_refuses_a_background_job_but_not_a_helper(w):
    path, a = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    w.procs[998] = (w.hc[a]["foreground"]["pid"], "node /x/foo-mcp")
    herdr_core.quiet(path)
    w.procs[999] = (w.hc[a]["pid"], "sleep 100")
    with pytest.raises(NotReady):
        herdr_core.quiet(path)


def test_quiet_refuses_a_plain_program_in_a_shell(w):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    b = w.term(None, path)
    w.hc[b]["foreground"] = {"argv": ["vim"], "pid": 997}
    w.procs[997] = (w.hc[b]["pid"], "vim")
    with pytest.raises(NotReady):
        herdr_core.quiet(path)
    w.hc[b]["foreground"] = {"argv": ["lazygit"], "pid": 997}
    w.procs[997] = (w.hc[b]["pid"], "lazygit")
    herdr_core.quiet(path)


def test_quiet_passes_a_checkout_with_no_terminals(w):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    core.run(["hc", "kill", "run-x", "--json"])
    assert herdr_core.quiet(path) == []


def test_remove_closes_the_space_ends_terminals_and_removes_the_checkout(w):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    herdr_core.open_tab(path, path, "run", [], "run-x")
    argv = herdr_core.remove_argv(path, path, False)
    w.handle(argv)
    assert w.spaces == {}
    assert not [t for t in w.hc.values() if t["labels"].get("muster.path") == path]
    assert path not in w.worktrees
    assert argv[2].endswith(shlex.join(["git", "-C", str(w.clone), "worktree", "remove", path]))


def test_remove_with_force_passes_force_to_git(w):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    assert herdr_core.remove_argv(path, path, True)[2].endswith(
        shlex.join(["worktree", "remove", "--force", path]))


def test_remove_without_a_window_still_removes_terminals_and_checkout(w):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    w.window = False
    argv = herdr_core.remove_argv(path, path, False)
    assert "space.close" not in argv[2]
    w.handle(argv)
    assert not calls(w, "open")
    assert path not in w.worktrees
    assert w.find("run-x") is None


def test_a_failed_git_remove_is_retried_from_quiet(w, monkeypatch):
    path, _ = w.hc_live(w.clone, "muster/7", "run-x", state="done")
    argv = herdr_core.remove_argv(path, path, False)
    original = w.git

    def failing(where, args):
        if args[:2] == ["worktree", "remove"]:
            raise core.CommandError("git worktree remove: exit 1")
        return original(where, args)
    monkeypatch.setattr(w, "git", failing)
    with pytest.raises(core.CommandError):
        w.handle(argv)
    monkeypatch.setattr(w, "git", original)
    assert path in w.worktrees
    assert herdr_core.quiet(path) == []
    w.handle(herdr_core.remove_argv(path, path, False))
    assert path not in w.worktrees


def analysis(w, name="ana"):
    folder = w.clone / name
    folder.mkdir()
    return str(folder), herdr_core.open_workspace(str(folder), "l")


def test_open_workspace_starts_a_named_shell_in_the_folders_space(w):
    cwd, rec = analysis(w)
    assert rec["terminal"] == rec["pane"]
    assert re.fullmatch(r"muster-[0-9a-f]{8}", rec["pane"])
    assert rec["workspace"] == cwd
    t = w.find(rec["pane"])
    assert t["labels"]["muster.path"] == cwd
    assert t["labels"]["herdr.space"] in w.spaces


def test_two_opens_of_one_folder_are_two_terminals_in_one_space(w):
    cwd, first = analysis(w)
    second = herdr_core.open_workspace(cwd, "l")
    assert first["pane"] != second["pane"]
    assert len(w.spaces) == 1
    spaces = {w.find(r["pane"])["labels"]["herdr.space"] for r in (first, second)}
    assert len(spaces) == 1


def test_analysis_gone_after_the_terminal_is_killed(w):
    _, rec = analysis(w)
    assert not herdr_core.analysis_gone(rec)
    herdr_core.analysis_check(rec)
    w.handle(["hc", "kill", rec["pane"], "--json"])
    assert herdr_core.analysis_gone(rec)
    with pytest.raises(NotReady):
        herdr_core.analysis_check(rec)


def test_analysis_check_refuses_a_terminal_relabelled_elsewhere(w):
    _, rec = analysis(w)
    w.handle(["hc", "label", rec["pane"], "muster.path=/elsewhere", "--json"])
    with pytest.raises(NotReady):
        herdr_core.analysis_check(rec)


def test_close_ends_only_musters_terminal(w):
    cwd, rec = analysis(w)
    (sid,) = w.spaces
    shell = [t for t in w.hc.values() if t["labels"].get("herdr.space") == sid and t["name"] != rec["pane"]]
    w.handle(herdr_core.close_argv(rec))
    assert sid in w.spaces
    assert herdr_core.get(rec["pane"]) is None
    assert all(t["state"] == "live" for t in shell)
