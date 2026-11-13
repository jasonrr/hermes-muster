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


def test_an_existing_branch_gets_gits_checkout_and_a_visible_repo(world):
    world.repos[str(world.clone)] = "repo9"
    world.branches[(str(world.clone), "muster/3")] = "sha_x"
    path = orca.worktree_path(world.clone, "muster/3")
    orca.create_worktree(str(world.clone), "muster/3", "main", path, "l")
    assert ["git", "-C", str(world.clone), "worktree", "add", path, "muster/3"] in world.calls
    assert not calls(world, "orca", "worktree", "create")
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


def test_remove_argv_names_the_checkout_by_path():
    assert orca.remove_argv("rid::/p", "/p", True) == [
        "orca", "worktree", "rm", "--worktree", "path:/p", "--force", "--json"]
