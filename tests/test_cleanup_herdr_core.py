import json

import pytest

import muster.cleanup as cleanup
import muster.config as config
import muster.core as core
from tests.fake_herdr_core import HcWorld
from tests.test_cleanup import BASE, World, cleanup_state, main, window  # noqa: F401 (the autouse fixture)


class HcCleanupWorld(World):
    """test_cleanup's World with the worktree in herdr-core: hc, herdr-app, sh, ps, `git worktree list --porcelain`
    and `git rev-parse --path-format=absolute --git-common-dir` go to an HcWorld that holds the same checkout."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.herdr = False  # a herdr-recorded worktree: ps is the parent's; discovery still runs through herdr-core
        self.inner = HcWorld(tmp_path / "hc")
        self.inner.worktrees[self.path] = {"branch": self.branch, "clone": str(self.clone),
                                           "git_dir": str(self.git_dir), "workspace": None}
        made = json.loads(self.inner.handle(["herdr-app", "do", "space.new", "--name", "x", "--folder", self.path]))
        self.address = self.inner.term("run-15", self.path, {"muster.path": self.path,
                                                             "herdr.space": made["space"]})
        self.inner.hc_claude(self.address, "done")
        self.card.update(host="herdr-core", workspace=self.path)
        self._write_card()

    def run(self, argv, env=None):
        to_inner = (argv[3:6] == ["worktree", "list", "--porcelain"]
                    or argv[3:] == ["rev-parse", "--path-format=absolute", "--git-common-dir"]
                    or argv[0] in ("hc", "herdr-app", "sh") or (not self.herdr and argv[0] == "ps"))
        if to_inner:
            self.calls.append(argv)
            if argv[:2] == ["sh", "-c"] and "worktree remove" in argv[2]:
                self.removed = True
            return self.inner.handle(argv)
        return super().run(argv, env)


def removes(world):
    return [c for c in world.calls if c[:2] == ["sh", "-c"]]


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "hc").mkdir()
    w = HcCleanupWorld(tmp_path)
    monkeypatch.setattr(core, "run", w.run)
    monkeypatch.setattr(core, "subscribe", lambda card, ledger=None: (w.subscribed.append(card),
                                                                      w.ledgers.append(ledger)))
    monkeypatch.setitem(config.settings, "host", "herdr-core")
    t = [BASE]
    w.t = t
    monkeypatch.setattr(cleanup, "clock", lambda: t[0])
    return w


def test_a_merged_quiet_worktree_is_removed_after_the_window(world):
    window(world)
    assert len(removes(world)) == 1
    assert world.inner.spaces == {}
    assert not [t for t in world.inner.hc.values()
                if t["state"] == "live" and t["labels"].get("muster.path") == world.path]
    assert world.path not in world.inner.worktrees


def test_a_working_agent_keeps_the_worktree(world):
    world.inner.hc[world.address]["status"]["state"] = "working"
    window(world)
    assert removes(world) == []


def test_a_herdr_worktree_is_still_cleaned_after_switching_to_herdr_core(world):
    world.herdr = True
    world.card.pop("host")
    world.card["workspace"] = "wR"
    world._write_card()
    window(world)
    assert [c for c in world.calls if c[:3] == ["herdr", "worktree", "remove"]] == [
        ["herdr", "worktree", "remove", "--workspace", "wR"]]
    assert removes(world) == []


def test_an_analysis_workspace_closes_only_its_terminal(world, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    assert main(["open", "--cwd", str(folder), "--label", "notes"]) == 0
    rec = json.loads(next((config.data_dir() / "workspaces").glob("*.json")).read_text())
    assert rec["host"] == "herdr-core"
    window(world)
    assert rec["pane"] in removes(world)[-1][2]
    assert rec["pane"] not in [t["name"] for t in world.inner.hc.values() if t["state"] == "live"]
    assert world.inner.spaces
    assert any(t["state"] == "live" and t["cwd"] == str(folder) for t in world.inner.hc.values())
