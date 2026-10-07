"""cleanup: close a workspace muster opened, once its work is finished and every pane has been quiet
for 30 minutes; warn the human about one left with unsaved work.

Cron runs it every five minutes. Every workspace with a durable owner record is covered:

  issue     a <branch_prefix><n> worktree whose <git dir>/muster-card.json names this repository,
            issue n, this worktree path and its open workspace
  run       an ad-hoc run: <data dir>/runs/<card>/run.json names repo, branch, base, worktree
            and workspace
  coding    <data dir>/workspaces/<name>.json, kind "coding", with the same fields
  analysis  <data dir>/workspaces/<name>.json, kind "analysis": a workspace with no pull request,
            opened by `hermes muster open`. herdr reuses workspace ids, so the record also names
            the workspace label and the terminal id of its first pane.

A workspace without a record is not ours and is never touched.

A coding worktree (issue, run, coding) is removed only when all of these hold:

  merged   at least one pull request from its branch, every one merged into its base from this
           repository, and HEAD is the head commit of the newest (issue: whose body has a line
           `Closes #<n>`)
  clean    `git status --porcelain --untracked-files=all` prints nothing but agent scaffolding a
           tool copied in from the main checkout (see `scaffolding`)
  pushed   HEAD is the head of a pull request, or every commit of HEAD is on a remote ref
  quiet    every pane with an agent is idle or done; every other pane runs only a shell,
           herdr-reviewr, herdr-file-view or lazygit; nothing else runs under any pane's shell but MCP or language servers
           (a job started with &, an agent's background command)

An analysis workspace needs only quiet. Closing it deletes no file.

Those facts, with each pane's agent state counter, foreground processes and visible screen, make
a fingerprint. herdr keeps no idle timestamp, so the state file records when this job first saw the
current fingerprint. Once runs no more than MAX_GAP apart, on a clock that never went backwards,
have seen the same fingerprint for IDLE seconds, everything is read again, and only if it still
matches does `herdr worktree remove --workspace` (coding) or `herdr workspace close` (analysis)
run, never with --force. The branch stays, locally and on GitHub. Any failed check or command
drops the record, so the 30-minute window starts over. Before that remove, the scaffolding copies
are read again, backed up under backups/cleanup with a manifest, and deleted, since git refuses to
remove a checkout with untracked files; any change to them since the check keeps the workspace.

A coding worktree that is quiet for 30 minutes but not clean or not pushed is never removed.
Unless a pull request from its branch is open (work under way), once per state, a card on the muster
board, subscribed notify+wake and blocked, pings the human and wakes the agent with the workspace,
branch and reason, and asks what to do. The card is archived when that state changes; a new problem
opens a new card. This job never pushes, commits, discards, deletes a branch, merges or deploys.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from . import config, core, events, runs


def state_path():
    return config.data_dir() / "logs" / "cleanup.json"


def lock_path():
    return config.data_dir() / "logs" / "cleanup.lock"


def owned_dir():
    return config.data_dir() / "workspaces"


def backups_dir():
    return config.data_dir() / "backups" / "cleanup"


IDLE = 1800
# Cron runs every 300 s. A longer gap between two observations (sleep, cron off) is time nobody watched.
MAX_GAP = 900
# How far GitHub's merge time may run ahead of this clock before the clock is not trusted.
SKEW = 300
# herdr: both mean the agent is ready for input.
READY = {"idle", "done"}
# The only foreground processes a pane without an agent may run.
# The herdr layout's own tools: herdr-reviewr and herdr-file-view only show files; lazygit keeps
# nothing but the repository, and a change it has not committed still fails `clean`.
QUIET = {"zsh", "bash", "sh", "fish", "herdr-reviewr", "herdr-file-view", "lazygit"}
# Long-lived helpers an agent keeps under it while idle: MCP servers and language servers, named by
# the program or its first argument (`tally mcp`, `pyright-langserver`, `node .../langserver.index.js`),
# never by a path or word further along. An MCP server named otherwise keeps the workspace (safe side).
HELPER = re.compile(r"mcp|.+-mcp|mcp-.+|.*langserver.*|.+-language-server|.+-lsp", re.I)
SECRET = re.compile(r"gh[pousr]_\w+|github_pat_\w+")
# Agent scaffolding a tool copies into each new worktree (Codex, 2026-10: radicalcandorai #461).
# Only these paths may be deleted as copies, and only when `copied` proves them one.
SCAFFOLD = re.compile(r"AGENTS\.md|\.agents/.+|\.codex/.+")
FAILURES = (core.CommandError, OSError, ValueError, KeyError, TypeError, AttributeError)
ASK_LINE = ("This card is a cleanup warning. Tell the human the workspace, branch and reason, and ask what "
            "to do; never push, commit, discard or remove anything yourself (skill muster:escalation).")
clock = time.time


class NotReady(Exception):
    """A check failed; the message says which."""


def say(line):
    """One log line: whitespace folded, anything shaped like a GitHub token redacted."""
    print(SECRET.sub("[redacted]", " ".join(str(line).split()))[:400])


def herdr(*args):
    return json.loads(core.run(["herdr", *args]))["result"]


def git(path, *args):
    return core.run(["git", "-C", path, *args])


def digest(data):
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def listed(clone):
    return [w for w in herdr("worktree", "list", "--cwd", str(clone))["worktrees"] if w.get("is_linked_worktree")]


def find(clone, path):
    """herdr's current entry for this checkout. NotReady when herdr no longer lists it."""
    wt = next((w for w in listed(clone) if w.get("path") == path), None)
    if wt is None:
        raise NotReady(f"herdr no longer lists {path}")
    return wt


