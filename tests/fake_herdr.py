"""herdr, git and `gh pr list` as a launch sees them, with herdr 0.9.3's documented agent behaviour.

agent-automation.mdx: `agent start` returns once Claude is detected and idle-ready (or fails with
the agent still there); `agent prompt --wait` returns the first working/blocked state, or
agent_blocked before sending anything, or agent_prompt_stalled / timeout, which prove nothing about
delivery. `start` and `prompt` modes pick the outcome; `on_submit(text)` stands for Claude's
UserPromptSubmit hook. `handle(argv)` returns None for a command it does not model.
"""

import json
from pathlib import Path

import muster.core as core


def fail(argv, code):
    raise core.CommandError(f"{' '.join(argv[:3])}: exit 1\n" + json.dumps({"error": {"code": code}}))


class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.worktrees = {}  # path -> {branch, clone, workspace, git_dir}
        self.branches = {}   # (clone, branch) -> sha
        self.origin = "sha_main"
        self.origin_head = "main"  # what refs/remotes/origin/HEAD names; None = unset
        self.remote_head = None    # what `git remote set-head origin -a` finds; None = it fails
        self.set_heads = 0
        self.panes = {}      # pane -> workspace
        self.agents = {}     # pane -> agent dict
        self.dirty, self.ahead, self.prs = {}, {}, []
        self.start, self.prompt = "ready", "working"
        self.submitted, self.on_submit, self.calls = [], None, []
        self.crash = None    # a predicate on argv: raise KeyboardInterrupt instead of running it
        self.crash_after = None  # a predicate on argv: run it, then raise KeyboardInterrupt
        self.seq, self.ids = 10, 0

    def next_id(self, prefix):
        self.ids += 1
        return f"{prefix}{self.ids}"

    def worktree(self, clone, branch, path=None, owner=None, workspace=True):
        """A checkout made earlier, outside this launch (owner: the card its marker names)."""
        path = str(path or Path(core.worktree_path(clone, branch)))
        git_dir = self.tmp / "gitdirs" / Path(path).name
        git_dir.mkdir(parents=True, exist_ok=True)
        Path(path).mkdir(parents=True, exist_ok=True)
        if owner:
            (git_dir / core.OWNER_FILE).write_text(json.dumps({"owner": owner, "branch": branch}))
        self.worktrees[path] = {"branch": branch, "clone": str(clone), "git_dir": str(git_dir),
                                "workspace": self.next_id("w") if workspace else None}
        self.branches[(str(clone), branch)] = self.branches.get((str(clone), branch), self.origin)
        return path, git_dir

    def live_agent(self, pane, workspace, name, cwd, status="idle"):
        self.panes[pane] = workspace
        self.agents[pane] = {"agent": "claude", "name": name, "agent_status": status, "pane_id": pane,
                             "workspace_id": workspace, "cwd": cwd, "state_change_seq": 1}

    def agent_by(self, target):
        if target in self.agents:
            return self.agents[target]
        return next((a for a in self.agents.values() if a["name"] == target), None)

    def handle(self, argv):
        if self.crash and self.crash(argv):
            raise KeyboardInterrupt  # killed before the command ran: it leaves no trace
        self.calls.append(argv)
        out = self.dispatch(argv)
        if self.crash_after and self.crash_after(argv):
            self.crash_after = None
            raise KeyboardInterrupt
        return out

    def dispatch(self, argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps(self.prs)
        if argv[0] == "git":
            return self.git(argv[2], argv[3:])
        if argv[0] != "herdr":
            return None
        verb = argv[1:3]
        if verb == ["worktree", "list"]:
            clone = argv[argv.index("--cwd") + 1]
            return json.dumps({"result": {"worktrees": [
                {"path": clone, "branch": "main", "is_linked_worktree": False}] + [
                {"path": p, "branch": w["branch"], "open_workspace_id": w["workspace"], "is_linked_worktree": True}
                for p, w in self.worktrees.items() if w["clone"] == clone]}})
        if verb == ["worktree", "create"]:
            clone, branch, path = (argv[argv.index(f) + 1] for f in ("--cwd", "--branch", "--path"))
            assert "--trust-repository" in argv and "--no-focus" in argv
            assert path not in self.worktrees and not any(w["branch"] == branch for w in self.worktrees.values())
            path, _ = self.worktree(clone, branch, path)
            return json.dumps({"result": {"workspace": {"workspace_id": self.worktrees[path]["workspace"]},
                                          "worktree": {"path": path}}})
        if verb == ["worktree", "open"]:
            path = argv[argv.index("--path") + 1]
            self.worktrees[path]["workspace"] = self.next_id("w")
            return json.dumps({"result": {"workspace": {"workspace_id": self.worktrees[path]["workspace"]}}})
        if verb == ["pane", "get"]:
            if argv[3] not in self.panes:
                fail(argv, "pane_not_found")
            return json.dumps({"result": {"pane": {"pane_id": argv[3], "workspace_id": self.panes[argv[3]]}}})
        if verb == ["pane", "list"]:
            workspace = argv[argv.index("--workspace") + 1]
            return json.dumps({"result": {"panes": [
                {"pane_id": p, "workspace_id": w, **({"agent": "claude"} if p in self.agents else {})}
                for p, w in self.panes.items() if w == workspace]}})
        if verb == ["tab", "create"]:
            workspace = argv[argv.index("--workspace") + 1]
            pane = f"{workspace}:{self.next_id('p')}"
            self.panes[pane] = workspace
            return json.dumps({"result": {"root_pane": {"pane_id": pane}}})
        if verb == ["agent", "get"]:
            agent = self.agent_by(argv[3])
            if agent is None:
                fail(argv, "agent_not_found")
            return json.dumps({"result": {"agent": agent}})
        if verb == ["agent", "start"]:
            return self.start_agent(argv)
        if verb == ["agent", "prompt"]:
            return self.submit(argv)
        raise AssertionError(argv)

    def git(self, where, args):
        wt = self.worktrees.get(where)
        if args[:1] == ["fetch"]:
            return ""
        if args == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
            if self.origin_head is None:
                raise core.CommandError("git -C x: exit 128\nfatal: ref refs/remotes/origin/HEAD is not a symbolic ref")
            return f"refs/remotes/origin/{self.origin_head}\n"
        if args == ["remote", "set-head", "origin", "-a"]:
            self.set_heads += 1
            if self.remote_head is None:
                raise core.CommandError("git -C x: exit 128\nfatal: could not read from remote repository")
            self.origin_head = self.remote_head
            return ""
        if args == ["rev-parse", "--absolute-git-dir"]:
            return wt["git_dir"] + "\n"
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return wt["branch"] + "\n"
        if args[:1] == ["rev-parse"] and args[-1].startswith("origin/"):
            return self.origin + "\n"
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            sha = self.branches.get((where, args[3].removeprefix("refs/heads/")))
            if sha is None:
                raise core.CommandError("git -C x: exit 1")
            return sha + "\n"
        if args[:2] == ["rev-list", "--count"]:
            key = wt["branch"] if wt else args[2].split("..refs/heads/")[1]
            return f"{self.ahead.get(key, 0)}\n"
        if args[:1] == ["update-ref"]:
            branch = args[1].removeprefix("refs/heads/")
            assert self.branches[(where, branch)] == args[3], "update-ref must name the old value"
            self.branches[(where, branch)] = args[2]
            return ""
        if args[:2] == ["status", "--porcelain"]:
            return "".join(f"?? {f}\n" for f in self.dirty.get(where, []))
        return None

    def start_agent(self, argv):
        name, pane = argv[3], argv[argv.index("--pane") + 1]
        tail = argv[argv.index("--") + 1:]
        # The two-phase protocol: start carries Claude's flags and never a task.
        assert tail[-2] == "--settings" and tail.count("--permission-mode") == 1
        if pane in self.agents:
            fail(argv, "pane_not_available")
        status = {"ready": "idle", "timeout-idle": "idle", "blocked": "blocked", "working": "working"}.get(self.start)
        if status:
            self.seq += 1
            self.agents[pane] = {"agent": "claude", "name": name, "agent_status": status, "pane_id": pane,
                                 "workspace_id": self.panes[pane], "state_change_seq": self.seq,
                                 "cwd": next(p for p, w in self.worktrees.items() if w["workspace"] == self.panes[pane])}
        if self.start == "ready":
            return json.dumps({"result": {"agent": self.agents[pane]}})
        fail(argv, "agent_not_ready" if self.start == "blocked" else "timeout")

    def submit(self, argv):
        pane, text = argv[3], argv[4]
        assert "--wait" in argv and argv.count("--until") == 2
        agent = self.agents.get(pane)
        if agent is None:
            fail(argv, "agent_not_found")
        if agent["agent_status"] == "blocked" or self.prompt == "agent_blocked":
            fail(argv, "agent_blocked")  # rejected before any input is sent
        self.submitted.append(text)
        if self.on_submit and self.prompt != "lost":
            self.on_submit(text)
        self.seq += 1
        mode = self.prompt
        if mode in ("working", "blocked"):
            agent.update(agent_status=mode, state_change_seq=self.seq)
            return json.dumps({"result": {"agent": agent}})
        if mode == "stalled-working":  # herdr missed it in 5 s, but Claude is on it now
            agent.update(agent_status="working", state_change_seq=self.seq)
        if mode == "stalled-done":     # the turn started and finished between two observations
            agent.update(agent_status="done", state_change_seq=self.seq, completion_seq=self.seq)
        fail(argv, "timeout" if mode == "timeout" else "agent_prompt_stalled")
