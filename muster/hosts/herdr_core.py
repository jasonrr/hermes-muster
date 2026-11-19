"""herdr-core: muster's runs in the Herdr desktop app's terminal core (`hc`): a git checkout, an hc shell named
after the agent, Claude typed into it, the brief sent with `hc send --wait`.

Every shape here was checked live on 2026-11-19 (docs/plans/2026-11-19-herdr-core-host.md, "Live check"). The
workspace is the checkout path and a pane is the hc terminal's name (the agent name): names are unique among live
terminals, so a live name is the identity and a taken name is the crash-recovery check. Every terminal muster
starts is labelled muster.path=<workspace>. A window shows a terminal only when it is labelled herdr.space=<id>;
with no window muster opens one in the background, and when none comes up the run proceeds hidden.
"""

import time

from .. import core
from . import git_worktrees, herdr

NAME = "herdr-core"
LABEL = "muster.path"
STATE = {"idle": "idle", "working": "working", "blocked": "blocked", "done": "done"}  # hc status.state -> muster's
APPS = {"claude-code": "claude"}  # hc status.app -> the agent kind muster's adapter names (claude.KIND)
START_WAIT = 120   # s Claude may take to be ready (herdr's start --timeout 120000)
WINDOW_WAIT = 30   # s a window muster opened may take to answer `herdr-app status`
SEND_WAIT = "10"   # s `hc send --wait` waits on the turn before it checks in; `started: true` is proof either way
clock, sleep = time.time, time.sleep  # tests replace both

# The app's own worktree layout is herdr's: <worktrees setting>/<repo>/<branch with / as ->.
worktree_path = herdr.worktree_path


def worktrees(clone):
    """Every git worktree of the clone. A checkout's workspace is its path: hc needs nothing opened."""
    return [{**wt, "open_workspace_id": wt["path"]} for wt in git_worktrees(clone)]


def create_worktree(clone, branch, base, path, label):
    """git's checkout, as the app's own worktree menu makes it (the launcher fetched origin/<base> first,
    launcher.py:226). An existing branch is checked out as it is. label: unused (open_tab names the space)."""
    try:
        core.run(["git", "-C", str(clone), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    except core.CommandError:
        core.run(["git", "-C", str(clone), "worktree", "add", "--no-track", "-b", branch, path, f"origin/{base}"])
    else:
        core.run(["git", "-C", str(clone), "worktree", "add", path, branch])
    return {"workspace": path, "path": path}


def open_worktree(clone, path, label):
    return path
