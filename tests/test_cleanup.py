"""cleanup: close a workspace muster owns after 30 quiet minutes, checked every 5 min; warn the human about unsaved work."""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import muster.cleanup as cleanup
import muster.config as config
import muster.core as core
import muster.runs as runs

REPO = "o/r"
MERGED_AT = "2026-09-28T17:00:10Z"
MERGED_EPOCH = datetime.fromisoformat(MERGED_AT.replace("Z", "+00:00")).timestamp()
BASE = MERGED_EPOCH + 7200


@pytest.fixture(autouse=True)
def cleanup_state(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setitem(config.settings, "repos", [f"{REPO}={clone}"])
    cleanup.lock_path().parent.mkdir(parents=True)


class World:
    """Fakes core.run: one merged, clean, quiet issue worktree of issue 15, the board, and
    any analysis workspaces in self.spaces."""

    def __init__(self, tmp_path):
        self.clone, _ = config.repos()[REPO]
        self.path = str(tmp_path / "wt15")
        self.git_dir = tmp_path / "wt15-gitdir"
        self.git_dir.mkdir()
        self.workspace = "wR"
        self.card = {"card": "t_1", "repo": REPO, "issue": 15, "pane": "wR:p2", "workspace": "wR",
                     "worktree": self.path}
        self._write_card()
        self.branch = f"{config.settings['branch_prefix']}15"
        self.listed_branch = self.branch
        self.no_workspace = False
        self.malformed_entry = None
        self.extra = []  # more worktree entries herdr lists
        self.removed = False
        self.head_sha = "abc123"
        self.status = ""
        self.unpushed = "0"  # git rev-list --count HEAD --not --remotes
        self.pulls = [self._pr()]
        self.screens = {"wR:p1": "shell prompt", "wR:p2": "claude ui", "wR:p3": "reviewer ui"}
        self.agent_status = {"wR:p2": "idle"}
        self.seq = {"wR:p2": 178}
        self.processes = {
            "wR:p1": [{"name": "zsh", "pid": 1, "cmdline": "-zsh"}],
            "wR:p2": [{"name": "2.1.283", "pid": 2, "cmdline": "claude"}],
            "wR:p3": [{"name": "herdr-reviewr", "pid": 3, "cmdline": "herdr-reviewr"}],
        }
        # Workspaces without a worktree: id -> {"label", "panes": {pane: terminal}}. Their panes run claude.
        self.spaces = {}
        self.shells = {}  # pane -> its shell's pid
        self.extra_ps = []  # more (pid, ppid, command) rows for ps
        self.dead = set()  # panes whose shell ps does not list
        self.cards, self.keys, self.blocks, self.subscribed = {}, {}, {}, []
        self.fail_on = set()  # argv prefix tuples that raise CommandError
        self.calls = []
        self.pane_reads = 0
        self.flip_after = None
        self.flip_screen = "typed"
        self.real_git = False  # git commands and worktree removal run for real (real_checkouts)

    def _write_card(self):
        self.git_dir.mkdir(exist_ok=True)
        (self.git_dir / core.CARD_FILE).write_text(json.dumps(self.card))

    def _pr(self, number=16, sha=None, merged_at=MERGED_AT):
        return {"number": number, "merged_at": merged_at, "base": {"ref": "main"},
                "head": {"ref": self.branch, "sha": sha or self.head_sha, "repo": {"full_name": REPO}},
                "body": "Fixes stuff.\n\nCloses #15\n"}

    def _panes(self, workspace):
        if workspace == self.workspace and not self.removed:
            return [dict({"pane_id": pid, "workspace_id": workspace, "terminal_id": f"t-{pid}"},
                         **({"agent": "claude"} if pid in self.agent_status else {}))
                    for pid in ("wR:p1", "wR:p2", "wR:p3")]
        space = self.spaces.get(workspace, {})
        return [{"pane_id": pid, "workspace_id": workspace, "terminal_id": term, "agent": "claude"}
                for pid, term in space.get("panes", {}).items()]

    def _shell(self, pane):
        return self.shells.setdefault(pane, 1000 + len(self.shells))

    def _ps(self):
        rows = [(1, 0, "launchd")]
        for pane, shell in self.shells.items():
            if pane in self.dead:
                continue
            rows.append((shell, 1, "-zsh"))
            rows += [(p["pid"], shell, p["cmdline"]) for p in self.processes.get(pane, [])]
        rows += self.extra_ps
        return "".join(f"{pid:>6} {ppid:>6} {command}\n" for pid, ppid, command in rows)

    def run(self, argv):
        self.calls.append(argv)
        for prefix in self.fail_on:
            if tuple(argv[:len(prefix)]) == prefix:
                raise core.CommandError(f"{' '.join(argv[:3])}: exit 1\nboom")
        if argv[:3] == ["herdr", "worktree", "list"]:
            clone = argv[4]
            worktrees = []
            if clone == str(self.clone) and not self.removed:
                entry = {"branch": self.listed_branch, "is_linked_worktree": True, "path": self.path}
                if not self.no_workspace:
                    entry["open_workspace_id"] = self.workspace
                if self.malformed_entry is not None:
                    worktrees.append(self.malformed_entry)
                worktrees.append(entry)
                worktrees.append({"branch": "fix/x", "is_linked_worktree": True,
                                  "path": str(Path(self.path).parent / "wtx"), "open_workspace_id": "wQ"})
                worktrees.extend(self.extra)
            return json.dumps({"result": {"worktrees": worktrees}})
        if argv[:3] == ["herdr", "workspace", "list"]:
            ids = list(self.spaces) + ([] if self.removed else [self.workspace])
            return json.dumps({"result": {"workspaces": [{"workspace_id": i} for i in ids]}})
        if argv[:3] == ["herdr", "workspace", "get"]:
            return json.dumps({"result": {"workspace": {"workspace_id": argv[3],
                                                        "label": self.spaces[argv[3]]["label"]}}})
        if argv[:3] == ["herdr", "workspace", "close"]:
            del self.spaces[argv[3]]
            return json.dumps({"result": {"type": "ok"}})
        if argv[0] == "git" and argv[1] == "-C":
            assert argv[2] == self.path, argv
            if self.real_git:
                return git(*argv[2:])
            if argv[3] == "rev-parse" and argv[4] == "--absolute-git-dir":
                return f"{self.git_dir}\n"
            if argv[3] == "rev-parse" and argv[4] == "HEAD":
                return f"{self.head_sha}\n"
            if argv[3] == "status":
                # Like git: an untracked file in a new directory shows only with --untracked-files=all.
                if "--untracked-files=all" in argv:
                    return self.status
                return "".join(l for l in self.status.splitlines(True) if not l.startswith("??"))
            if argv[3:] == ["rev-list", "--count", "HEAD", "--not", "--remotes"]:
                return f"{self.unpushed}\n"
            raise AssertionError(argv)
        if argv[:2] == ["gh", "api"] and "/pulls?head=" in argv[-1]:
            assert f"repos/{REPO}/pulls?head=o:{self.branch}&state=all&per_page=100" in argv[-1]
            return json.dumps(self.pulls)
        if argv[:3] == ["herdr", "pane", "list"]:
            panes = self._panes(argv[4])
            for pane in panes:  # like herdr: every pane has its shell from the start
                self._shell(pane["pane_id"])
            return json.dumps({"result": {"panes": panes}})
        if argv[:3] == ["herdr", "pane", "process-info"]:
            pid = argv[4]
            return json.dumps({"result": {"process_info": {"foreground_processes": self.processes.get(pid, []),
                                                            "shell_pid": self._shell(pid)}}})
        if argv == ["ps", "-A", "-o", "pid=,ppid=,command="]:
            return self._ps()
        if argv[:3] == ["herdr", "pane", "read"]:
            pid = argv[3]
            self.pane_reads += 1
            if self.flip_after is not None and self.pane_reads > self.flip_after and pid in ("wR:p1", "wY:p1"):
                return self.flip_screen
            return self.screens.get(pid, "")
        if argv[:3] == ["herdr", "agent", "get"]:
            pid = argv[3]
            return json.dumps({"result": {"agent": {"agent": "claude",
                                                     "agent_status": self.agent_status.get(pid, "idle"),
                                                     "state_change_seq": self.seq.get(pid, 0)}}})
        if argv[:3] == ["herdr", "worktree", "remove"]:
            if self.real_git:  # like herdr without --force: git refuses a checkout with untracked files
                try:
                    git(str(self.clone), "worktree", "remove", self.path)
                except subprocess.CalledProcessError as error:
                    raise core.CommandError(error.stderr)
            self.removed = True
            return json.dumps({"result": {"type": "worktree_removed"}})
        if argv[:4] == ["hermes", "kanban", "--board", config.settings["board"]]:
            verb = argv[4]
            if verb == "create":
                key = argv[argv.index("--idempotency-key") + 1]
                card = self.keys.setdefault(key, f"t_warn{len(self.keys) + 1}")
                self.cards.setdefault(card, {"status": "ready", "body": argv[argv.index("--body") + 1],
                                             "title": argv[-1]})
                return json.dumps({"id": card, "status": self.cards[card]["status"]})
            if verb == "show":
                return json.dumps({"task": {"id": argv[5], "status": self.cards[argv[5]]["status"]}})
            if verb == "block":
                card = argv[7]
                assert self.cards[card]["status"] == "ready"
                self.cards[card].update(status="blocked", reason=argv[8])
                self.blocks[card] = self.blocks.get(card, 0) + 1
                return ""
            if verb == "archive":
                self.cards[argv[5]]["status"] = "archived"
                return ""
        raise AssertionError(argv)


def main(argv):
    """The cron and CLI entries, driven by the argv shape the old script took."""
    if argv[:1] == ["open"]:
        return cleanup.open_workspace(argparse.Namespace(cwd=argv[2], label=argv[4]))
    return cleanup.cleanup(argparse.Namespace(dry_run="--dry-run" in argv))


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(core, "run", w.run)
    monkeypatch.setattr(core, "subscribe", w.subscribed.append)
    t = [BASE]
    w.t = t
    monkeypatch.setattr(cleanup, "clock", lambda: t[0])
    return w


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout


def tick(world, minutes, argv=None):
    world.t[0] += minutes * 60
    return main([] if argv is None else argv)


def window(world, runs=6, argv=None):
    """Minute 0, then `runs` more runs five minutes apart."""
    assert main([] if argv is None else argv) == 0
    for _ in range(runs):
        tick(world, 5, argv)


def removes(world):
    return [c for c in world.calls if c[:3] == ["herdr", "worktree", "remove"]
            or c[:3] == ["herdr", "workspace", "close"]]


def quiet_state():
    return json.loads(cleanup.state_path().read_text())["quiet"]


def as_coding(world, branch, workspace, record_dir, name="run.json"):
    """Make the world's worktree a non-intake one (no card, no Closes line), recorded in record_dir."""
    (world.git_dir / core.CARD_FILE).unlink()
    world.branch = world.listed_branch = branch
    world.workspace = workspace
    world.pulls = [world._pr(number=17)]
    world.pulls[0]["body"] = "No closing line.\n"
    record_dir.mkdir(parents=True, exist_ok=True)
    rec = {"kind": "coding", "repo": REPO, "clone": str(world.clone), "branch": branch, "base": "main",
           "worktree": world.path, "workspace": workspace}
    (record_dir / name).write_text(json.dumps(rec))
    return record_dir / name


def as_run(world, branch="fix/cleanup-thing", workspace="wZ"):
    rec = as_coding(world, branch, workspace, runs.runs_dir() / "t_run1")
    data = json.loads(rec.read_text())
    del data["kind"]  # run.json as the run writes it
    data.update(card="t_run1", title="t", pane=f"{workspace}:p2", launched=True)
    rec.write_text(json.dumps(data))
    return rec


# 1. happy path -------------------------------------------------------------

def test_happy_path_removes_after_30_quiet_minutes(world):
    assert main([]) == 0  # minute 0
    for minute in range(5, 35, 5):
        tick(world, 5)
        if minute < 30:
            assert removes(world) == []
        else:
            assert removes(world) == [["herdr", "worktree", "remove", "--workspace", "wR"]]
    assert not any("--force" in c for c in world.calls)
    assert not any("branch" in c and ("-D" in c or "-d" in c) for c in world.calls)
    assert not any(c[:2] == ["gh", "pr"] for c in world.calls)
    for c in world.calls:
        if "--workspace" in c:
            assert c[c.index("--workspace") + 1] == "wR"
    assert not any("wtx" in str(c) or "fix/x" in c for c in world.calls)
    assert world.cards == {}  # a clean merged worktree pages no one


# 2. idempotent repeat --------------------------------------------------------

def test_idempotent_repeat_after_removal_does_nothing(world, capsys):
    window(world)
    assert len(removes(world)) == 1
    capsys.readouterr()
    calls_before = len(world.calls)
    assert tick(world, 5) == 0
    assert tick(world, 5) == 0
    assert len(removes(world)) == 1  # no new remove call
    assert world.calls[calls_before:]  # more calls happened (worktree list) ...
    out = capsys.readouterr().out
    assert "15" not in out
    assert quiet_state() == {}


# 3. active pane restarts the clock -----------------------------------------

def test_active_pane_restarts_the_clock(world):
    assert main([]) == 0
    for _ in range(5):
        tick(world, 5)  # to minute 25
    world.screens["wR:p1"] = "typed"
    tick(world, 5)  # minute 30, screen changed -> restart
    assert removes(world) == []
    for minute in range(5, 35, 5):
        tick(world, 5)
        if minute < 30:
            assert removes(world) == []
        else:
            assert len(removes(world)) == 1


# 4. agent not ready ----------------------------------------------------------

@pytest.mark.parametrize("status", ["working", "blocked", "some-unknown-status"])
def test_agent_not_ready_blocks_removal(world, status, capsys):
    world.agent_status["wR:p2"] = status
    assert main([]) == 0
    out = capsys.readouterr().out
    assert f"pane wR:p2: claude is {status}" in out
    assert quiet_state() == {}


# 5. done counts as ready -----------------------------------------------------

def test_agent_done_counts_as_ready(world):
    world.agent_status["wR:p2"] = "done"
    window(world)
    assert len(removes(world)) == 1


# 6. foreground process --------------------------------------------------------

def test_unexpected_foreground_process_blocks_removal(world, capsys):
    world.processes["wR:p1"] = [{"name": "node", "pid": 9, "cmdline": "node server.js"}]
    assert main([]) == 0
    assert "pane wR:p1 runs node" in capsys.readouterr().out
    assert quiet_state() == {}


# 6b. background work under a pane's shell -------------------------------------------

@pytest.mark.parametrize("row", [
    (50, 2, "/bin/zsh -c npm run build"),  # an agent's background command, under claude
    (51, 1001, "python3 long_job.py"),  # a job started with & in the shell pane
    (52, 2, "caffeinate -i -t 300"),  # Claude keeps the machine awake while it works
    (53, 2, "python ~/mcp/train.py"),  # mentions mcp, is not a server
    (54, 2, "python train-lsp-model.py"),
    (55, 1001, "make lsp-bench"),
])
def test_background_work_keeps_the_workspace(world, row, capsys):
    world.extra_ps = [row]
    window(world, runs=12)
    assert removes(world) == []
    assert "runs in the background" in capsys.readouterr().out


def test_mcp_and_language_servers_under_an_idle_agent_are_quiet(world):
    world.extra_ps = [(60, 2, "/Users/j/.config/herdr/plugins/tally/bin/tally mcp"),
                      (61, 2, "Python /x/.venv/bin/pyright-langserver --stdio"),
                      (62, 61, "/opt/homebrew/bin/node /x/pyright/dist/langserver.index.js -- --stdio")]
    window(world)
    assert len(removes(world)) == 1


def test_a_pane_whose_shell_is_not_running_is_kept(world, capsys):
    world.dead = {"wR:p1"}
    window(world)
    assert removes(world) == [] and "is not running" in capsys.readouterr().out


def test_a_background_job_that_ends_restarts_the_window(world):
    window(world, runs=1)
    world.extra_ps = [(51, 1001, "python3 long_job.py")]
    window(world, runs=1)
    world.extra_ps = []
    window(world, runs=5)  # 25 min after the job ended
    assert removes(world) == []
    tick(world, 5)
    assert len(removes(world)) == 1


@pytest.mark.parametrize("name", ["herdr-file-view", "lazygit"])
def test_herdr_layout_tool_panes_are_quiet(world, name):
    world.processes["wR:p3"] = [{"name": name, "pid": 3, "cmdline": name}]
    window(world)
    assert len(removes(world)) == 1


# 7. unsaved work: never removed; the human is warned once per state -------------------

@pytest.mark.parametrize("dirty, unpushed, reason", [
    ("?? new/deep.txt\n", "0", "1 uncommitted or untracked files"),
    (" M a.py\n", "0", "1 uncommitted or untracked files"),
    ("", "2", "local commits not pushed (HEAD abc123)"),
])
def test_unsaved_work_is_kept_and_warns_the_human_once(world, dirty, unpushed, reason):
    world.status, world.unpushed = dirty, unpushed
    if unpushed != "0":
        world.head_sha = "abc123"
        world.pulls[0]["head"]["sha"] = "older"  # HEAD is past the PR head, on no remote ref
    window(world, runs=5)  # through minute 25: no page while the window runs
    assert world.cards == {}
    tick(world, 5)  # minute 30
    assert removes(world) == []
    [(card, data)] = world.cards.items()
    assert data["status"] == "blocked" and world.subscribed == [card]
    assert reason in data["reason"] and data["reason"].endswith("\nOpen Herdr workspace wR.")
    assert "muster/15" in data["body"] and "muster/15" not in data["reason"]  # provenance stays on the card
    assert "Decision for the human" in data["body"] and world.path in data["body"]
    for _ in range(24):  # two more hours of cron: no second page
        tick(world, 5)
    assert world.blocks == {card: 1} and len(world.cards) == 1
    assert removes(world) == []
    assert not any(c[:2] == ["git", "-C"] and c[3] in ("push", "reset", "checkout", "clean", "stash")
                   for c in world.calls)


def test_unsaved_work_with_an_open_pr_pages_no_one(world):
    world.status = " M a.py\n"
    world.pulls[0].update(state="open", merged_at=None)
    window(world, runs=12)
    assert world.cards == {} and removes(world) == []


def test_unsaved_work_with_no_pr_at_all_warns(world):
    as_run(world)
    world.pulls, world.unpushed = [], "1"
    window(world)
    [(card, data)] = world.cards.items()
    assert "local commits not pushed" in data["reason"] and removes(world) == []


def test_a_new_problem_rearms_and_a_fix_clears_the_warning(world):
    world.status = " M a.py\n"
    window(world)
    [first] = world.cards
    world.status = " M a.py\n?? b.py\n"  # the state changed: old card archived, the window restarts
    tick(world, 5)
    assert world.cards[first]["status"] == "archived"
    window(world)
    assert len(world.cards) == 2 and sum(world.blocks.values()) == 2
    second = [c for c in world.cards if c != first][0]
    world.status = ""  # committed and pushed: the warning clears, cleanup proceeds
    tick(world, 5)
    assert world.cards[second]["status"] == "archived"
    window(world)
    assert len(removes(world)) == 1


def test_a_pr_head_is_pushed_even_when_no_remote_ref_has_it(world):
    world.unpushed = "3"  # squash-merged, branch pruned: the PR head is on no remote ref
    window(world)
    assert world.cards == {} and len(removes(world)) == 1


def test_a_failed_warning_is_retried_and_pages_once(world):
    world.status = " M a.py\n"
    world.fail_on = {("hermes", "kanban", "--board", config.settings["board"], "create")}
    window(world)
    assert world.cards == {}
    world.fail_on = set()
    tick(world, 5)
    tick(world, 5)
    assert sum(world.blocks.values()) == 1


# 7b. agent scaffolding copied from the main checkout --------------------------------

HOOKS = '{{"hooks": {{"PostToolUse": [{{"command": "{root}/.codex/hooks/post-edit-checks.sh"}}]}}}}'
MCP = {"mcpServers": {"neon": {"type": "stdio", "command": "npx",
                               "args": ["-y", "mcp-remote", "https://mcp.neon.tech/mcp"], "env": {}}}}
NEON = '[mcp_servers.neon]\ncommand = "npx"\nargs = [\n    "-y",\n    "mcp-remote",\n    "https://mcp.neon.tech/mcp",\n]\n'
COPIES = ["AGENTS.md", ".agents/skills/x/SKILL.md", ".codex/hooks/post-edit-checks.sh", ".codex/hooks.json"]


def put(root, rel, text):
    Path(root, rel).parent.mkdir(parents=True, exist_ok=True)
    Path(root, rel).write_text(text)


def real_checkouts(world):
    """A real main checkout and a real linked worktree of the run's branch, with the agent files a
    tool copied into it: byte copies of main's, hooks.json with the worktree's own path, and
    .codex/config.toml generated from the tracked .mcp.json. HEAD is the merged PR head."""
    as_run(world)
    main = world.clone
    git(main, "init", "-q", "-b", "main")
    put(main, ".mcp.json", json.dumps(MCP, indent=2))
    put(main, "app.py", "print(1)\n")
    git(main, "add", ".mcp.json", "app.py")
    git(main, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    git(main, "worktree", "add", "-q", "-b", world.branch, world.path)
    for rel in COPIES[:-1]:
        put(main, rel, f"main's {rel}\n")
        put(world.path, rel, f"main's {rel}\n")
    put(main, ".codex/hooks.json", HOOKS.format(root=main))
    put(world.path, ".codex/hooks.json", HOOKS.format(root=world.path))
    put(world.path, ".codex/config.toml", NEON)
    world.real_git = True
    world.head_sha = git(world.path, "rev-parse", "HEAD").strip()
    world.pulls[0]["head"]["sha"] = world.head_sha
    return main


def found(world):
    status = git(world.path, "status", "--porcelain", "--untracked-files=all")
    artifacts, rest = cleanup.scaffolding(world.path, world.clone, status)
    return [rel for rel, _ in artifacts], rest


def test_copied_agent_scaffolding_is_recognized(world):
    real_checkouts(world)
    assert found(world) == (sorted(COPIES + [".codex/config.toml"]), [])


@pytest.mark.parametrize("rel, change", [
    ("AGENTS.md", lambda w, m: put(w.path, "AGENTS.md", "edited by the agent\n")),
    (".agents/skills/new/SKILL.md", lambda w, m: put(w.path, ".agents/skills/new/SKILL.md", "new\n")),
    (".codex/hooks.json", lambda w, m: put(w.path, ".codex/hooks.json", HOOKS.format(root="/elsewhere"))),
    (".codex/hooks.json", lambda w, m: put(w.path, ".codex/hooks.json", "{not json")),
    (".codex/config.toml", lambda w, m: put(w.path, ".codex/config.toml", NEON + "model = \"o3\"\n")),
    (".codex/config.toml", lambda w, m: put(w.path, ".codex/config.toml",
                                            NEON.replace("mcp.neon.tech", "evil.example"))),
    (".codex/config.toml", lambda w, m: put(w.path, ".codex/config.toml",
                                            NEON + '[mcp_servers.other]\ncommand = "x"\nargs = []\n')),
    (".codex/config.toml", lambda w, m: put(w.path, ".codex/config.toml", "[mcp_servers.neon\n")),
    (".codex/config.toml", lambda w, m: put(m, ".codex/config.toml", "# main has its own\n")),
    (".codex/config.toml", lambda w, m: Path(m, ".mcp.json").write_text("{}")),
    ("AGENTS.md", lambda w, m: Path(m, "AGENTS.md").unlink()),
    ("AGENTS.md", lambda w, m: Path(w.path, "AGENTS.md").chmod(0)),
    ("AGENTS.md", lambda w, m: (Path(w.path, "AGENTS.md").unlink(),
                                Path(w.path, "AGENTS.md").symlink_to(Path(m, "AGENTS.md")))),
    (".agents/skills/x/SKILL.md", lambda w, m: (shutil.rmtree(Path(w.path, ".agents/skills/x")),
                                                Path(w.path, ".agents/skills/x").symlink_to(
                                                    Path(m, ".agents/skills/x")))),
    ("app2.py", lambda w, m: (put(m, "app2.py", "same\n"), put(w.path, "app2.py", "same\n"))),
])
def test_anything_not_a_verified_copy_still_counts_as_unsaved(world, rel, change):
    main = real_checkouts(world)
    change(world, main)
    artifacts, rest = found(world)
    # git lists a symlinked directory as one entry: .agents/skills/x
    assert rel not in artifacts and any(rel == line[3:] or rel.startswith(line[3:] + "/") for line in rest)


def test_tracked_or_staged_scaffolding_is_never_a_copy(world):
    main = real_checkouts(world)
    git(world.path, "add", "AGENTS.md")
    artifacts, rest = found(world)
    assert "AGENTS.md" not in artifacts and "A  AGENTS.md" in rest


def test_only_scaffolding_copies_are_backed_up_deleted_and_the_worktree_removed(world, capsys):
    real_checkouts(world)
    window(world)
    assert removes(world) == [["herdr", "worktree", "remove", "--workspace", "wZ"]]
    assert not Path(world.path).exists() and world.cards == {}
    assert git(world.clone, "branch", "--list", world.branch).strip()  # the branch stays
    [backup] = cleanup.backups_dir().iterdir()
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["worktree"] == world.path and manifest["branch"] == world.branch
    assert sorted(rel for rel, _ in manifest["files"]) == sorted(COPIES + [".codex/config.toml"])
    assert (backup / "files" / ".codex/hooks.json").read_text() == HOOKS.format(root=world.path)
    assert "restore" in manifest
    assert str(backup) in capsys.readouterr().out


def test_a_unique_file_beside_the_copies_keeps_everything_and_names_it(world):
    real_checkouts(world)
    put(world.path, ".agents/skills/x/notes.md", "my work\n")
    window(world)
    assert removes(world) == [] and Path(world.path, "AGENTS.md").exists()
    [(card, data)] = world.cards.items()
    assert "1 uncommitted or untracked files: .agents/skills/x/notes.md" in data["reason"]
    assert not cleanup.backups_dir().exists()


def test_dry_run_with_scaffolding_deletes_nothing(world, capsys):
    real_checkouts(world)
    window(world, argv=["--dry-run"])
    assert removes(world) == [] and len(found(world)[0]) == 5
    assert "would remove" in capsys.readouterr().out and not cleanup.backups_dir().exists()


@pytest.mark.parametrize("race", [
    lambda w: put(w.path, "AGENTS.md", "edited at the last moment\n"),
    lambda w: put(w.path, ".codex/extra.json", "{}"),
])
def test_a_change_right_before_deletion_keeps_the_workspace(world, monkeypatch, capsys, race):
    real_checkouts(world)
    window(world, runs=5)  # through minute 25
    calls, real = [], world.run

    def run(argv):
        if argv[3:4] == ["status"]:
            calls.append(argv)
            if len(calls) == 3:  # the check, the recheck, then right before deleting
                race(world)
        return real(argv)
    monkeypatch.setattr(core, "run", run)
    tick(world, 5)
    assert removes(world) == [] and Path(world.path, ".codex/hooks.json").exists()
    assert "changed right before removal" in capsys.readouterr().out
    assert not cleanup.backups_dir().exists()


# 8. linkage --------------------------------------------------------------------

def _card(world, **changes):
    world.card.update(changes)
    world._write_card()


@pytest.mark.parametrize("mutate", [
    lambda w: _card(w, repo="o/other-repo"),
    lambda w: _card(w, worktree="/somewhere/else"),
    lambda w: _card(w, workspace="wOther"),
])
def test_card_mismatch_blocks_removal(world, mutate):
    mutate(world)
    assert main([]) == 0
    assert removes(world) == []
    assert quiet_state() == {}


def test_missing_open_workspace_blocks_removal(world):
    world.no_workspace = True
    assert main([]) == 0
    assert removes(world) == []


def test_worktree_on_the_wrong_issue_branch_blocks_removal(world):
    world.listed_branch = "muster/16"
    window(world)
    assert removes(world) == []


# 9. PR checks --------------------------------------------------------------------

@pytest.mark.parametrize("mutate", [
    lambda w: setattr(w, "pulls", []),
    lambda w: w.pulls.append(w._pr(number=20, sha="other", merged_at=None)),  # a second PR, not merged
    lambda w: w.pulls[0].__setitem__("merged_at", None),
    lambda w: w.pulls[0]["base"].__setitem__("ref", "dev"),
    lambda w: w.pulls[0].__setitem__("body", "Closes #150\n"),
    lambda w: w.pulls[0].__setitem__("body", "no closing line here\n"),
    lambda w: w.pulls[0]["head"].__setitem__("sha", "different-sha"),
])
def test_pr_check_blocks_removal(world, mutate):
    mutate(world)
    window(world)
    assert removes(world) == []
    assert world.cards == {}


def test_every_pr_merged_and_head_is_the_newest_removes(world):
    world.pulls.insert(0, world._pr(number=9, sha="first", merged_at="2026-09-27T10:00:00Z"))
    window(world)
    assert len(removes(world)) == 1


def test_head_of_an_older_merged_pr_only_is_kept(world):
    world.pulls.append(world._pr(number=30, sha="newer", merged_at="2026-09-28T17:30:00Z"))
    window(world)
    assert removes(world) == []


# 10. clock ------------------------------------------------------------------------

def test_clock_going_backwards_restarts_the_quiet_window(world):
    assert main([]) == 0
    world.t[0] -= 100
    assert main([]) == 0
    assert quiet_state()[world.path]["since"] == world.t[0]
    assert removes(world) == []


def test_a_gap_over_max_gap_restarts_the_quiet_window(world):
    assert main([]) == 0
    world.t[0] += 20 * 60
    assert main([]) == 0
    assert quiet_state()[world.path]["since"] == world.t[0]
    assert removes(world) == []


def test_merged_at_far_ahead_of_the_clock_is_not_trusted(world, capsys):
    world.t[0] = MERGED_EPOCH - cleanup.SKEW - cleanup.IDLE - 400  # still ahead after the window
    window(world)
    assert "clock" in capsys.readouterr().out
    assert removes(world) == []


# 11. API error ------------------------------------------------------------------

def test_api_failure_drops_the_record_and_reports_failure(world):
    window(world, runs=5)  # through minute 25, quiet clock still running
    assert removes(world) == []
    world.fail_on = {("gh", "api")}
    assert tick(world, 5) == 1  # minute 30
    assert removes(world) == []
    assert quiet_state() == {}
    world.fail_on = set()
    assert main([]) == 0  # restarts cleanly, window begins again
    assert removes(world) == []


def test_a_failed_workspace_list_keeps_every_analysis_workspace(world):
    world.spaces["wY"] = {"label": "L", "panes": {"wY:p1": "term_y"}}
    cleanup.owned_dir().mkdir()
    (cleanup.owned_dir() / "y.json").write_text(json.dumps(
        {"kind": "analysis", "workspace": "wY", "label": "L", "cwd": "/x", "terminal": "term_y"}))
    world.fail_on = {("herdr", "workspace", "list")}
    for _ in range(13):
        assert tick(world, 5) == 1
    assert "wY" in world.spaces and (cleanup.owned_dir() / "y.json").exists()


# 12. recheck -----------------------------------------------------------------------

def test_a_change_on_the_recheck_keeps_the_worktree(world, capsys):
    window(world, runs=5)  # through minute 25
    world.flip_after = world.pane_reads + 3  # flips p1's screen only on the recheck pass
    assert tick(world, 5) == 0  # minute 30
    assert removes(world) == []
    assert "changed on the recheck" in capsys.readouterr().out


# 13. dry-run -------------------------------------------------------------------------

def test_dry_run_prints_would_remove_and_makes_no_call(world, capsys):
    window(world, argv=["--dry-run"])
    assert removes(world) == []
    assert "would remove" in capsys.readouterr().out


def test_dry_run_prints_would_warn_and_pages_no_one(world, capsys):
    world.status = " M a.py\n"
    window(world, argv=["--dry-run"])
    assert world.cards == {}
    assert "would warn the human" in capsys.readouterr().out


# 14. lock held -----------------------------------------------------------------------

def test_lock_held_returns_zero_and_makes_no_calls(world):
    with open(cleanup.lock_path(), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main([]) == 0
    assert world.calls == []


# 15. credentials ----------------------------------------------------------------------

def test_say_redacts_anything_shaped_like_a_github_token(capsys):
    cleanup.say("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and github_pat_11ABC_xyz found")
    out = capsys.readouterr().out
    assert "[redacted]" in out
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in out
    assert "github_pat_11ABC_xyz" not in out


# 16. malformed herdr entry -------------------------------------------------------------

def test_malformed_worktree_entry_is_reported_without_crashing(world, capsys):
    world.malformed_entry = {"branch": "muster/99", "is_linked_worktree": True, "open_workspace_id": "wZ"}
    assert main([]) == 1
    out = capsys.readouterr().out
    assert f"{REPO}: None: kept," in out
    assert world.path in quiet_state()  # the well-formed worktree was still evaluated


# 17. ad-hoc coding runs, and hand-written records ----------------------------

def test_an_ad_hoc_run_worktree_is_removed_from_its_run_registry(world):
    as_run(world)
    window(world)
    assert removes(world) == [["herdr", "worktree", "remove", "--workspace", "wZ"]]
    assert world.cards == {}


def test_an_unmerged_ad_hoc_run_is_kept_without_a_page(world):
    as_run(world)
    world.pulls[0]["merged_at"] = None  # open PR, HEAD pushed as its head
    window(world, runs=12)
    assert removes(world) == [] and world.cards == {}


def test_an_ad_hoc_run_whose_worktree_is_gone_is_skipped_silently(world, capsys):
    as_run(world)
    world.path = world.path + "-gone"  # run.json names a checkout herdr no longer lists
    window(world, runs=1)
    out = capsys.readouterr().out
    assert "fix/cleanup-thing" not in out and removes(world) == []


@pytest.mark.parametrize("name, branch, workspace", [
    ("wS-fix-merged-pane-cleanup", "fix/merged-pane-cleanup", "wS"),
    ("wX-fix-ad-hoc-herdr-wake", "fix/ad-hoc-herdr-wake", "wX"),
])
def test_a_hand_written_coding_record_makes_it_removable(world, name, branch, workspace):
    as_coding(world, branch, workspace, cleanup.owned_dir(), f"{name}.json")
    window(world)
    assert removes(world) == [["herdr", "worktree", "remove", "--workspace", workspace]]
    assert not (cleanup.owned_dir() / f"{name}.json").exists()  # its record goes with it


@pytest.mark.parametrize("mutate", [
    lambda w: setattr(w, "workspace", "wQ"),  # the right branch in another workspace
    lambda w: setattr(w, "listed_branch", "fix/other"),  # the recorded path, another branch
])
def test_a_coding_record_that_does_not_match_herdr_is_kept(world, mutate):
    as_coding(world, "fix/merged-pane-cleanup", "wS", cleanup.owned_dir(), "wS.json")
    mutate(world)
    window(world)
    assert removes(world) == [] and world.cards == {}


def test_worktrees_and_workspaces_without_a_record_are_never_touched(world):
    """Another person's fix/* worktree, wT and wY with no record: listed by herdr, never read."""
    as_run(world)
    world.extra = [{"branch": "fix/someone-else", "is_linked_worktree": True, "path": "/elsewhere/fix",
                    "open_workspace_id": "wT"}]
    world.spaces["wY"] = {"label": "Pinnacle-pressure-test", "panes": {"wY:p1": "term_y"}}
    window(world)
    assert removes(world) == [["herdr", "worktree", "remove", "--workspace", "wZ"]]
    assert not any(s in str(c) for c in world.calls for s in ("/elsewhere/fix", "wT", "wY:p1", "wtx"))


# 18. analysis workspaces (no PR) -----

def test_open_registers_an_analysis_workspace(world, monkeypatch, capsys):
    made = {"workspace": {"workspace_id": "w0", "label": "deal-review"},
            "root_pane": {"pane_id": "w0:p1", "terminal_id": "term_new"}}

    def run(argv):
        assert argv[:3] == ["herdr", "workspace", "create"] and "--no-focus" in argv, argv
        return json.dumps({"result": made})
    monkeypatch.setattr(core, "run", run)
    assert main(["open", "--cwd", "/tmp", "--label", "deal-review"]) == 0
    rec = json.loads((cleanup.owned_dir() / "term_new.json").read_text())
    assert rec["kind"] == "analysis" and (rec["workspace"], rec["terminal"], rec["label"]) == (
        "w0", "term_new", "deal-review")
    assert json.loads(capsys.readouterr().out)["pane"] == "w0:p1"


def _analysis(world, name, workspace, label, terminal):
    world.spaces[workspace] = {"label": label, "panes": {f"{workspace}:p1": terminal}}
    world.agent_status[f"{workspace}:p1"] = "done"
    cleanup.owned_dir().mkdir(exist_ok=True)
    (cleanup.owned_dir() / name).write_text(json.dumps(
        {"kind": "analysis", "workspace": workspace, "label": label, "cwd": "/x", "terminal": terminal}))


def test_a_hand_written_analysis_record_closes_after_a_quiet_window_with_no_pr_gate(world):
    world.removed = True  # no issue worktree in this world
    _analysis(world, "wY-pressure-test.json", "wY", "pressure-test", "term_y")
    window(world, runs=5)
    assert removes(world) == []
    tick(world, 5)
    assert removes(world) == [["herdr", "workspace", "close", "wY"]]
    assert not any(c[:2] == ["gh", "api"] for c in world.calls)
    assert not (cleanup.owned_dir() / "wY-pressure-test.json").exists()


@pytest.mark.parametrize("mutate", [
    lambda w: w.agent_status.__setitem__("wA:p1", "working"),  # active work
    lambda w: w.spaces["wA"].__setitem__("label", "renamed"),
    lambda w: w.spaces["wA"].__setitem__("panes", {"wA:p1": "term_other"}),  # herdr reused the id
])
def test_an_analysis_workspace_that_is_active_or_not_the_recorded_one_is_kept(world, mutate):
    world.removed = True
    _analysis(world, "a.json", "wA", "deal-review", "term_a")
    mutate(world)
    window(world, runs=12)
    assert removes(world) == [] and "wA" in world.spaces


def test_an_analysis_record_whose_workspace_is_gone_is_dropped(world):
    world.removed = True
    _analysis(world, "a.json", "wA", "deal-review", "term_a")
    del world.spaces["wA"]
    assert main([]) == 0
    assert not (cleanup.owned_dir() / "a.json").exists()


def test_dry_run_keeps_the_record_of_a_gone_workspace(world):
    world.removed = True
    _analysis(world, "a.json", "wA", "deal-review", "term_a")
    del world.spaces["wA"]
    assert main(["--dry-run"]) == 0
    assert (cleanup.owned_dir() / "a.json").exists()


def test_a_warning_card_that_cannot_be_archived_does_not_stall_cleanup(world):
    world.status = " M a.py\n"
    window(world)
    world.status = ""
    world.fail_on = {("hermes", "kanban", "--board", config.settings["board"], "show")}
    window(world)
    assert len(removes(world)) == 1


def test_an_analysis_workspace_that_changes_on_the_recheck_is_kept(world, capsys):
    world.removed = True
    _analysis(world, "y.json", "wY", "deal-review", "term_y")
    window(world, runs=5)
    world.flip_after = world.pane_reads + 1
    tick(world, 5)
    assert removes(world) == [] and "changed on the recheck" in capsys.readouterr().out


# 19. corrupt STATE ----------------------------------------------------------------------

@pytest.mark.parametrize("content", ["not json", "[]", json.dumps({"/old/format": {"since": 1}})])
def test_corrupt_or_old_state_is_treated_as_empty(world, content):
    cleanup.state_path().parent.mkdir(parents=True, exist_ok=True)
    cleanup.state_path().write_text(content)
    assert main([]) == 0
    assert world.path in quiet_state()


# 20. state write fails ----------------------------------------------------------------

def test_a_state_file_that_cannot_be_written_is_reported_not_raised(world, monkeypatch, capsys):
    blocker = cleanup.lock_path().parent / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(cleanup, "state_path", lambda: blocker / "cleanup.json")
    assert main([]) == 1
    assert "state not saved" in capsys.readouterr().out
    assert removes(world) == []


def test_dry_run_closes_nothing(world):
    window(world, argv=["--dry-run"])
    assert removes(world) == [] and world.cards == {}
    assert not any(c[:3] in (["herdr", "worktree", "remove"], ["herdr", "workspace", "close"]) for c in world.calls)


def test_a_prefixed_branch_without_a_card_is_not_ours_and_not_a_failure(world, capsys):
    (world.git_dir / core.CARD_FILE).unlink()
    world.listed_branch = "muster/foo"
    window(world)
    assert main([]) == 0
    assert removes(world) == [] and "failed" not in capsys.readouterr().out


def test_a_run_worktree_under_an_overlapping_prefix_is_still_cleaned_by_its_record(world, monkeypatch):
    monkeypatch.setitem(config.settings, "branch_prefix", "fix/cleanup-")
    as_run(world)
    window(world)
    assert removes(world) == [["herdr", "worktree", "remove", "--workspace", "wZ"]]
