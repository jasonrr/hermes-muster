import json
import shlex

import pytest

import muster.core as core
import muster.hosts as hosts
import muster.hosts.orca as orca
from tests.fake_orca import OrcaWorld


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = OrcaWorld(tmp_path)

    def run(argv, env=None):
        out = w.handle(argv)
        if out is None:
            raise AssertionError(argv)
        return out
    monkeypatch.setattr(core, "run", run)
    monkeypatch.setattr(orca, "sleep", lambda s: None)
    w.clone = tmp_path / "clone"
    w.clone.mkdir()
    return w


def jump(monkeypatch, *times):
    """orca.clock returns these, then the last one forever."""
    seq = list(times)
    monkeypatch.setattr(orca, "clock", lambda: seq.pop(0) if len(seq) > 1 else seq[0])


def tab(w, title="run-x"):
    """A new Orca worktree with a titled shell: (workspace, pane)."""
    path = orca.worktree_path(w.clone, "muster/7")
    ws = orca.create_worktree(str(w.clone), "muster/7", "main", path, "l")["workspace"]
    return ws, orca.open_tab(ws, path, "l", [], title)


def calls(w, *prefix):
    return [c for c in w.calls if c[: len(prefix)] == list(prefix)]


def test_a_new_branch_gets_orcas_own_worktree(world):
    path = orca.worktree_path(world.clone, "muster/7")
    made = orca.create_worktree(str(world.clone), "muster/7", "main", path, "l")
    assert made["path"] == path
    assert made["workspace"] == f"{world.repos[str(world.clone)]}::{path}"
    assert calls(world, "orca", "repo", "add")


def test_an_existing_branch_gets_gits_checkout_and_open_tab_makes_it_visible(world):
    world.repos[str(world.clone)] = "repo9"
    world.branches[(str(world.clone), "muster/3")] = "sha_x"
    path = orca.worktree_path(world.clone, "muster/3")
    made = orca.create_worktree(str(world.clone), "muster/3", "main", path, "l")
    assert ["git", "-C", str(world.clone), "worktree", "add", path, "muster/3"] in world.calls
    assert not calls(world, "orca", "worktree", "create")
    assert world.visible == set()
    orca.open_tab(made["workspace"], path, "l", [], "run-x")
    assert world.visible == {"repo9"}


def test_worktrees_lists_git_checkouts_and_adds_nothing(world):
    assert all(w["open_workspace_id"] is None for w in orca.worktrees(str(world.clone)))
    assert not calls(world, "orca", "repo", "add")
    path = orca.worktree_path(world.clone, "muster/7")
    orca.create_worktree(str(world.clone), "muster/7", "main", path, "l")
    listed = orca.worktrees(str(world.clone))
    assert listed[0]["is_linked_worktree"] is False
    assert listed[1]["is_linked_worktree"] is True and listed[1]["branch"] == "muster/7"
    assert listed[1]["open_workspace_id"]


def test_open_tab_waits_for_the_title(world):
    world.title_late = 2
    _, p = tab(world)
    made = next(t for t in world.terminals.values() if t["paneTitle"])
    assert p.endswith(f"#{made['tabId']}:{made['leafId']}")
    assert not calls(world, "orca", "terminal", "set-pane-title")


def test_open_tab_sets_the_title_itself_when_orca_never_shows_it(world, monkeypatch):
    world.title_late = 10**6
    jump(monkeypatch, 0, 0, 100)
    tab(world)
    assert len(calls(world, "orca", "terminal", "set-pane-title")) == 1


def test_open_tab_reuses_a_titled_shell_left_by_a_crash(world):
    ws, p = tab(world)
    assert orca.open_tab(ws, "/unused", "l", [], "run-x") == p
    creates = [c for c in calls(world, "orca", "terminal", "create") if "run-x" in c]
    assert len(creates) == 1


def test_start_types_claude_with_its_env_and_waits_until_ready(world):
    world.boot = ["working"]
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, ["--model", "opus", "--settings", "/s"], ["HERMES_HOME=/h", "GH_TOKEN="])
    env = " ".join(map(shlex.quote, ["HERMES_HOME=/h", "GH_TOKEN="]))
    line = " ".join(map(shlex.quote, ["claude", "--model", "opus", "--settings", "/s"]))
    assert world.typed == [f"export {env}; {line}"]
    assert orca.agent(p)["agent_status"] == "done"


