"""hc (herdr-core), herdr-app, git and ps as muster's herdr-core host sees them, with the shapes checked live on
2026-11-19 (docs/plans/2026-11-19-herdr-core-host.md, "Live check"). Built on OrcaWorld for `git worktree
list/add`, ps and `gh pr list`; nothing here answers an orca call."""

import json
import re
import shlex

import muster.core as core
from muster.hosts.herdr_core import worktree_path
from tests.fake_orca import OrcaWorld


def fail(argv, kind, code=1):
    """hc exits non-zero with {"error": {"kind": …}} on stderr; core.run keeps stderr."""
    raise core.CommandError(f"{' '.join(argv[:3])}: exit {code}\n" + json.dumps({"error": {"kind": kind,
                                                                                           "message": kind}}))


class HcWorld(OrcaWorld):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.hc = {}                 # address -> {address, name, cwd, labels, pid, state, status, status_ms, turn,
                                     #             foreground}
        self.window = True           # a Herdr window answers `herdr-app status`
        self.opens = True            # `open -g -a Herdr` brings a window up
        self.spaces = {}             # space id -> {"name", "folder"}
        # hc's own knobs: OrcaWorld's agent_start, send_mode and after_turn keep their orca meanings, because
        # AcceptanceWorld (Task 8) serves both hosts from one world.
        self.hc_start = "idle"       # what a typed `claude` reports first: idle | blocked | none
        self.hc_send = "started"     # started | not_started | blocked
        self.hc_after = "working"    # the status right after a started turn
        self.hc_late = 0             # `hc get`s a new Claude answers with no status yet (booting)
        self.booting = {}            # address -> `hc get`s left before its status shows

    def term(self, name, cwd, labels=None):
        """A live login shell; returns its address."""
        address = self.next_id("t")
        self.pid += 1
        self.procs[self.pid] = (1, "-/bin/zsh -l")
        self.hc[address] = {"address": address, "name": name, "cwd": cwd, "labels": dict(labels or {}),
                            "pid": self.pid, "state": "live", "status": None, "status_ms": None, "turn": 0,
                            "foreground": None}
        return address

    def hc_claude(self, address, state):
        """Claude running in a terminal's shell, reporting state."""
        t = self.hc[address]
        self.pid += 1
        self.seq += 1
        self.procs[self.pid] = (t["pid"], "claude --model opus")
        t.update(status={"app": "claude-code", "state": state}, status_ms=self.seq,
                 foreground={"argv": ["claude", "--model", "opus"], "cwd": t["cwd"], "name": "2.1.328",
                             "pid": self.pid})
        self.booting[address] = self.hc_late

    def hc_live(self, clone, branch, name, state="idle"):
        """A checkout with a named, labelled terminal running Claude, made before this test. Returns (path, address)."""
        path, _ = self.worktree(clone, branch, worktree_path(clone, branch))
        address = self.term(name, path, {"muster.path": path})
        self.hc_claude(address, state)
        return path, address

    def hc_restart(self):
        """The core restarted: each kept terminal is a fresh login shell, its program gone (hc --skill)."""
        for t in self.hc.values():
            if t["state"] == "live":
                for pid in [p for p, (ppid, _) in self.procs.items() if p == t["pid"] or ppid == t["pid"]]:
                    del self.procs[pid]
                self.pid += 1
                self.procs[self.pid] = (1, "-/bin/zsh -l")
                t.update(pid=self.pid, status=None, foreground=None)

    def find(self, target):
        """The live terminal with this name or address, else a finished one, else None."""
        hits = [t for t in self.hc.values() if target in (t["address"], t["name"])]
        return next((t for t in hits if t["state"] == "live"), hits[0] if hits else None)

    def end(self, t):
        t.update(state="finished", status=None, foreground=None)
        for pid in [p for p, (ppid, _) in self.procs.items() if p == t["pid"] or ppid == t["pid"]]:
            del self.procs[pid]

    def dispatch(self, argv):
        if argv[0] == "hc":
            return self.hc_cmd(argv)
        if argv[0] == "herdr-app":
            return self.app(argv)
        if argv == ["open", "-g", "-a", "Herdr"]:
            self.window = self.window or self.opens
            return ""
        if argv[:2] == ["sh", "-c"]:
            return self.shell(argv[2])
        return super().dispatch(argv)

    def git(self, where, args):
        if args[:4] == ["worktree", "add", "--no-track", "-b"]:
            branch, path, base = args[4], args[5], args[6]
            assert base.startswith("origin/") and (where, branch) not in self.branches
            self.worktree(where, branch, path)
            return ""
        if args == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            wt = self.worktrees.get(where)
            return f"{wt['clone'] if wt else where}/.git\n"
        if args[:2] == ["worktree", "remove"]:
            del self.worktrees[args[-1]]
            return ""
        return super().git(where, args)

    def row(self, t):
        return {k: t[k] for k in ("address", "name", "cwd", "state", "turn")} | {"agent": "zsh"}

    def shown(self, t):
        """`hc get`'s shape: foreground only while a program runs; a booting Claude has no status yet."""
        out = {k: v for k, v in t.items() if k != "foreground"}
        if self.booting.get(t["address"]):
            self.booting[t["address"]] -= 1
            out["status"] = None
        return out | ({"foreground": t["foreground"]} if t["foreground"] else {})

    def hc_cmd(self, argv):
        if argv[1] == "read":
            t = self.find(argv[2])
            return f"screen of {t['address']}\n"
        assert "--json" in argv, argv
        args = [a for a in argv[1:] if a != "--json"]
        verb = args[0]
        if verb == "run":
            assert {"--bg", "--no-parent", "--outlive-parent", "--keep"} <= set(args), argv
            name, cwd = args[args.index("--name") + 1], args[args.index("--cwd") + 1]
            if any(t["name"] == name and t["state"] == "live" for t in self.hc.values()):
                fail(argv, "name_taken")
            labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
            return json.dumps(self.row(self.hc[self.term(name, cwd, labels)]))
        if verb == "get":
            t = self.find(args[1])
            if t is None:
                fail(argv, "not_found", 3)
            return json.dumps(self.shown(t))
        if verb == "ls":
            label = args[args.index("--label") + 1].split("=", 1) if "--label" in args else None
            rows = [self.row(t) for t in self.hc.values()
                    if (t["state"] == "live" or "--all" in args) and (not label or t["labels"].get(label[0]) == label[1])]
            return json.dumps({"terminals": rows, "hidden_finished": 0})
        if verb == "label":
            t = self.find(args[1])
            key, value = args[2].split("=", 1)
            t["labels"][key] = value
            return json.dumps(self.shown(t))
        if verb == "send":
            cut = args.index("--")
            opts, (target, text) = args[1:cut], args[cut + 1:]
            t = self.find(target)
            if t is None or t["state"] != "live":
                fail(argv, "not_found", 3)
            if t["foreground"] is None:  # a shell at its prompt: muster types Claude's command line
                assert "--wait" not in opts
                self.typed.append(text)
                if self.hc_start != "none":
                    self.hc_claude(t["address"], self.hc_start)
                return json.dumps({"started": None, "status": None, "turn": t["turn"]})
            if (t["status"] or {}).get("state") == "blocked" or self.hc_send == "blocked":
                fail(argv, "blocked", 9)  # refused before typing
            assert opts == ["--wait", "--timeout", "10"], opts
            before = t["status"]["state"]
            self.submitted.append(text)
            if self.on_submit:
                self.on_submit(text)
            if self.hc_send == "not_started":
                return json.dumps({"started": False, "status": before, "turn": t["turn"]})
            self.seq += 1
            t["status"] = {"app": "claude-code", "state": self.hc_after}
            t["status_ms"] = self.seq
            return json.dumps({"started": True, "status": before, "turn": t["turn"],
                               "wait": {"status": self.hc_after, "reason": "timeout", "ended": False,
                                        "turn": t["turn"]}})
        if verb == "kill":
            t = self.find(args[1])
            if t is None:
                fail(argv, "not_found", 3)
            self.end(t)
            return json.dumps(self.shown(t))
        if verb == "rm":
            t = self.find(args[1])
            if t is None:
                fail(argv, "not_found", 3)
            assert t["state"] == "finished", "hc rm of a live terminal"
            del self.hc[t["address"]]
            return json.dumps({"removed": t["address"]})
        raise AssertionError(argv)

    def app(self, argv):
        if not self.window and argv[1] != "commands":
            raise core.CommandError(f"{' '.join(argv[:3])}: exit 1\nno Herdr window is open")
        if argv[1:] == ["status"]:
            return json.dumps({"space": next(iter(self.spaces), None)})
        if argv[1:] == ["ls"]:
            return json.dumps({"spaces": [{"id": s, "name": v["name"], "folder": v["folder"], "machine": None,
                                           "tabs": []} for s, v in self.spaces.items()]})
        if argv[1:3] == ["do", "space.new"]:
            name, folder = argv[argv.index("--name") + 1], argv[argv.index("--folder") + 1]
            sid = self.next_id("sp")
            self.spaces[sid] = {"name": name, "folder": folder}
            address = self.term(None, folder, {"herdr.space": sid})  # the space's own shell
            return json.dumps({"space": sid, "tab": self.next_id("tb"), "terminal": address})
        if argv[1:3] == ["do", "space.close"]:
            sid = argv[argv.index("--id") + 1]
            assert argv[argv.index("--confirmed") + 1] == "true"
            self.spaces.pop(sid)
            for t in self.hc.values():
                if t["labels"].get("herdr.space") == sid and t["state"] == "live":
                    self.end(t)
            return json.dumps({"ok": True})
        raise AssertionError(argv)

    def shell(self, script):
        """remove_argv's and close_argv's `sh -c` line, run step by step through this world (Task 4's shape)."""
        for step in script.split(" && "):
            words = shlex.split(re.sub(r"[{};]|>/dev/null|2>&1", " ", step))
            if words[:3] == ["herdr-app", "do", "space.close"]:
                self.handle(["herdr-app", "do", "space.close", "--id", words[4], "--confirmed", "true"])
            elif words[:2] == ["hc", "kill"]:
                address = words[2]
                t = self.find(address)
                if t and t["state"] == "live":
                    self.handle(["hc", "kill", address, "--json"])
                if "rm" in words and self.find(address):  # `hc rm A || ! hc get A`: gone already is fine
                    self.handle(["hc", "rm", address, "--json"])
            elif words[:1] == ["git"]:
                self.handle(words)
            else:
                raise AssertionError(step)
        return ""
