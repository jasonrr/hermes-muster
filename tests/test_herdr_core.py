import pytest

import muster.core as core
import muster.hosts.herdr_core as herdr_core
from tests.fake_orca import OrcaWorld


class LocalWorld(OrcaWorld):
    def git(self, where, args):
        if args[:4] == ["worktree", "add", "--no-track", "-b"]:
            self.worktree(where, args[4], args[5])
            return ""
        return super().git(where, args)


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = LocalWorld(tmp_path)   # Task 2: HcWorld

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


def calls(w, *verb):
    return [c for c in w.calls if isinstance(c, list) and c[:len(verb)] == list(verb)]


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
