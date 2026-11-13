"""hosts: the terminal host a run lives in, one module per host, picked by name (`host` setting).

herdr is the default; orca is the option (docs/decisions/2026-11-12-orca-host.md). A host module is a peer of the
others; the contract is this docstring, no base class (the muster/channels pattern). Every host speaks herdr's dict
shapes and fills only the fields it really has. A record made before this seam names no host: it is herdr's.

- `NAME`: the module's name, as records store it.
- `worktree_path(clone, branch) -> str`: where a new checkout of branch goes.
- `worktrees(clone) -> [{"path", "branch", "open_workspace_id", "is_linked_worktree"}]`: every git worktree of
  the clone; branch is short (no refs/heads/); open_workspace_id is None when the host has none open.
- `create_worktree(clone, branch, base, path, label) -> {"workspace", "path"}`: a checkout of branch from
  origin/<base>; path is where it really is (the caller records it).
- `open_worktree(clone, path, label) -> workspace`: the host's workspace for an existing checkout.
- `panes(workspace) -> [{"pane_id", "workspace_id", "terminal_id", "agent"}]`: agent is the kind, or None.
- `pane(pane) -> {"pane_id", "workspace_id"} | None`: None only when the pane is gone.
- `open_tab(workspace, path, label, env, title) -> pane`: a shell tab in the checkout; title names the agent.
- `agent(target) -> {"agent", "name", "cwd", "foreground_cwd", "agent_status", "state_change_seq",
  "completion_seq", "pane_id", "workspace_id"} | None`: target is a pane or an agent name. None only when there is
  none; raises core.CommandError when the host cannot tell (try again later). agent_status is idle, working,
  blocked or done; anything else is neither ready nor delivered.
- `start_agent(name, kind, pane, args, env)`: start kind in the pane with args, returning once it is ready or shows
  a dialog; raises core.CommandError otherwise (the caller then reads `agent(pane)` itself).
- `prompt(pane, text) -> agent_status`: send one prompt; raises Blocked when refused before anything was sent,
  core.CommandError when delivery is unproven.
- `screen(pane) -> str`: what the pane shows now.
- `quiet(workspace) -> list`: each pane's evidence, JSON-serialisable; raises NotReady when one is busy or unknown.
- `remove_argv(workspace, path, force) -> argv`: the command that removes the checkout and its workspace.
- `open_workspace(cwd, label) -> {"workspace", "terminal", "pane"}`: an analysis workspace.
- `analysis_gone(rec) -> bool`: an analysis record's workspace was closed by hand.
- `analysis_check(rec)`: raises NotReady unless the record still names what is there.
- `close_argv(rec) -> argv`: closes an analysis workspace; raises NotReady when it cannot be named now.
"""

import importlib
import re

from .. import config, core
from ..config import ConfigError

NAMES = ("herdr", "orca")
READY = ("idle", "done")
DELIVERED = ("working", "blocked", "done")
# The only foreground processes a pane without an agent may run.
# The herdr layout's own tools: herdr-reviewr and herdr-file-view only show files; lazygit keeps
# nothing but the repository, and a change it has not committed still fails `clean`.
QUIET = {"zsh", "bash", "sh", "fish", "herdr-reviewr", "herdr-file-view", "lazygit"}
# Long-lived helpers an agent keeps under it while idle: MCP servers and language servers, named by
# the program or its first argument (`tally mcp`, `pyright-langserver`, `node .../langserver.index.js`),
# never by a path or word further along. An MCP server named otherwise keeps the workspace (safe side).
HELPER = re.compile(r"mcp|.+-mcp|mcp-.+|.*langserver.*|.+-language-server|.+-lsp", re.I)


class NotReady(Exception):
    """A check failed; the message says which."""


class Blocked(core.CommandError):
    """The host refused a prompt before sending anything: the agent shows a dialog."""


def get(name):
    if name not in NAMES:
        raise ConfigError(f"muster: host {name!r} is not one of {', '.join(NAMES)}")
    return importlib.import_module(f"{__name__}.{name}")


def of(record):
    """The host a record names: its own "host", else its launch's; neither (made before this seam) is herdr."""
    record = record or {}
    launch = record.get("launch") if isinstance(record.get("launch"), dict) else {}
    return get(record.get("host") or launch.get("host") or "herdr")


def configured():
    return get(config.settings["host"])


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


def program(command):
    """A command line's program name: `-/bin/zsh -l` -> zsh, `/usr/local/bin/claude --model x` -> claude."""
    return (command.split() or [""])[0].lstrip("-").rsplit("/", 1)[-1]
