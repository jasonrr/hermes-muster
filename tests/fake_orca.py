"""Orca, git, lsof and ps as muster's orca host sees them, with the shapes checked live on 2026-11-12
(docs/plans/2026-11-12-orca-host.md, "Live check"). Built on fake_herdr.World for git and `gh pr list`."""

import json
from pathlib import Path

import muster.core as core
import muster.hosts.orca as orca
from tests.fake_herdr import World


def fail(argv, code):
    """Orca exits 1 with its JSON error on stdout; core.run keeps it (Task 1)."""
    raise core.CommandError(f"{' '.join(argv[:3])}: exit 1\n" + json.dumps({"ok": False, "error": {"code": code}}))


def ok(result):
    return json.dumps({"ok": True, "result": result})


class OrcaWorld(World):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.repos = {}         # clone or folder path -> repo id
        self.visible = set()    # repo ids that show external worktrees
        self.terminals = {}     # handle -> {worktreeId, worktreePath, paneTitle, tabId, leafId, ptyId, shell, login,
                                #            late, orphaned, recordedPaneKey}
        self.orca_agents = {}   # handle -> {agentType, state, stateStartedAt}
        self.cwds = {}          # pid -> cwd (lsof)
        self.procs = {}         # pid -> (ppid, command) (ps)
        self.typed = []         # command lines typed into a shell
        self.title_late = 0     # terminal list calls before a new terminal shows its paneTitle
        self.agent_start = "done"   # what a typed `claude` shows first: done | waiting | working | none
        self.boot = []          # states a started agent passes through, one per `worktree ps`, before agent_start
        self.send_mode = "turn_started"  # turn_started | accepted (no turn proof) | blocked | lost (no hook)
        self.after_turn = "working"     # the agent state right after a proven turn start
        self.pid = 1000

    def wid(self, path):
        return f"{self.repos[self.worktrees[path]['clone']]}::{path}"

    def orca_live(self, clone, branch, title, state="done"):
        """A checkout with a titled terminal running Claude, made before this test. Returns (path, handle)."""
        self.repos.setdefault(str(clone), self.next_id("repo"))
        path, _ = self.worktree(clone, branch, orca.worktree_path(clone, branch))
        handle = self.terminal(self.wid(path), path, title)
        self.claude(handle, state)
        return path, handle

    def terminal(self, wid, path, title):
        handle = self.next_id("term_")
        self.pid += 2
        login, shell = self.pid, self.pid + 1
        self.procs[login] = (1, "/usr/bin/login -flpq u /bin/zsh -l")
        self.procs[shell] = (login, "-/bin/zsh -l")
        self.cwds[shell] = path
        self.terminals[handle] = {"worktreeId": wid, "worktreePath": path, "paneTitle": title,
                                  "tabId": self.next_id("tab"), "leafId": self.next_id("leaf"),
                                  "ptyId": f"{wid}@@{handle}", "shell": shell, "login": login,
                                  "late": self.title_late, "orphaned": False, "recordedPaneKey": None}
        return handle

    def pane_key(self, handle):
        t = self.terminals[handle]
        return f"{t['tabId']}:{t['leafId']}"

    def claude(self, handle, state):
        t = self.terminals[handle]
        self.seq += 1
        self.orca_agents[handle] = {"agentType": "claude", "state": state, "stateStartedAt": self.seq}
        self.pid += 1
        self.procs[self.pid] = (t["shell"], "claude --model opus")
        self.cwds[self.pid] = t["worktreePath"]
        t["agent_pid"] = self.pid

    def restart(self):
        """Orca restarted and no window shows the terminals yet: new handles, rows orphaned, titles and agents gone
        from Orca's view; the processes run on (live check)."""
        moved, agents = {}, {}
        for handle, t in self.terminals.items():
            new = self.next_id("term_")
            moved[new] = {**t, "orphaned": True, "recordedPaneKey": f"{t['tabId']}:{t['leafId']}",
                          "tabId": f"pty:{t['ptyId']}", "leafId": f"pty:{t['ptyId']}", "paneTitle": None,
                          "saved": t}
            if handle in self.orca_agents:
                agents[new] = self.orca_agents[handle]
        self.terminals, self.hidden_agents, self.orca_agents = moved, agents, {}

    def adopt(self):
        """A window shows the terminals again, with their old tab and leaf (assumed; Task 10 checks) but no title."""
        for handle, t in list(self.terminals.items()):
            if t.get("orphaned"):
                self.terminals[handle] = {**t["saved"], "paneTitle": None, "late": 0}
        self.orca_agents.update(getattr(self, "hidden_agents", {}))
        self.hidden_agents = {}

    def dispatch(self, argv):
        if argv[0] == "orca":
            assert argv[-1] == "--json", argv
            return self.orca(argv)
        if argv == ["lsof", "-a", "-d", "cwd", "-Fpn"]:
            return "".join(f"p{pid}\nn{cwd}\n" for pid, cwd in self.cwds.items())
        if argv == ["ps", "-A", "-o", "pid=,ppid=,command="]:
            return "".join(f"{pid} {ppid} {cmd}\n" for pid, (ppid, cmd) in self.procs.items())
        return super().dispatch(argv)

    def git(self, where, args):
        if args == ["worktree", "list", "--porcelain"]:
            out = f"worktree {where}\nHEAD sha\nbranch refs/heads/main\n\n"
            for p, w in self.worktrees.items():
                if w["clone"] == where:
                    out += f"worktree {p}\nHEAD sha\nbranch refs/heads/{w['branch']}\n\n"
            return out
        if args[:2] == ["worktree", "add"]:
            path, branch = args[2], args[3]
            assert (where, branch) in self.branches, "git worktree add of a branch that does not exist"
            assert not any(w["branch"] == branch and w["clone"] == where for w in self.worktrees.values())
            self.worktree(where, branch, path)
            return ""
        return super().git(where, args)

    def advance_boot(self):
        for state in self.orca_agents.values():
            if state.get("boot"):
                state["state"] = state["boot"].pop(0)

    def orca(self, argv):
        args = argv[1:-1]

        def opt(flag):
            return args[args.index(flag) + 1]
        verb = args[:2]
        if verb == ["repo", "show"]:
            path = opt("--repo").removeprefix("path:")
            if path not in self.repos:
                fail(argv, "repo_not_found")
            return ok({"repo": {"id": self.repos[path], "path": path}})
        if verb == ["repo", "add"]:
            path = opt("--path")
            self.repos.setdefault(path, self.next_id("repo"))
            return ok({"repo": {"id": self.repos[path], "path": path}})
        if verb == ["repo", "set"]:
            assert opt("--external-worktree-visibility") == "show"
            self.visible.add(opt("--repo").removeprefix("id:"))
            return ok({})
        if verb == ["worktree", "create"]:
            rid = opt("--repo").removeprefix("id:")
            clone = next(p for p, r in self.repos.items() if r == rid)
            branch, name = opt("--branch"), opt("--name")
            assert opt("--base-branch").startswith("origin/") and opt("--setup") == "skip" and "--no-parent" in args
            # Live check: Orca renames a branch with commits of its own. muster never asks it for an existing one.
            assert (clone, branch) not in self.branches, "orca worktree create on an existing branch"
            path = str(Path(orca.ROOT) / Path(clone).name / name)
            self.worktree(clone, branch, path)
            self.terminal(self.wid(path), path, None)  # Orca opens one plain shell with the worktree
            return ok({"worktree": {"id": self.wid(path), "path": path, "branch": f"refs/heads/{branch}"}})
        if verb == ["worktree", "rm"]:
            path = opt("--worktree").removeprefix("path:")
            del self.worktrees[path]
            for h in [h for h, t in self.terminals.items() if t["worktreePath"] == path]:
                self.close(h)
            return ok({})
        if verb == ["worktree", "ps"]:
            self.advance_boot()
            rows = {}
            for h, t in self.terminals.items():
                row = rows.setdefault(t["worktreeId"], {"worktreeId": t["worktreeId"], "path": t["worktreePath"],
                                                        "agents": []})
                if h in self.orca_agents:
                    a = self.orca_agents[h]
                    row["agents"].append({"paneKey": self.pane_key(h), "agentType": a["agentType"],
                                          "state": a["state"], "stateStartedAt": a["stateStartedAt"]})
            return ok({"worktrees": list(rows.values()), "truncated": False})
        if verb == ["terminal", "list"]:
            wanted = opt("--worktree").removeprefix("id:") if "--worktree" in args else None
            rows = []
            for h, t in self.terminals.items():
                if wanted and t["worktreeId"] != wanted:
                    continue
                rows.append({"handle": h, "connected": True, "orphaned": t["orphaned"],
                             "recordedPaneKey": t["recordedPaneKey"],
                             "paneTitle": t["paneTitle"] if t["late"] <= 0 else None,
                             **{k: t[k] for k in ("worktreeId", "worktreePath", "tabId", "leafId", "ptyId")}})
                t["late"] -= 1
            return ok({"terminals": rows})
        if verb == ["terminal", "create"]:
            sel = opt("--worktree")
            if sel.startswith("id:"):
                wid = sel[3:]
                path = wid.split("::", 1)[1]
            else:
                path = sel.removeprefix("path:")
                if path not in self.repos:
                    fail(argv, "selector_not_found")
                wid = f"{self.repos[path]}::{path}"
            handle = self.terminal(wid, path, opt("--title"))
            t = self.terminals[handle]
            return ok({"terminal": {"handle": handle, "processId": t["login"], "worktreeId": wid,
                                    "paneKey": self.pane_key(handle), "tabId": t["tabId"], "ptyId": t["ptyId"]}})
        if verb == ["terminal", "set-pane-title"]:
            t = self.terminals[opt("--terminal")]
            t["paneTitle"], t["late"] = opt("--title"), 0
            return ok({})
        if verb == ["terminal", "send"]:
            handle = opt("--terminal")
            if handle not in self.terminals:
                fail(argv, "terminal_handle_stale")
            text = opt("--text")
            if handle not in self.orca_agents:  # a shell: muster types Claude's command line
                assert "--enter" in args and "--wait-submit" not in args
                self.typed.append(text)
                if self.agent_start != "none":
                    self.claude(handle, self.boot[0] if self.boot else self.agent_start)
                    self.orca_agents[handle]["boot"] = [*self.boot[1:], self.agent_start] if self.boot else []
                return ok({"send": {"handle": handle, "accepted": True}})
            if self.orca_agents[handle]["state"] == "waiting" or self.send_mode == "blocked":
                fail(argv, "agent_prompt_blocked")  # refused before any input is sent
            assert opt("--wait-submit") == "30"
            self.submitted.append(text)
            if self.on_submit and self.send_mode != "lost":
                self.on_submit(text)
            stages = ["input_accepted"]
            if self.send_mode in ("turn_started", "lost"):
                stages.append("turn_started")
                self.seq += 1
                self.orca_agents[handle].update(state=self.after_turn, stateStartedAt=self.seq)
            return ok({"send": {"handle": handle, "accepted": True, "prompt": {"stages": stages}}})
        if verb == ["terminal", "read"]:
            assert "--screen" in args
            return ok({"terminal": {"tail": [f"screen of {self.terminals[opt('--terminal')]['ptyId']}"]}})
        if verb == ["terminal", "close"]:
            self.close(opt("--terminal"))
            return ok({})
        raise AssertionError(argv)

    def close(self, handle):
        t = self.terminals.pop(handle)
        self.orca_agents.pop(handle, None)
        for pid in [p for p, (ppid, _) in self.procs.items() if p in (t["login"], t["shell"]) or ppid == t["shell"]]:
            self.procs.pop(pid, None)
            self.cwds.pop(pid, None)
