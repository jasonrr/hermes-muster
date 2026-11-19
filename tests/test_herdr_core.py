import json

import pytest

import muster.core as core
import muster.hosts.herdr_core as herdr_core
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
