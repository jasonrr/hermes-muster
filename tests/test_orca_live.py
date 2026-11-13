"""Opt-in check against the real Orca app (ORCA_LIVE=1): the shapes muster/hosts/orca.py relies on.

Creates throwaway worktrees under ~/orca/workspaces and starts `claude --model haiku` in an untrusted scratch clone.
The CLI has no `repo rm`, so each test leaves its scratch clone registered in Orca; the worktrees are removed.
"""

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
from muster import core
from muster.hosts import orca

ORCA_BIN = "/Applications/Orca.app/Contents/Resources/bin"

pytestmark = pytest.mark.skipif(
    os.environ.get("ORCA_LIVE") != "1" or not (shutil.which("orca") or Path(ORCA_BIN, "orca").exists()),
    reason="Opt-in live Orca run; set ORCA_LIVE=1 with Orca running",
)


def git(*argv, cwd):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path, monkeypatch):
    monkeypatch.setattr(orca, "ROOT", "~/orca/workspaces")  # Orca decides the real path
    core.prepare_env()
    git("init", "-q", "--bare", "origin.git", cwd=tmp_path)
    # a unique directory name: Orca's worktrees go to ~/orca/workspaces/<clone dir name>/
    path = tmp_path / f"muster-live-{secrets.token_hex(3)}"
    git("clone", "-q", "origin.git", str(path), cwd=tmp_path)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    git("push", "-q", "origin", "HEAD:main", cwd=path)
    git("remote", "set-head", "origin", "main", cwd=path)
    return path


def remove(clone, made):
    try:
        core.run(orca.remove_argv(made["workspace"], made["path"], True))
    finally:
        shutil.rmtree(made["path"], ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=clone, capture_output=True)


def test_live_new_branch_start_and_remove(clone):
    made = None
    p = None
    try:
        made = orca.create_worktree(clone, "muster-live/1", "main", orca.worktree_path(clone, "muster-live/1"), "l")
        name = f"muster-live-{secrets.token_hex(3)}"
        p = orca.open_tab(made["workspace"], made["path"], "l", [], name)
        orca.start_agent(name, "claude", p, ["--model", "haiku"], [])
        # a fresh clone is untrusted: Claude waits on the trust dialog
        assert orca.agent(p)["agent_status"] == "blocked"
    finally:
        if made:
            remove(clone, made)
        # open questions, recorded not asserted
        if p:
            try:
                print("LIVE (a) orca.pane after removal:", repr(orca.pane(p)))
            except Exception as error:
                print("LIVE (a) orca.pane after removal raised:", type(error).__name__, str(error))
        proc = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn"], capture_output=True, text=True)
        print(f"LIVE (b) lsof returncode={proc.returncode} stderr_nonempty={bool(proc.stderr.strip())} "
              f"stderr={proc.stderr.strip()[:200]!r}")
    assert made["path"] not in git("worktree", "list", cwd=clone)


def test_live_existing_branch_keeps_its_name(clone):
    made = None
    try:
        git("checkout", "-q", "-b", "muster-live/2", cwd=clone)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "work", cwd=clone)
        git("checkout", "-q", "main", cwd=clone)
        made = orca.create_worktree(clone, "muster-live/2", "main", orca.worktree_path(clone, "muster-live/2"), "l")
        assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=made["path"]) == "muster-live/2"
    finally:
        if made:
            remove(clone, made)