def intake_link(repo, wt):
    """An issue worktree's link, from the card the launch left in its git dir."""
    path, workspace = wt["path"], wt.get("open_workspace_id")
    card = json.loads(Path(git(path, "rev-parse", "--absolute-git-dir").strip(), core.CARD_FILE).read_text())
    number = card.get("issue")
    if (not isinstance(number, int)
            or (card.get("repo"), card.get("worktree"), card.get("workspace")) != (repo, path, workspace)):
        raise NotReady(f"card names {card.get('repo')}#{number} {card.get('worktree')} {card.get('workspace')}; "
                       f"herdr has {wt.get('branch')} {path} {workspace}")
    return {"repo": repo, "branch": f"{config.settings['branch_prefix']}{number}", "base": "main", "worktree": path,
            "workspace": workspace, "issue": number, "card": card}


def processes():
    """pid -> (parent pid, command) of every process on this machine."""
    table = {}
    for line in core.run(["ps", "-A", "-o", "pid=,ppid=,command="]).splitlines():
        pid, ppid, *command = line.split(None, 2)
        table[int(pid)] = (int(ppid), command[0] if command else "")
    return table


def helper(command):
    """True when the program or its first argument is an MCP or language server."""
    return any(HELPER.fullmatch(word.rsplit("/", 1)[-1]) for word in command.split()[:2])


def under(shell, table):
    """(pid, depth below the shell, command) of every process the pane's shell started, however deep."""
    found = []
    for pid, (_, command) in table.items():
        up, depth = pid, 0
        while up in table and up != shell and up > 1 and depth < 64:
            up, depth = table[up][0], depth + 1
        if up == shell and pid != shell:
            found.append((pid, depth, command))
    return sorted(found)