def test_start_that_never_gets_ready_raises(world, monkeypatch):
    world.agent_start = "none"
    _, p = tab(world)
    jump(monkeypatch, 0, 0, 0, 1000)
    with pytest.raises(core.CommandError):
        orca.start_agent("run-x", "claude", p, [], [])


def test_a_waiting_agent_reads_blocked_and_refuses_a_prompt(world):
    world.agent_start = "waiting"
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    assert orca.agent(p)["agent_status"] == "blocked"
    with pytest.raises(hosts.Blocked):
        orca.prompt(p, "hi")


def test_a_proven_prompt_is_delivered_even_if_orca_still_reads_done(world):
    world.after_turn = "done"
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    assert orca.prompt(p, "hi") == "working"


def test_a_prompt_without_turn_proof_is_unproven(world):
    world.send_mode = "accepted"
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    with pytest.raises(core.CommandError) as e:
        orca.prompt(p, "hi")
    assert type(e.value) is core.CommandError


def test_after_a_restart_the_pane_is_unknown_not_gone(world):
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    world.restart()
    assert orca.pane(p) is not None
    with pytest.raises(core.CommandError, match="detached"):
        orca.agent(p)
    with pytest.raises(core.CommandError, match="detached"):
        orca.prompt(p, "x")
    world.adopt()
    assert orca.agent(p)["agent_status"] == "done"


def test_an_agent_is_found_by_name_anywhere(world):
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    assert orca.agent("run-x")["pane_id"] == p


def test_two_terminals_with_one_title_is_an_error(world):
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    other = orca.worktree_path(world.clone, "muster/8")
    world.worktree(world.clone, "muster/8", other)
    world.terminal(world.wid(other), other, "run-x")
    with pytest.raises(core.CommandError, match="close the extra one"):
        orca.agent("run-x")


def test_an_unknown_orca_state_is_neither_ready_nor_delivered(world):
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    world.orca_agents[next(iter(world.orca_agents))]["state"] = "thinking"
    status = orca.agent(p)["agent_status"]
    assert status == "orca-thinking"
    assert status not in hosts.READY and status not in hosts.DELIVERED


def test_an_orphan_elsewhere_does_not_stall_a_name_lookup(world):
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    world.restart()
    assert orca.agent("run-y") is None


def test_a_delivered_prompt_stays_delivered_if_the_state_read_fails(world, monkeypatch):
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    real = orca.agent

    def agent(target):
        if world.submitted:
            raise core.CommandError("orca: flaky")
        return real(target)
    monkeypatch.setattr(orca, "agent", agent)
    assert orca.prompt(p, "hi") == "working"


def test_a_prompt_into_a_bare_shell_sends_nothing(world):
    _, p = tab(world)
    with pytest.raises(core.CommandError, match="nothing sent"):
        orca.prompt(p, "hi")
    assert not calls(world, "orca", "terminal", "send")


def test_open_worktree_is_the_workspace_id(world):
    world.repos[str(world.clone)] = "repo9"
    assert orca.open_worktree(str(world.clone), "/p", "l") == "repo9::/p"


def test_agent_by_key_keeps_the_keys_title_when_claude_retitles_the_terminal(world):
    _, p = tab(world)
    orca.start_agent("run-x", "claude", p, [], [])
    next(t for t in world.terminals.values() if t["paneTitle"] == "run-x")["paneTitle"] = "Claude: fixing a bug"
    assert orca.agent(p)["name"] == "run-x"


def test_remove_argv_names_the_checkout_by_path():
    assert orca.remove_argv("rid::/p", "/p", True) == [
        "orca", "worktree", "rm", "--worktree", "path:/p", "--force", "--json"]


def quiet_world(w):
    path, h = w.orca_live(w.clone, "muster/7", "run-x")
    return path, h, w.wid(path), w.terminals[h]["shell"], w.terminals[h]["agent_pid"]


def test_an_idle_checkout_is_quiet_with_a_stable_fingerprint(world):
    _, _, ws, _, _ = quiet_world(world)
    first = orca.quiet(ws)
    assert first == orca.quiet(ws)
    json.dumps(first)


def test_a_working_agent_is_busy(world):
    _, h, ws, _, _ = quiet_world(world)
    world.orca_agents[h]["state"] = "working"
    with pytest.raises(hosts.NotReady, match="is working"):
        orca.quiet(ws)


