import json

import pytest

import muster.cleanup as cleanup
import muster.config as config
import muster.core as core
from tests.fake_orca import OrcaWorld
from tests.test_cleanup import BASE, World, cleanup_state, main, tick, window  # noqa: F401 (the autouse fixture)


class OrcaCleanupWorld(World):
    """test_cleanup's World (git, gh api, hermes kanban, the issue-15 card) with the worktree living in Orca:
    orca, lsof, ps and `git worktree list --porcelain` go to an OrcaWorld that holds the same checkout."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.herdr = False  # a herdr-recorded worktree: ps is the parent's; discovery still runs through the configured orca
        self.orca = OrcaWorld(tmp_path / "orca")
        self.orca.repos[str(self.clone)] = "repo1"
        self.orca.worktrees[self.path] = {"branch": self.branch, "clone": str(self.clone),
                                          "git_dir": str(self.git_dir), "workspace": None}
        self.wid = f"repo1::{self.path}"
        self.handle = self.orca.terminal(self.wid, self.path, "run-15")
        self.orca.claude(self.handle, "done")
        self.orca.terminal(self.wid, self.path, None)  # the shell Orca opened with the worktree
        self.card.update(host="orca", workspace=self.wid)
        self._write_card()

    def run(self, argv, env=None):
        listing = argv[3:6] == ["worktree", "list", "--porcelain"]
        if listing or argv[0] == "orca" or (not self.herdr and argv[0] in ("lsof", "ps")):
            self.calls.append(argv)
            if argv[:3] == ["orca", "worktree", "rm"]:
                self.removed = True
            return self.orca.handle(argv)
        return super().run(argv, env)


def orca_removes(world):
    return [c for c in world.calls if c[:3] in (["orca", "worktree", "rm"], ["orca", "terminal", "close"])]


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "orca").mkdir()
    w = OrcaCleanupWorld(tmp_path)
    monkeypatch.setattr(core, "run", w.run)
    monkeypatch.setattr(core, "subscribe", lambda card, ledger=None: (w.subscribed.append(card),
                                                                      w.ledgers.append(ledger)))
    monkeypatch.setitem(config.settings, "host", "orca")
    t = [BASE]
    w.t = t
    monkeypatch.setattr(cleanup, "clock", lambda: t[0])
    return w


def test_a_merged_quiet_orca_worktree_is_removed_after_the_window(world):
    window(world)
    assert orca_removes(world) == [["orca", "worktree", "rm", "--worktree", f"path:{world.path}", "--json"]]


def test_a_busy_orca_worktree_is_kept(world, capsys):
    world.orca.procs[9001] = (world.orca.terminals[world.handle]["shell"], "npm run dev")
    window(world)
    assert orca_removes(world) == []
    assert "npm run dev" in capsys.readouterr().out


def test_a_restarted_orca_keeps_the_worktree(world, capsys):
    world.orca.restart()
    window(world)
    assert orca_removes(world) == []
    assert "detached" in capsys.readouterr().out


def test_a_herdr_worktree_is_still_cleaned_after_switching_to_orca(world):
    world.herdr = True
    world.card.pop("host")
    world.card["workspace"] = "wR"
    world._write_card()
    window(world)
    assert [c for c in world.calls if c[:3] == ["herdr", "worktree", "remove"]] == [
        ["herdr", "worktree", "remove", "--workspace", "wR"]]
    assert orca_removes(world) == []
    assert any(c[3:6] == ["worktree", "list", "--porcelain"] for c in world.calls)
    assert any(c[:3] == ["herdr", "worktree", "list"] for c in world.calls)


def test_an_orca_analysis_workspace_closes_only_its_terminal(world, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    assert main(["open", "--cwd", str(folder), "--label", "notes"]) == 0
    rec = json.loads(next((config.data_dir() / "workspaces").glob("*.json")).read_text())
    assert rec["host"] == "orca"
    world.orca.restart()
    world.orca.adopt()
    new = next(h for h, t in world.orca.terminals.items() if t["paneTitle"] is None and t["worktreePath"] == str(folder))
    window(world)
    assert orca_removes(world)[-1:] == [["orca", "terminal", "close", "--terminal", new, "--json"]]