def quiet(workspace):
    """Each pane's evidence. NotReady when a pane is busy."""
    panes = herdr("pane", "list", "--workspace", workspace)["panes"]
    if not panes or any(p.get("workspace_id") != workspace for p in panes):
        raise NotReady(f"workspace {workspace} lists no panes, or another workspace's")
    table = processes()
    seen = []
    for pane in sorted(panes, key=lambda p: p["pane_id"]):
        pid = pane["pane_id"]
        info = herdr("pane", "process-info", "--pane", pid)["process_info"]
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
        if pane.get("agent"):
            agent = herdr("agent", "get", pid)["agent"]
            if agent.get("agent_status") not in READY:
                raise NotReady(f"pane {pid}: {agent.get('agent')} is {agent.get('agent_status')}")
            state = [agent.get("agent"), agent.get("agent_status"), agent.get("state_change_seq")]
        else:
            busy = sorted({str(p.get("name")) for p in procs} - QUIET)
            if busy:
                raise NotReady(f"pane {pid} runs {', '.join(busy)}")
            state = None
        screen = hashlib.sha256(core.run(["herdr", "pane", "read", pid, "--source", "visible"]).encode())
        seen.append([pid, pane.get("terminal_id"), state,
                     sorted(json.dumps([p.get("pid"), p.get("cmdline")]) for p in procs), screen.hexdigest(),
                     [p for p, _, _ in tree]])
    return seen