def test_a_job_under_a_shell_is_busy(world):
    _, _, ws, shell, _ = quiet_world(world)
    world.procs[9001] = (shell, "npm run dev")
    with pytest.raises(hosts.NotReady, match="npm run dev"):
        orca.quiet(ws)


def test_an_agents_mcp_server_is_quiet_and_its_command_is_not(world):
    _, _, ws, _, agent_pid = quiet_world(world)
    world.procs[9002] = (agent_pid, "node tally-mcp")
    orca.quiet(ws)
    world.procs[9003] = (agent_pid, "pytest -q")
    with pytest.raises(hosts.NotReady, match="pytest"):
        orca.quiet(ws)


def test_a_detached_job_in_the_checkout_is_busy(world):
    path, _, ws, _, _ = quiet_world(world)
    world.cwds[9100] = path
    world.procs[9100] = (1, "python server.py")
    with pytest.raises(hosts.NotReady, match="outside any terminal"):
        orca.quiet(ws)


def test_a_shell_that_left_the_checkout_is_busy(world):
    path, _, ws, _, _ = quiet_world(world)
    h2 = world.terminal(ws, path, None)
    world.cwds[world.terminals[h2]["shell"]] = "/elsewhere"
    with pytest.raises(hosts.NotReady, match="left the checkout"):
        orca.quiet(ws)


def test_a_claude_orca_cannot_see_is_busy(world):
    _, h, ws, _, _ = quiet_world(world)
    world.orca_agents.pop(h)
    with pytest.raises(hosts.NotReady, match="shows no state"):
        orca.quiet(ws)


def test_an_orphaned_terminal_is_busy(world):
    _, _, ws, _, _ = quiet_world(world)
    world.restart()
    with pytest.raises(hosts.NotReady, match="detached"):
        orca.quiet(ws)


def test_open_workspace_adds_the_folder_once_and_titles_its_terminal(world, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    rec = orca.open_workspace(str(folder), "deal review")
    assert calls(world, "orca", "repo", "add") == [["orca", "repo", "add", "--path", str(folder), "--kind", "folder",
                                                    "--json"]]
    assert calls(world, "orca", "terminal", "create")
    assert orca.pane(rec["pane"]) is not None
    orca.open_workspace(str(folder), "deal review")
    assert len(calls(world, "orca", "repo", "add")) == 1


def test_analysis_close_and_gone(world, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    rec = orca.open_workspace(str(folder), "deal review")
    world.handle(orca.close_argv(rec))
    assert orca.analysis_gone(rec)
    with pytest.raises(hosts.NotReady):
        orca.close_argv(rec)


def test_an_extra_login_shell_in_the_checkout_is_busy(world):
    path, _, ws, _, _ = quiet_world(world)
    world.procs[9200] = (1, "/usr/bin/login -flpq u /bin/zsh -l")
    world.procs[9201] = (9200, "-/bin/zsh -l")
    world.cwds[9201] = path
    with pytest.raises(hosts.NotReady, match="shell from outside"):
        orca.quiet(ws)


def test_a_detached_shell_script_in_the_checkout_is_busy(world):
    path, _, ws, _, _ = quiet_world(world)
    world.cwds[9300] = path
    world.procs[9300] = (1, "bash deploy.sh")
    with pytest.raises(hosts.NotReady, match="outside any terminal"):
        orca.quiet(ws)


def test_cwd_pids_match_the_resolved_path(world, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    world.cwds[9400] = str(real.resolve())
    assert 9400 in orca.cwd_pids(str(link))


def test_analysis_check_rejects_a_pane_from_another_workspace(world, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    rec = orca.open_workspace(str(folder), "deal review")
    orca.analysis_check(rec)
    with pytest.raises(hosts.NotReady, match="belongs to"):
        orca.analysis_check({**rec, "workspace": "repo99::/elsewhere"})


def test_open_workspace_adds_a_checkout_as_a_repo_not_a_folder(world, tmp_path):
    clone = tmp_path / "other"
    (clone / ".git").mkdir(parents=True)
    orca.open_workspace(str(clone), "review")
    assert calls(world, "orca", "repo", "add") == [["orca", "repo", "add", "--path", str(clone), "--json"]]
