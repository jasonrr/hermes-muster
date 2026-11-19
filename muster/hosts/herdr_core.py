"""herdr-core: muster's runs in the Herdr desktop app's terminal core (`hc`): a git checkout, an hc shell named
after the agent, Claude typed into it, the brief sent with `hc send --wait`.

Every shape here was checked live on 2026-11-19 (docs/plans/2026-11-19-herdr-core-host.md, "Live check"). The
workspace is the checkout path and a pane is the hc terminal's name (the agent name): names are unique among live
terminals, so a live name is the identity and a taken name is the crash-recovery check. Every terminal muster
starts is labelled muster.path=<workspace>. A window shows a terminal only when it is labelled herdr.space=<id>;
with no window muster opens one in the background, and when none comes up the run proceeds hidden.
"""

import json
import shlex
import time
from pathlib import Path

from .. import core
from . import READY, Blocked, git_worktrees, herdr, program

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


def hc(*args):
    return json.loads(core.run(["hc", *args, "--json"]))


def send(pane_id, text, *opts):
    """`hc send` with the positionals after `--`, so a text starting with '-' is never read as a flag."""
    return json.loads(core.run(["hc", "send", *opts, "--json", "--", pane_id, text]))


def kind(error):
    """hc's error kind from a CommandError (hc prints {"error": {"kind": …}} on stderr; core.run keeps stderr)."""
    text = str(error)
    try:
        return json.loads(text[text.index("{"):])["error"]["kind"]
    except (ValueError, KeyError, TypeError):
        return None


def get(target):
    """hc's live terminal for a name or address, or None when there is none (unknown, or finished)."""
    try:
        t = hc("get", target)
    except core.CommandError as error:
        if kind(error) == "not_found":
            return None
        raise
    return t if t.get("state") == "live" else None


def up():
    """True when a Herdr window answers `herdr-app status` (exit 0 and JSON with no error)."""
    try:
        return "error" not in json.loads(core.run(["herdr-app", "status"]))
    except (core.CommandError, ValueError):
        return False


def window():
    """True when a Herdr window answers; with none, open one in the background and wait up to WINDOW_WAIT."""
    if up():
        return True
    try:
        core.run(["open", "-g", "-a", "Herdr"])
    except core.CommandError:
        return False
    deadline = clock() + WINDOW_WAIT
    while clock() < deadline:
        sleep(1)
        if up():
            return True
    return False


def space(path):
    """The window's space for a folder: the one already there, else a new one named after it. None when no window
    comes up or the window fails: visibility never fails a launch; the run proceeds hidden and the log says so."""
    try:
        if window():
            for s in json.loads(core.run(["herdr-app", "ls"]))["spaces"]:
                if s.get("folder") == path and not s.get("machine"):
                    return s["id"]
            return json.loads(core.run(["herdr-app", "do", "space.new", "--name", Path(path).name,
                                        "--folder", path]))["space"]
    except (core.CommandError, ValueError, KeyError) as error:
        core.log("hosts", f"herdr-core: the Herdr window failed for {path}: {error}")
        return None
    core.log("hosts", f"herdr-core: no Herdr window came up; {path} runs hidden (hc ls --label {LABEL}={path})")
    return None


def app(t):
    """The agent kind a terminal runs, from hc's status; None for a shell or a plain program."""
    name = (t.get("status") or {}).get("app")
    return APPS.get(name, name) if name else None


def labels(t):
    return t.get("labels") or {}


def pane(pane_id):
    t = get(pane_id)
    return {"pane_id": pane_id, "workspace_id": labels(t).get(LABEL)} if t else None


def terminals(path):
    """hc's live terminals in a checkout: muster's (labelled) and any other started inside it (the space's own
    shell, a person's)."""
    mine = {t["address"] for t in hc("ls", "--label", f"{LABEL}={path}")["terminals"]}
    return [t for t in hc("ls")["terminals"]
            if t["address"] in mine or t.get("cwd") == path or (t.get("cwd") or "").startswith(path + "/")]


