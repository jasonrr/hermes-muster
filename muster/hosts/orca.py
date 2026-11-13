"""orca: muster's runs in Orca: a checkout, a terminal titled with the agent's name, Claude typed into it.

Every shape here was checked live on 2026-11-12 (docs/plans/2026-11-12-orca-host.md, "Live check"). Orca's terminal
handles go stale when Orca restarts, and its rows come back orphaned (no title, no agent, recordedPaneKey = the old
tab:leaf), so muster's pane is its own durable key, "orca:<worktree id>#<title>#<tab id>:<leaf id>", resolved on
every call. An orphaned match is unknown, never gone. Orca's agent detection rides on its wrapper around `claude`
in its shells, so Claude is typed into a shell, never exec'd with `env`.
"""

import hashlib
import json
import os
import secrets
import shlex
import time
from pathlib import Path

from .. import core
from . import QUIET, READY, Blocked, NotReady, helper, processes, program, under

NAME = "orca"
ROOT = "~/orca/workspaces"  # Orca's default workspace directory: <ROOT>/<clone dir name>/<worktree name>
# Orca's agent states (live check) -> muster's. Anything else passes through prefixed: never ready, never delivered.
STATE = {"working": "working", "waiting": "blocked", "done": "done", "idle": "idle"}
TITLE_WAIT = 5    # s a new terminal takes to show its pane title (about 1 s observed)
START_WAIT = 120  # s Claude may take to be ready (herdr's start --timeout 120000)
clock, sleep = time.time, time.sleep  # tests replace both


def result(*args):
    return json.loads(core.run(["orca", *args, "--json"]))["result"]


def known_repo(clone):
    """Orca's id for a clone or folder, or None when Orca does not know it (nothing is added)."""
    try:
        return result("repo", "show", "--repo", f"path:{clone}")["repo"]["id"]
    except core.CommandError as error:
        if "repo_not_found" not in str(error):
            raise
    return None


def repo_id(clone):
    """Orca's id for a clone or folder, adding it to Orca the first time."""
    return known_repo(clone) or result("repo", "add", "--path", str(clone))["repo"]["id"]


def worktree_path(clone, branch):
    """Where Orca puts a worktree named after the branch; git's checkout for an existing branch goes there too."""
    return str(Path(ROOT).expanduser() / Path(clone).name / branch.replace("/", "-"))


def worktrees(clone):
    """Every git worktree of the clone, from git: Orca's own list leaves out checkouts it did not create. Read-only:
    a clone Orca does not know yet has no workspace open (open_workspace_id None)."""
    rid = known_repo(clone)
    found, entry = [], None
    for line in core.run(["git", "-C", str(clone), "worktree", "list", "--porcelain"]).splitlines() + [""]:
        if line.startswith("worktree "):
            entry = {"path": line[len("worktree "):], "branch": None}
        elif line.startswith("branch ") and entry:
            entry["branch"] = line[len("branch "):].removeprefix("refs/heads/")
        elif not line and entry:
            found.append(entry)
            entry = None
    for n, wt in enumerate(found):
        wt["is_linked_worktree"] = n > 0  # git lists the main checkout first
        wt["open_workspace_id"] = f"{rid}::{wt['path']}" if rid else None
    return found