def regular(root, rel):
    """The bytes of root/rel, or None unless it is a regular file reached through no symlink."""
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or any(part in (".", "..") for part in parts):
        return None
    path = Path(root)
    for part in parts[:-1]:
        path = path / part
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return None
    fd = os.open(path / parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        return handle.read() if stat.S_ISREG(os.fstat(fd).st_mode) else None


def mcp_only(data, roots):
    """True when this .codex/config.toml holds nothing but MCP servers whose command and args are
    the ones each root's .mcp.json names: the config a tool generates from .mcp.json."""
    servers = tomllib.loads(data.decode())
    if list(servers) != ["mcp_servers"] or not servers["mcp_servers"]:
        return False
    sources = [json.loads(regular(root, ".mcp.json"))["mcpServers"] for root in roots]
    return all(server == {"command": source[name]["command"], "args": source[name]["args"]}
               for name, server in servers["mcp_servers"].items() for source in sources)


def copied(path, clone, rel):
    """sha256 of the worktree's file when it is a copy of the main checkout's: the same bytes;
    .codex/hooks.json the same JSON once this worktree's path reads as main's; .codex/config.toml,
    which main does not have, generated from .mcp.json. None otherwise, or on any doubt."""
    try:
        mine = regular(path, rel)
        if mine is None:
            return None
        theirs = regular(clone, rel) if os.path.lexists(Path(clone, rel)) else None
        same = (mine == theirs
                or (rel == ".codex/hooks.json" and theirs is not None
                    and json.loads(mine.decode().replace(str(path), str(clone))) == json.loads(theirs))
                or (rel == ".codex/config.toml" and not os.path.lexists(Path(clone, rel))
                    and mcp_only(mine, (path, clone))))
        return hashlib.sha256(mine).hexdigest() if same else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def scaffolding(path, clone, status):
    """(copies, rest): the untracked scaffolding files that are verified copies, as sorted
    [path, sha256] pairs, and every other line of `git status --porcelain`."""
    copies, rest = [], []
    for line in status.splitlines():
        sha = line.startswith("?? ") and SCAFFOLD.fullmatch(line[3:]) and copied(path, clone, line[3:])
        if sha:
            copies.append([line[3:], sha])
        else:
            rest.append(line)
    return sorted(copies), rest


def clear(link, clone, copies):
    """Back up and delete the scaffolding copies the check found, read again first: anything else
    untracked or changed keeps them all. The backup's path."""
    path = link["worktree"]
    now, rest = scaffolding(path, clone, git(path, "status", "--porcelain", "--untracked-files=all"))
    if rest or now != copies:
        raise NotReady("scaffolding changed right before removal")
    backup = backups_dir() / f"{int(clock())}-{Path(path).name}"
    for rel, sha in copies:
        (backup / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
        (backup / "files" / rel).write_bytes(regular(path, rel))
        if hashlib.sha256((backup / "files" / rel).read_bytes()).hexdigest() != sha:
            raise NotReady(f"scaffolding changed right before removal: {rel}")
    (backup / "manifest.json").write_text(json.dumps({
        "worktree": path, "clone": str(clone), "branch": link["branch"], "workspace": link["workspace"],
        "files": copies, "restore": f"mkdir -p {path} && cp -Rp {backup}/files/. {path}/"}, indent=2))
    for rel, sha in copies:
        data = regular(path, rel)
        if data is None or hashlib.sha256(data).hexdigest() != sha:
            raise NotReady(f"scaffolding changed right before removal: {rel}")
        Path(path, rel).unlink()
    for rel, _ in copies:  # the directories they leave empty
        for parent in Path(rel).parents:
            if str(parent) != ".":
                try:
                    Path(path, parent).rmdir()
                except OSError:
                    break
    return backup


def merged_gate(link, prs, head, now):
    """Why this worktree's pull requests do not allow removal yet, or None."""
    repo, branch, base = link["repo"], link["branch"], link["base"]
    if not prs:
        return f"no pull request from {branch}"
    for pr in prs:
        if (not pr["merged_at"] or pr["base"]["ref"] != base or pr["head"]["ref"] != branch
                or pr["head"]["repo"]["full_name"] != repo):
            return f"PR #{pr['number']} is not merged from {branch} into {base}"
        if datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")).timestamp() > now + SKEW:
            return f"PR #{pr['number']} merged after this clock's now: clock not trusted"
    newest = max(prs, key=lambda pr: pr["merged_at"])
    if "issue" in link and not re.search(rf"(?m)^Closes #{link['issue']}\s*$", newest["body"] or ""):
        return f"PR #{newest['number']} has no line 'Closes #{link['issue']}'"
    if head != newest["head"]["sha"]:
        return f"HEAD {head[:12]} is not the merged head {newest['head']['sha'][:12]}"
    return None


def coding(link, wt, now, clone):
    """(fingerprint, warning, gate, copies) of a quiet tracked worktree. warning is (reason, signature)
    when work would be lost; gate says why the pull requests do not allow removal yet; copies are the
    scaffolding files to delete before removal."""
    path, workspace = wt["path"], wt.get("open_workspace_id")
    if not workspace or (link["worktree"], link["workspace"], link["branch"]) != (path, workspace, wt.get("branch")):
        raise NotReady(f"record names {link['branch']} {link['worktree']} {link['workspace']}; "
                       f"herdr has {wt.get('branch')} {path} {workspace}")
    repo, branch = link["repo"], link["branch"]
    head = git(path, "rev-parse", "HEAD").strip()
    status = git(path, "status", "--porcelain", "--untracked-files=all")
    prs = json.loads(core.run(
        ["gh", "api", f"repos/{repo}/pulls?head={repo.split('/')[0]}:{branch}&state=all&per_page=100"]))
    copies, rest = scaffolding(path, clone, status)
    problems = []
    if rest:
        names = ", ".join(line[3:] for line in rest[:5]) + (", ..." if len(rest) > 5 else "")
        problems.append(f"{len(rest)} uncommitted or untracked files: {names}")
    # A squash merge leaves a PR head on no remote ref once its branch is pruned, so a PR head counts as pushed.
    if (head not in {pr["head"]["sha"] for pr in prs}
            and git(path, "rev-list", "--count", "HEAD", "--not", "--remotes").strip() != "0"):
        problems.append(f"local commits not pushed (HEAD {head[:12]})")
    seen = quiet(workspace)
    # An open pull request is work still under way: its own run pages for it.
    under_way = any(pr.get("state") == "open" for pr in prs)
    warning = ("; ".join(problems), digest([link, head, status])) if problems and not under_way else None
    prs_seen = [[pr["number"], pr["merged_at"], pr["head"]["sha"]] for pr in prs]
    return (digest([link, head, status, copies, prs_seen, seen]), warning, merged_gate(link, prs, head, now),
            copies)


def analysis(rec):
    """(fingerprint, None, None) of a quiet analysis workspace that is still the recorded one."""
    workspace = rec["workspace"]
    if herdr("workspace", "get", workspace)["workspace"].get("label") != rec["label"]:
        raise NotReady(f"workspace {workspace} is no longer labelled {rec['label']}")
    terminals = {p.get("terminal_id") for p in herdr("pane", "list", "--workspace", workspace)["panes"]}
    if rec["terminal"] not in terminals:
        raise NotReady(f"workspace {workspace} no longer has terminal {rec['terminal']}")
    return digest([rec, quiet(workspace)]), None, None


def escalate(link, reason, signature):
    """Open (or find) this state's warning card, subscribed notify+wake and blocked once. Its id."""
    where = f"workspace {link['workspace']} | {link['repo']} {link['branch']} | worktree {link['worktree']}"
    card = json.loads(core.kanban(
        "create", "--body",
        f"Cleanup warning. {where}\nNot removed: {reason}.\nDecision for the human: push or commit the work, "
        f"discard it, or keep the workspace? Cleanup waits until the checkout is clean and pushed.\n{ASK_LINE}\n{core.PROVENANCE}",
        "--idempotency-key", f"muster-cleanup:{signature}", "--created-by", "muster-cleanup", "--json",
        "--", f"Unsaved work in {link['repo'].split('/')[-1]}"))["id"]
    if events.status(card) == "ready":
        core.subscribe(card)
        core.kanban("block", "--kind", "needs_input", card,
                         f"Cleanup kept a workspace because it has unsaved work: {reason}. Push or commit it, "
                         f"discard it, or keep the workspace?\nOpen Herdr workspace {link['workspace']}.")
        events.expect(card, "blocked", "cleanup warning")
    return card


def step(target, state, dry):
    """One target, one run. Records its fingerprint only while every check passes. True on a failed command."""
    name, key, check = target["name"], target["key"], target["check"]
    old, new, warned = state
    now = clock()
    try:
        fingerprint, warning, gate = check(now)
    except NotReady as why:
        say(f"{name}: kept, {why}")
        return False
    except FAILURES as error:
        say(f"{name}: kept, check failed: {error}")
        return True
    if key in warned and (warning is None or warned[key]["sig"] != warning[1]) and not dry:
        card = warned[key]["card"]
        try:
            if events.status(card) != "archived":
                core.kanban("archive", card)
            say(f"{name}: state changed; warning card {card} archived")
        except FAILURES as error:  # the card is only a notice: forget it rather than stall this target
            say(f"{name}: state changed; archiving warning card {card} failed, archive it by hand: {error}")
        del warned[key]
    was = old.get(key) if isinstance(old.get(key), dict) else {}
    since, last = was.get("since"), was.get("seen")
    watched = (was.get("fingerprint") == fingerprint
               and all(isinstance(t, (int, float)) for t in (since, last))
               and since <= last <= now <= last + MAX_GAP)
    if not watched:
        new[key] = {"fingerprint": fingerprint, "since": now, "seen": now}
        say(f"{name}: kept, quiet clock starts now")
        return False
    new[key] = {"fingerprint": fingerprint, "since": since, "seen": now}
    if now - since < IDLE:
        say(f"{name}: kept, quiet {int(now - since) // 60} of {IDLE // 60} min")
        return False
    if warning:
        reason, signature = warning
        if warned.get(key, {}).get("sig") == signature:
            return False  # the human already has this exact state
        if dry:
            say(f"{name}: would warn the human: {reason}")
            return False
        try:
            card = escalate(target["link"], reason, signature)
        except FAILURES as error:
            say(f"{name}: kept, {reason}; warning failed: {error}")
            return True
        warned[key] = {"sig": signature, "card": card}
        say(f"{name}: kept, {reason}; human warned on card {card}")
        return False
    if gate:
        say(f"{name}: kept, {gate}")
        return False
    # Read everything again, right before removing: any change since the check above keeps it.
    del new[key]
    try:
        later = clock()
        if not now <= later <= now + MAX_GAP or check(later) != (fingerprint, None, None):
            say(f"{name}: kept, changed on the recheck")
            return False
    except NotReady as why:
        say(f"{name}: kept, recheck: {why}")
        return False
    except FAILURES as error:
        say(f"{name}: kept, recheck failed: {error}")
        return True
    argv, what = target["remove"]()
    if dry:
        say(f"{name}: would {what}")
        new[key] = {"fingerprint": fingerprint, "since": since, "seen": now}
        return False
    try:
        if target.get("clear"):
            backup = target["clear"]()
            if backup:
                say(f"{name}: agent scaffolding backed up to {backup} and deleted")
        core.run(argv)
        if target.get("record"):
            target["record"].unlink(missing_ok=True)
    except NotReady as why:
        say(f"{name}: kept, {why}")
        return False
    except FAILURES as error:
        say(f"{name}: {what} failed: {error}")
        return True
    say(f"{name}: did {what}; branch kept")
    return False


def coding_target(name, repo, clone, path, link=None, record=None):
    """A worktree whose link is known up front (run, coding) or read from its card each check (intake)."""
    held, copies = dict(link or {}), []

    def check(now):
        wt = find(clone, path)
        if link is None:
            held.clear()
            held.update(intake_link(repo, wt))
        fingerprint, warning, gate, copies[:] = coding(held, wt, now, clone)
        return fingerprint, warning, gate

    def remove():
        also = f", after deleting {len(copies)} agent scaffolding copies" if copies else ""
        return (["herdr", "worktree", "remove", "--workspace", held["workspace"]],
                f"remove worktree {path} and workspace {held['workspace']}{also}")
    return {"name": name, "key": path, "link": held, "check": check, "remove": remove, "record": record,
            "clear": lambda: copies and clear(held, clone, list(copies))}


def targets(dry=False):
    """Every workspace with an owner record, and whether a listing failed."""
    found, failed, paths, lists = [], False, set(), {}

    def worktrees(label, clone):
        """herdr's worktrees of a clone, listed once per run; None when the listing failed."""
        if str(clone) not in lists:
            try:
                lists[str(clone)] = listed(clone)
            except FAILURES as error:
                say(f"{label}: worktree list failed: {error}")
                lists[str(clone)] = None
        return lists[str(clone)]

    prefix = config.settings["branch_prefix"]
    for repo, (clone, _) in config.repos().items():
        if worktrees(repo, clone) is None:
            failed = True
            continue
        for wt in worktrees(repo, clone):
            branch = wt.get("branch") or ""
            if not branch.startswith(prefix):
                continue
            if not wt.get("path"):  # a malformed herdr entry: name it, keep going
                say(f"{repo}: {wt.get('path')}: kept, herdr lists no path")
                failed = True
                continue
            try:
                if not Path(git(wt["path"], "rev-parse", "--absolute-git-dir").strip(), core.CARD_FILE).is_file():
                    continue  # the user's own branch, or a run's: not an issue worktree
            except FAILURES as error:
                say(f"{repo}: {wt['path']}: kept, check failed: {error}")
                failed = True
                continue
            paths.add(wt["path"])
            found.append(coding_target(f"{repo}#{branch[len(prefix):]}", repo, clone, wt["path"]))
    workspaces = None
    # Newest first: a branch reused after its old card was archived is claimed by its current run.
    records = sorted(runs.runs_dir().glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for record in records + sorted(owned_dir().glob("*.json")):
        try:
            rec = json.loads(record.read_text())
            owned = record.parent == owned_dir()
            if owned and rec.get("kind") == "analysis":
                if workspaces is None:
                    workspaces = {w["workspace_id"] for w in herdr("workspace", "list")["workspaces"]}
                if rec["workspace"] not in workspaces:  # closed by hand; herdr may hand its id to another
                    if not dry:
                        record.unlink()
                    say(f"{record.name}: workspace {rec['workspace']} is gone; record dropped")
                    continue
                found.append({
                    "name": f"{rec['workspace']} {rec['label']}", "key": f"{rec['workspace']}:{rec['terminal']}",
                    "check": lambda now, rec=rec: analysis(rec), "record": record,
                    "remove": lambda rec=rec: (["herdr", "workspace", "close", rec["workspace"]],
                                               f"close workspace {rec['workspace']} ({rec['label']})")})
            elif (not owned or rec.get("kind") == "coding") and rec.get("worktree") and rec.get("workspace"):
                listing = worktrees(rec["repo"], rec["clone"])
                if listing is None:
                    failed = True
                    continue
                # Removed already (most finished runs), or seen under another record: nothing to do.
                if rec["worktree"] in paths or not any(w.get("path") == rec["worktree"] for w in listing):
                    continue
                paths.add(rec["worktree"])
                link = {k: rec[k] for k in ("repo", "branch", "base", "worktree", "workspace")}
                found.append(coding_target(f"{rec['repo']} {rec['branch']}", rec["repo"], rec["clone"],
                                           rec["worktree"], link, record if owned else None))
        except FAILURES as error:
            say(f"{record}: kept, unreadable: {error}")
            failed = True
    return found, failed


def run_once(dry):
    try:
        old = json.loads(state_path().read_text())
    except FileNotFoundError:
        old = {}
    except (OSError, ValueError):
        say(f"{state_path()} unreadable: every quiet clock starts over")
        old = {}
    if not isinstance(old, dict):
        old = {}
    quiet_old = old.get("quiet") if isinstance(old.get("quiet"), dict) else {}
    warned = old.get("warned") if isinstance(old.get("warned"), dict) else {}
    # Only targets seen this run carry a quiet record forward: a removed one drops out by itself.
    # ponytail: a warning for a worktree removed by hand stays in `warned`; its card stays for the human to close.
    new = {}
    found, failed = targets(dry)
    for target in found:
        try:
            failed = step(target, (quiet_old, new, warned), dry) or failed
        except FAILURES as error:
            say(f"{target['name']}: kept, {error}")
            failed = True
    try:
        state_path().parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps({"quiet": new, "warned": warned}))
        os.replace(tmp, state_path())
    except OSError as error:  # the old file stays; the next run judges from it or starts over
        say(f"state not saved: {error}")
        failed = True
    return 1 if failed else 0


def open_one(cwd, label):
    """An analysis workspace: open it and record its owner, so cleanup may close it later."""
    cwd = str(Path(cwd).expanduser().resolve())
    made = herdr("workspace", "create", "--cwd", cwd, "--label", label, "--no-focus")
    rec = {"kind": "analysis", "workspace": made["workspace"]["workspace_id"], "label": label, "cwd": cwd,
           "terminal": made["root_pane"]["terminal_id"], "pane": made["root_pane"]["pane_id"],
           "created": int(clock())}
    owned_dir().mkdir(parents=True, exist_ok=True)
    tmp = owned_dir() / f".{rec['terminal']}.tmp"
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, owned_dir() / f"{rec['terminal']}.json")
    print(json.dumps(rec))
    return rec


def cleanup(args):
    core.prepare_env()
    config.require()
    lock_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path(), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return run_once(args.dry_run)


def open_workspace(args):
    core.prepare_env()
    config.require()
    try:
        open_one(args.cwd, args.label)
    except FAILURES as error:
        print(f"open failed: {error}", file=sys.stderr)
        return 1
    return 0