def panes(workspace):
    found = []
    for row in terminals(workspace):
        t = get(row["address"])
        if t:
            found.append({"pane_id": t.get("name") or t["address"], "workspace_id": workspace,
                          "terminal_id": t["address"], "agent": app(t)})
    return found


def open_tab(workspace, path, label, env, title):
    """A login shell named title in the checkout, in the checkout's space. A live terminal already named title in
    this workspace (a crash after create) is reused, and is labelled into a space if it has none yet (the window
    was closed at create): setup runs on the reuse path too. env is applied when Claude starts. label: unused."""
    found = get(title)
    if found and labels(found).get(LABEL) != workspace:
        raise core.CommandError(f"herdr-core: a live terminal named {title} belongs to "
                                f"{labels(found).get(LABEL) or 'something muster did not start'}")
    where = labels(found or {}).get("herdr.space") or space(path)
    if found:
        if where and labels(found).get("herdr.space") != where:
            hc("label", title, f"herdr.space={where}")
        return title
    hc("run", "--bg", "--no-parent", "--outlive-parent", "--keep", "--name", title, "--cwd", path,
       "--label", f"{LABEL}={workspace}")
    if where:
        hc("label", title, f"herdr.space={where}")  # the shape checked live, the same call as the reuse path
    return title


def agent(target):
    """The agent in a pane or under a name (the same thing here), in herdr's shape; None when there is none: no live
    terminal, a shell with no agent (a core restart leaves a kept terminal so), or a status left over after the
    agent exited (hc reports a foreground only while a program runs, so no foreground is no agent). state_change_seq
    is hc's status_ms, which every status change moves and which holds still while idle (live check); hc has no
    completion seq."""
    t = get(target)
    if t is None or app(t) is None or not t.get("foreground"):
        return None
    state = t["status"].get("state")
    return {"agent": app(t), "name": t.get("name"), "cwd": t.get("cwd"),
            "foreground_cwd": (t.get("foreground") or {}).get("cwd"),
            "agent_status": STATE.get(state, f"hc-{state}"), "state_change_seq": t.get("status_ms"),
            "completion_seq": None, "pane_id": t.get("name") or t["address"], "workspace_id": labels(t).get(LABEL)}


def start_agent(name, kind_, pane_id, args, env):
    """Type `export <env>; <kind> <args>` into the pane's shell, then wait until hc shows the agent ready or at a
    dialog (herdr's start contract). A pane already running the agent (a crash after typing, Claude still booting
    with no status yet) gets nothing typed and is waited on; a pane running anything else raises."""
    t = get(pane_id)
    if t is None:
        raise core.CommandError(f"herdr-core: no live terminal {pane_id}")
    running = (t.get("foreground") or {}).get("argv") or []
    if running and program(running[0]) != kind_:
        raise core.CommandError(f"herdr-core: {pane_id} already runs {' '.join(running)[:80]}; nothing typed")
    if not running:
        line = " ".join(shlex.quote(a) for a in [kind_, *args])
        if env:
            line = "export " + " ".join(shlex.quote(pair) for pair in env) + "; " + line
        send(pane_id, line)
    deadline = clock() + START_WAIT
    while clock() < deadline:
        now = agent(pane_id)
        if now and now["agent_status"] in (*READY, "blocked"):
            return
        sleep(1)
    raise core.CommandError(f"herdr-core: {kind_} was not ready in {pane_id} within {START_WAIT} s")


def prompt(pane_id, text):
    """One prompt with hc's proof: `started: true` is delivered (blocked when the turn is at a dialog, else working);
    hc's `blocked` refusal sent nothing; anything else is unproven. A pane with no agent gets nothing typed."""
    if agent(pane_id) is None:
        raise core.CommandError(f"herdr-core: no agent in {pane_id}; nothing sent")
    try:
        sent = send(pane_id, text, "--wait", "--timeout", SEND_WAIT)
    except core.CommandError as error:
        if kind(error) == "blocked":
            raise Blocked(str(error)) from None
        raise
    if sent.get("started") is not True:
        raise core.CommandError(f"herdr-core: hc took the prompt to {pane_id} but saw no turn start")
    return "blocked" if (sent.get("wait") or {}).get("status") == "blocked" else "working"