def create_worktree(clone, branch, base, path, label):
    """Orca's own create for a new branch. An existing branch gets git's checkout at the same place, since Orca
    renames a branch with commits of its own (muster/3 -> muster/3-2); the repo then shows external worktrees, so
    the checkout is visible in Orca. label: unused (Orca names the worktree by its directory). Setup hooks are
    skipped: builders own dependency setup (#69)."""
    rid = repo_id(clone)
    try:
        core.run(["git", "-C", str(clone), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    except core.CommandError:
        made = result("worktree", "create", "--repo", f"id:{rid}", "--name", Path(path).name, "--branch", branch,
                      "--base-branch", f"origin/{base}", "--setup", "skip", "--no-parent")["worktree"]
        return {"workspace": made["id"], "path": made["path"]}
    core.run(["git", "-C", str(clone), "worktree", "add", path, branch])
    result("repo", "set", "--repo", f"id:{rid}", "--external-worktree-visibility", "show")
    return {"workspace": f"{rid}::{path}", "path": path}


def open_worktree(clone, path, label):
    """Orca needs no open: a checkout's workspace is its id. A checkout git made outside Orca is visible to Orca
    (terminal create, worktree ps) only while the repo shows external worktrees, so that is set (idempotent)."""
    rid = repo_id(clone)
    result("repo", "set", "--repo", f"id:{rid}", "--external-worktree-visibility", "show")
    return f"{rid}::{path}"


def key(workspace, title, pane_key):
    return f"orca:{workspace}#{title}#{pane_key}"


def parse(pane):
    """(workspace, title, tab:leaf) of a pane key; the workspace may hold '#', the other two never do."""
    workspace, title, pane_key = pane.removeprefix("orca:").rsplit("#", 2)
    return workspace, title, pane_key


def terminals(workspace=None):
    """Orca's terminal rows, orphaned ones included."""
    rows = result("terminal", "list", *(["--worktree", f"id:{workspace}"] if workspace else []))["terminals"]
    return [t for t in rows if workspace is None or t.get("worktreeId") == workspace]


def agents(workspace):
    """tab:leaf -> Orca's agent row, for the workspace's agents that a window shows."""
    for w in result("worktree", "ps", "--limit", "10000")["worktrees"]:
        if w.get("worktreeId") == workspace:
            return {a["paneKey"]: a for a in w.get("agents") or [] if a.get("paneKey")}
    return {}


def own(row):
    return f"{row['tabId']}:{row['leafId']}"


def find(pane):
    """Orca's terminal row for a pane key (orphaned or not), or None when no terminal matches it."""
    workspace, title, pane_key = parse(pane)
    rows = [t for t in terminals(workspace)
            if own(t) == pane_key or t.get("recordedPaneKey") == pane_key
            or (t.get("paneTitle") == title and not t.get("orphaned"))]
    if len({t["handle"] for t in rows}) > 1:
        raise core.CommandError(f"orca: {len(rows)} terminals in {workspace} match {title}; close the extra one")
    return rows[0] if rows else None


def live(pane):
    """The pane's row when a window shows it. Raises (unknown, try later) when it is orphaned or gone."""
    row = find(pane)
    if row is None:
        raise core.CommandError(f"orca: no terminal for {pane}")
    if row.get("orphaned"):
        raise core.CommandError(f"orca: the terminal for {pane} is detached from Orca's window (Orca restarted?); "
                                f"open its workspace in Orca")
    return row


def pane(pane_id):
    row = find(pane_id)
    return {"pane_id": pane_id, "workspace_id": row["worktreeId"]} if row else None


def panes(workspace):
    showing = agents(workspace)
    return [{"pane_id": key(workspace, t.get("paneTitle") or t["handle"], own(t)), "workspace_id": workspace,
             "terminal_id": t["ptyId"],
             "agent": (showing.get(own(t)) or {}).get("agentType") or ("unknown" if t.get("orphaned") else None)}
            for t in terminals(workspace)]


def open_tab(workspace, path, label, env, title):
    """A shell titled with the agent's name. A titled shell left by a crash after create is reused; env is applied
    when Claude starts (start_agent). path, label: unused (the workspace is the checkout)."""
    for t in terminals(workspace):
        if t.get("paneTitle") == title and not t.get("orphaned"):
            return key(workspace, title, own(t))
    made = result("terminal", "create", "--worktree", f"id:{workspace}", "--title", title)["terminal"]
    pane_id = key(workspace, title, made["paneKey"])
    deadline = clock() + TITLE_WAIT
    while (find(pane_id) or {}).get("paneTitle") != title:
        if clock() >= deadline:
            result("terminal", "set-pane-title", "--terminal", made["handle"], "--title", title)
            break
        sleep(0.5)
    return pane_id


def agent(target):
    """The agent in a pane (a key) or under a name (a pane title anywhere), in herdr's shape; None when there is
    none. Raises when Orca cannot tell (an orphaned terminal). A pane key's name is the title in the key, which
    Claude's own terminal title updates cannot change."""
    if target.startswith("orca:"):
        row = find(target)
    else:
        # ponytail: an orphaned terminal has no title, so a name lookup cannot see it. Every fresh launch looks its
        # name up, so raising on any orphan would stall all launches after an Orca restart. The miss only matters
        # after a crash between open_tab and its save AND an Orca restart; record the workspace with the name if
        # that ever happens.
        rows = [t for t in terminals() if t.get("paneTitle") == target and not t.get("orphaned")]
        if len(rows) > 1:
            raise core.CommandError(f"orca: {len(rows)} terminals in Orca are titled {target}; close the extra one")
        row = rows[0] if rows else None
    if row is None:
        return None
    if row.get("orphaned"):
        live(target)  # raises: unknown
    found = agents(row["worktreeId"]).get(own(row))
    if not found:
        return None
    state = found.get("state")
    workspace = row["worktreeId"]
    pane_id = target if target.startswith("orca:") else key(workspace, row.get("paneTitle"), own(row))
    return {"agent": found.get("agentType"), "name": parse(pane_id)[1], "cwd": row.get("worktreePath"),
            "foreground_cwd": None, "agent_status": STATE.get(state, f"orca-{state}"),
            "state_change_seq": found.get("stateStartedAt"), "completion_seq": None,
            "pane_id": pane_id, "workspace_id": workspace}


def start_agent(name, kind, pane_id, args, env):
    """Type `export <env>; <kind> <args>` into the pane's shell, then wait until Claude is ready or shows a dialog
    (herdr's start contract)."""
    # ponytail: a crash after typing and before the agent shows (up to START_WAIT) leaves recovery typing the line
    # again, into Claude this time; it then reads busy and a person recovers. Check the shell's children first if
    # that ever happens.
    row = live(pane_id)
    line = " ".join(shlex.quote(a) for a in [kind, *args])
    if env:
        line = "export " + " ".join(shlex.quote(pair) for pair in env) + "; " + line
    result("terminal", "send", "--terminal", row["handle"], "--text", line, "--enter")
    deadline = clock() + START_WAIT
    while clock() < deadline:
        now = agent(pane_id)
        if now and now["agent_status"] in (*READY, "blocked"):
            return
        sleep(1)
    raise core.CommandError(f"orca: {kind} was not ready in {pane_id} within {START_WAIT} s")


def prompt(pane_id, text):
    """One prompt with Orca's proof: `turn_started` is delivered (working, or blocked when a dialog is already up);
    agent_prompt_blocked sent nothing; anything else is unproven. A pane with no agent (Claude exited, a bare
    shell) gets nothing typed. Once the turn is proven, a failed state read does not undo the delivery."""
    row = live(pane_id)
    if agent(pane_id) is None:
        raise core.CommandError(f"orca: no agent in {pane_id}; nothing sent")
    try:
        sent = result("terminal", "send", "--terminal", row["handle"], "--text", text, "--enter",
                      "--wait-submit", "30")["send"]
    except core.CommandError as error:
        if "agent_prompt_blocked" in str(error):
            raise Blocked(str(error)) from None
        raise
    if "turn_started" not in ((sent.get("prompt") or {}).get("stages") or []):
        raise core.CommandError(f"orca: the prompt to {pane_id} was accepted; no turn start was seen in 30 s")
    try:
        now = agent(pane_id)
    except core.CommandError:
        return "working"
    return "blocked" if now and now["agent_status"] == "blocked" else "working"


def screen(pane_id):
    row = live(pane_id)
    return "\n".join(result("terminal", "read", "--terminal", row["handle"], "--screen")["terminal"]["tail"])


def remove_argv(workspace, path, force):
    """Orca's rm takes Orca-made and git-made checkouts alike, closes their terminals and keeps the branch."""
    return ["orca", "worktree", "rm", "--worktree", f"path:{path}", *(["--force"] if force else []), "--json"]


def cwd_pids(path):
    """pids of processes whose working directory is the checkout or inside it."""
    found, pid, roots = set(), None, {path, os.path.realpath(path)}  # lsof prints resolved paths
    for line in core.run(["lsof", "-a", "-d", "cwd", "-Fpn"]).splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and any(line[1:] == r or line[1:].startswith(r + "/") for r in roots):
            found.add(pid)
    return found


# ponytail: an analysis folder's own terminals (not only muster's) count too, so a folder the human works in stays
# open; scope to muster's terminal if that bites.
def quiet(workspace):
    """Each terminal's evidence. NotReady when one is busy or unknown: an orphaned terminal (Orca shows no agent for
    it), an agent not idle or done, a Claude process Orca shows no agent for, anything under a terminal's shell but
    the agent, a nested shell or an MCP/language server, a process in the checkout outside any terminal's shell, or
    fewer shells in the checkout than Orca has terminals (one left it, so its jobs cannot be seen). Orca shows no
    shell pid per terminal, so shells are found by working directory (lsof)."""
    path = workspace.split("::", 1)[1]
    rows = terminals(workspace)
    if not rows:
        raise NotReady(f"workspace {workspace} lists no terminals")
    showing, seen = agents(workspace), []
    for t in sorted(rows, key=lambda t: t["ptyId"]):
        if t.get("orphaned"):
            raise NotReady(f"terminal {t['ptyId']} is detached from Orca's window; its agent cannot be seen")
        a = showing.get(own(t))
        status = STATE.get(a.get("state"), a.get("state")) if a else None
        if a and status not in READY:
            raise NotReady(f"terminal {t.get('paneTitle') or t['handle']}: {a.get('agentType')} is {status}")
        text = "\n".join(result("terminal", "read", "--terminal", t["handle"], "--screen")["terminal"]["tail"])
        seen.append([t["ptyId"], [a.get("agentType"), a.get("state"), a.get("stateStartedAt")] if a else None,
                     hashlib.sha256(text.encode()).hexdigest()])
    table, inside = processes(), cwd_pids(path)
    roots = sorted(p for p in inside if p in table and table[p][0] not in inside)
    # An Orca terminal's shell is a child of a /usr/bin/login wrapper (live check); ps shows it, lsof does not.
    # ponytail: an Orca shell that left and an outside login shell in the checkout at the same moment balance out;
    # attributing shells to terminals needs a shell pid from Orca's terminal list.
    odd = [p for p in roots if program(table[p][1]) not in QUIET or program(table.get(table[p][0], (0, ""))[1]) != "login"]
    if odd:
        raise NotReady(f"{path}: {table[odd[0]][1][:80]} runs outside any terminal's shell")
    if len(roots) < len(rows):
        raise NotReady(f"{path}: {len(rows)} terminals but {len(roots)} shells: a shell left the checkout")
    if len(roots) > len(rows):
        raise NotReady(f"{path}: {len(roots)} shells but {len(rows)} terminals: a shell from outside Orca is here")
    kind, agents_running = core.adapter().KIND, 0
    for shell in roots:
        tree = under(shell, table)
        for _, depth, command in tree:
            if depth == 1 and program(command) == kind:
                agents_running += 1  # judged by Orca's state above
            elif not (depth == 1 and program(command) in QUIET) and not helper(command):
                raise NotReady(f"{path}: {command[:80]} is running")
        seen.append([shell, [p for p, _, _ in tree]])
    if agents_running > len(showing):
        raise NotReady(f"{path}: {agents_running} {kind} processes but Orca shows no state for "
                       f"{agents_running - len(showing)} of them")
    return seen


def open_workspace(cwd, label):
    """A terminal muster owns in a folder, the folder added to Orca the first time. label: the record's; the pane
    title is unique per open, so two opens of one folder stay apart."""
    title = f"muster-{secrets.token_hex(4)}"
    try:
        made = result("terminal", "create", "--worktree", f"path:{cwd}", "--title", title)["terminal"]
    except core.CommandError as error:
        if "selector_not_found" not in str(error):
            raise
        result("repo", "add", "--path", cwd, "--kind", "folder")
        made = result("terminal", "create", "--worktree", f"path:{cwd}", "--title", title)["terminal"]
    return {"workspace": made["worktreeId"], "terminal": made["handle"],
            "pane": key(made["worktreeId"], title, made["paneKey"])}


def analysis_gone(rec):
    """Its terminal was closed by hand. An orphaned one is not gone. (Orca cannot remove a folder project from its
    CLI; the folder stays listed in Orca.)"""
    return find(rec["pane"]) is None


def analysis_check(rec):
    if parse(rec["pane"])[0] != rec["workspace"]:
        raise NotReady(f"terminal {rec['pane']} belongs to another workspace than {rec['workspace']}")
    if find(rec["pane"]) is None:
        raise NotReady(f"terminal {rec['pane']} is gone")


def close_argv(rec):
    """Close muster's own terminal, found again (its handle may be stale), never the folder's other terminals."""
    row = find(rec["pane"])
    if row is None:
        raise NotReady(f"terminal {rec['pane']} is gone")
    return ["orca", "terminal", "close", "--terminal", row["handle"], "--json"]
