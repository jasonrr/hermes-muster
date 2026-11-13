"""herdr: muster's runs in herdr (>= 0.9.3): a trusted worktree, a tab, Claude started empty, then the brief.

The two-phase protocol and every argv here are herdr's (agent-automation.mdx); muster.launcher's header explains
why. Moved from launcher.py and cleanup.py unchanged when the host seam came in.
"""

import hashlib
import json
from pathlib import Path

from .. import core
from . import READY, Blocked, NotReady, QUIET, helper, processes, under

NAME = "herdr"


def result(*args):
    return json.loads(core.run(["herdr", *args]))["result"]


def worktree_path(clone, branch):
    """herdr's own worktree layout (<worktrees.directory>/<repo>/<branch-slug>); passed as --path so a launch
    knows its checkout before it exists."""
    return str(core.worktrees_dir() / Path(clone).name / branch.replace("/", "-"))


def worktrees(clone):
    return result("worktree", "list", "--cwd", str(clone))["worktrees"]


def create_worktree(clone, branch, base, path, label):
    made = result("worktree", "create", "--cwd", clone, "--branch", branch, "--base", f"origin/{base}",
                  "--path", path, "--label", label, "--no-focus", "--trust-repository")
    return {"workspace": made["workspace"]["workspace_id"], "path": made["worktree"]["path"]}


def open_worktree(clone, path, label):
    return result("worktree", "open", "--cwd", clone, "--path", path, "--label", label, "--no-focus",
                  "--trust-repository")["workspace"]["workspace_id"]


def panes(workspace):
    return result("pane", "list", "--workspace", workspace)["panes"]


def pane(pane_id):
    try:
        return result("pane", "get", pane_id)["pane"]
    except core.CommandError as error:
        if "pane_not_found" in str(error):
            return None
        raise


def open_tab(workspace, path, label, env, title):
    """A fresh shell tab, not the layout's own claude pane: its start races any scripted exit. title: unused
    (herdr names the agent at start)."""
    argv = ["herdr", "tab", "create", "--workspace", workspace, "--cwd", path, "--label", label]
    for pair in env:
        argv += ["--env", pair]
    return json.loads(core.run([*argv, "--no-focus"]))["result"]["root_pane"]["pane_id"]


def agent(target):
    """herdr's agent for a pane or name, or None when herdr says there is none. Raises when herdr fails."""
    try:
        return result("agent", "get", target)["agent"]
    except core.CommandError as error:
        if "agent_not_found" in str(error) or "pane_not_found" in str(error):
            return None
        raise


def start_agent(name, kind, pane_id, args, env):
    """env: unused (the tab already carries it). herdr returns once Claude is idle-ready."""
    core.run(["herdr", "agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "120000", "--", *args])


def prompt(pane_id, text):
    try:
        got = result("agent", "prompt", pane_id, text, "--wait", "--until", "working", "--until", "blocked",
                     "--timeout", "30000")
    except core.CommandError as error:
        if "agent_blocked" in str(error):  # refused before any input was sent (agent-automation.mdx)
            raise Blocked(str(error)) from None
        raise
    return (got.get("agent") or {}).get("agent_status")


def screen(pane_id):
    return core.run(["herdr", "pane", "read", pane_id, "--source", "visible"])


def quiet(workspace):
    """Each pane's evidence. NotReady when a pane is busy."""
    listed = panes(workspace)
    if not listed or any(p.get("workspace_id") != workspace for p in listed):
        raise NotReady(f"workspace {workspace} lists no panes, or another workspace's")
    table = processes()
    seen = []
    for entry in sorted(listed, key=lambda p: p["pane_id"]):
        pid = entry["pane_id"]
        info = result("pane", "process-info", "--pane", pid)["process_info"]
        procs = info["foreground_processes"]
        # A job started with & or an agent's background command: anything under the shell that is
        # neither the pane's foreground program nor a helper. A job detached with nohup or setsid
        # leaves this tree, and closing the pane does not stop it either.
        if info.get("shell_pid") not in table:
            raise NotReady(f"pane {pid}: its shell {info.get('shell_pid')} is not running")
        tree = under(info["shell_pid"], table)
        foreground = {p.get("pid") for p in procs}
        busy = [c for p, depth, c in tree if not (depth == 1 and p in foreground) and not helper(c)]
        if busy:
            raise NotReady(f"pane {pid} runs in the background: {busy[0][:80]}")
        if entry.get("agent"):
            found = result("agent", "get", pid)["agent"]
            if found.get("agent_status") not in READY:
                raise NotReady(f"pane {pid}: {found.get('agent')} is {found.get('agent_status')}")
            state = [found.get("agent"), found.get("agent_status"), found.get("state_change_seq")]
        else:
            busy = sorted({str(p.get("name")) for p in procs} - QUIET)
            if busy:
                raise NotReady(f"pane {pid} runs {', '.join(busy)}")
            state = None
        shot = hashlib.sha256(screen(pid).encode())
        seen.append([pid, entry.get("terminal_id"), state,
                     sorted(json.dumps([p.get("pid"), p.get("cmdline")]) for p in procs), shot.hexdigest(),
                     [p for p, _, _ in tree]])
    return seen


def remove_argv(workspace, path, force):
    return ["herdr", "worktree", "remove", "--workspace", workspace, *(["--force"] if force else [])]


def open_workspace(cwd, label):
    made = result("workspace", "create", "--cwd", cwd, "--label", label, "--no-focus")
    return {"workspace": made["workspace"]["workspace_id"], "terminal": made["root_pane"]["terminal_id"],
            "pane": made["root_pane"]["pane_id"]}


def analysis_gone(rec):
    """Closed by hand; herdr may hand its id to another."""
    return rec["workspace"] not in {w["workspace_id"] for w in result("workspace", "list")["workspaces"]}


def analysis_check(rec):
    workspace = rec["workspace"]
    if result("workspace", "get", workspace)["workspace"].get("label") != rec["label"]:
        raise NotReady(f"workspace {workspace} is no longer labelled {rec['label']}")
    terminals = {p.get("terminal_id") for p in panes(workspace)}
    if rec["terminal"] not in terminals:
        raise NotReady(f"workspace {workspace} no longer has terminal {rec['terminal']}")


def close_argv(rec):
    return ["herdr", "workspace", "close", rec["workspace"]]
