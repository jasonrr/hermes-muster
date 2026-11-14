"""Opt-in check against the real Orca app (ORCA_LIVE=1): the shapes muster/hosts/orca.py relies on.

Creates throwaway worktrees under ~/orca/workspaces and starts `claude --model haiku` in them. Like a real run, they
are worktrees of a repo Claude already trusts: one scratch clone at a fixed path (CLONE), trusted once through Claude's
own dialog, so its worktrees inherit the trust. The fixed path also keeps Orca's sidebar at one `muster-live` project
(the CLI has no `repo rm`). The worktrees are removed. The trust-dialog -> blocked mapping is the fake's
(test_an_untrusted_checkout_stops_as_readiness): Orca does not reliably report an agent at that dialog.
"""

import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from muster import core
from muster.hosts import orca

ORCA_BIN = "/Applications/Orca.app/Contents/Resources/bin"
CLONE = Path("~/.cache/muster-orca-live/muster-live").expanduser()  # worktrees: ~/orca/workspaces/muster-live/

pytestmark = pytest.mark.skipif(
    os.environ.get("ORCA_LIVE") != "1" or not (shutil.which("orca") or Path(ORCA_BIN, "orca").exists()),
    reason="Opt-in live Orca run; set ORCA_LIVE=1 with Orca running",
)


def git(*argv, cwd):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def screen(handle):
    return "\n".join(orca.result("terminal", "read", "--terminal", handle, "--screen")["terminal"]["tail"])


def trust(clone):
    """Start Claude in the clone's own checkout and answer its trust dialog once; Claude keeps the answer."""
    p = orca.open_tab(orca.open_worktree(clone, str(clone), "l"), str(clone), "l", [], "muster-live-trust")
    handle = orca.live(p)["handle"]
    try:
        orca.result("terminal", "send", "--terminal", handle, "--text", "claude --model haiku", "--enter")
        deadline = time.time() + orca.START_WAIT
        while time.time() < deadline:
            if (orca.agent(p) or {}).get("agent_status") in orca.READY:
                return
            shown = screen(handle)
            if "❯ Yes, I trust this folder" in shown:
                orca.result("terminal", "send", "--terminal", handle, "--enter")
            elif "Yes, I trust this folder" in shown:  # the menu starts on "No, exit"
                orca.result("terminal", "send", "--terminal", handle, "--text", "\x1b[B")  # Down
            time.sleep(2)
        raise AssertionError(f"Claude did not start in {clone}:\n{screen(handle)}")
    finally:
        orca.result("terminal", "close", "--terminal", handle)


@pytest.fixture(scope="module")
def trusted():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(orca, "ROOT", "~/orca/workspaces")  # Orca decides the real path
        core.prepare_env()
        if not CLONE.exists():
            CLONE.parent.mkdir(parents=True, exist_ok=True)
            git("init", "-q", "--bare", "origin.git", cwd=CLONE.parent)
            git("clone", "-q", "origin.git", str(CLONE), cwd=CLONE.parent)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=CLONE)
            git("push", "-q", "origin", "HEAD:main", cwd=CLONE)
            git("remote", "set-head", "origin", "main", cwd=CLONE)
        trust(CLONE)
        yield CLONE


@pytest.fixture
def clone(trusted, monkeypatch):
    """The trusted clone, reset in place: rebuilding a repo under a path Orca has registered confuses Orca."""
    monkeypatch.setattr(orca, "ROOT", "~/orca/workspaces")  # again: an autouse fixture points it at tmp_path per test
    git("checkout", "-q", "main", cwd=trusted)
    git("worktree", "prune", cwd=trusted)
    for branch in ("muster-live/1", "muster-live/2"):
        subprocess.run(["git", "branch", "-q", "-D", branch], cwd=trusted, capture_output=True)
    return trusted


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
        # a worktree of a trusted repo: no dialog, Claude starts idle
        assert orca.agent(p)["agent_status"] in orca.READY
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
        # as a launch does next: a terminal in a git-made checkout (open_tab makes it visible to Orca)
        assert orca.agent(orca.open_tab(made["workspace"], made["path"], "l", [], f"muster-live-{secrets.token_hex(3)}")) is None
    finally:
        if made:
            remove(clone, made)
