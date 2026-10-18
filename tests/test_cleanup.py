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
import muster.decisions as decisions
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
        self.clone = config.repos()[REPO][0]
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
        self.pulls_out = None  # raw `gh api` output for the PR lookup instead of the slurped self.pulls
        self.issue = {"number": 15, "state": "open", "state_reason": None}
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
        self.cards, self.keys, self.blocks, self.subscribed, self.ledgers = {}, {}, {}, [], []
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

    def run(self, argv, env=None):
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
        if argv[0] == "git" and self.real_git:
            done = subprocess.run(argv, capture_output=True, text=True, env=env)
            if done.returncode:
                raise core.CommandError(f"{' '.join(argv[:4])}: exit {done.returncode}\n{done.stderr}")
            return done.stdout
        if argv[0] == "git" and argv[1] == "-C":
            assert argv[2] == self.path, argv
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
            if argv[3:] == ["ls-files", "-v", "-z"]:
                return ""  # no index flags; real-git tests cover them
            raise AssertionError(argv)
        if argv[:2] == ["gh", "api"] and "/pulls?head=" in argv[-1]:
            assert f"repos/{REPO}/pulls?head=o:{self.branch}&state=all&per_page=100" in argv[-1]
            assert "--paginate" in argv and "--slurp" in argv
            return self.pulls_out if self.pulls_out is not None else json.dumps([self.pulls])
        if argv[:2] == ["gh", "api"] and argv[-1] == f"repos/{REPO}/issues/15":
            return json.dumps(self.issue)
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
                    git(str(self.clone), "worktree", "remove", *(["--force"] if "--force" in argv else []), self.path)
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
                if argv[5] not in self.cards:
                    raise core.CommandError(f"hermes kanban: exit 1\nno such task: {argv[5]}")
                return json.dumps({"task": {"id": argv[5], "status": self.cards[argv[5]]["status"]},
                                   "comments": [{"body": b} for b in self.cards[argv[5]].get("comments", [])]})
            if verb == "comment":
                self.cards[argv[5]].setdefault("comments", []).append(argv[6])
                return ""
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
    monkeypatch.setattr(core, "subscribe", lambda card, ledger=None: (w.subscribed.append(card),
                                                                      w.ledgers.append(ledger)))
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
def test_unsaved_unfinished_work_is_kept_and_warns_the_human_once(world, dirty, unpushed, reason):
    world.status, world.unpushed = dirty, unpushed
    world.pulls[0].update(state="closed", merged_at=None)  # not finished: nothing is preserved or removed
    if unpushed != "0":
        world.head_sha = "abc123"
        world.pulls[0]["head"]["sha"] = "older"  # HEAD is past the PR head, on no remote ref
    window(world, runs=5)  # through minute 25: no page while the window runs
    assert world.cards == {}
    tick(world, 5)  # minute 30
    assert removes(world) == []
    [(card, data)] = world.cards.items()
    assert data["status"] == "blocked" and world.subscribed == [card] and world.ledgers == ["t_1"]  # its run's topic
    assert reason in data["reason"] and data["reason"].endswith("\nHerdr workspace: wR.")
    assert "Archive the run's ledger card t_1: cleanup then backs up the unsaved work" in data["reason"]
    assert "Nothing is removed." in data["reason"]  # committing and pushing unfinished work removes nothing
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
    world.status = "UU a.py\n"
    window(world)
    [first] = world.cards
    world.status = "UU a.py\n?? b.py\n"  # the state changed: old card archived, the window restarts
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
    world.status = "UU a.py\n"
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


def test_a_unique_file_beside_the_copies_is_preserved_with_them(world):
    real_checkouts(world)
    put(world.path, ".agents/skills/x/notes.md", "my work\n")
    window(world)
    assert removes(world) == [["herdr", "worktree", "remove", "--workspace", "wZ", "--force"]] and world.cards == {}
    [backup] = cleanup.backups_dir().iterdir()
    untracked = json.loads((backup / "manifest.json").read_text())["untracked"]
    assert ".agents/skills/x/notes.md" in untracked and "AGENTS.md" in untracked


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


@pytest.mark.parametrize("pr_base, removed", [("master", 1), ("main", 0)])
def test_the_merged_gate_compares_against_the_base_the_launch_recorded(world, pr_base, removed):
    world.card["base"] = "master"
    world._write_card()
    world.pulls[0]["base"]["ref"] = pr_base
    window(world)
    assert len(removes(world)) == removed


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
    world.status = "UU a.py\n"
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
    world.status = "UU a.py\n"
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


def test_the_head_repository_compares_case_insensitively(world):
    world.pulls[0]["head"]["repo"]["full_name"] = REPO.upper()
    window(world)
    assert len(removes(world)) == 1


# run topics (#24): a removal is the run's end, so its topic closes --------------------------------

def test_removing_an_issue_worktree_closes_its_runs_topic(world):
    from muster import conversation
    conversation.update("t_1", state="open", thread_id="41")
    window(world, runs=5)
    assert conversation.load("t_1")["state"] == "open"  # quiet 25 of 30 minutes: kept, still open
    tick(world, 5)
    assert len(removes(world)) == 1 and conversation.load("t_1")["state"] == "closing"


def test_removing_a_runs_worktree_closes_its_topic_and_a_dry_run_does_not(world):
    from muster import conversation
    as_run(world)
    conversation.update("t_run1", state="open", thread_id="41")
    window(world, argv=["--dry-run"])
    assert conversation.load("t_run1")["state"] == "open"
    window(world)
    assert len(removes(world)) == 1 and conversation.load("t_run1")["state"] == "closing"



# retire (#34): an archived run, or one whose issue was closed as not planned or a duplicate --------------------

def closed_unmerged(world, ledger="done"):
    """#496's shape: the run's only PR closed unmerged; its ledger card t_1 on the board."""
    world.pulls[0].update(state="closed", merged_at=None)
    world.cards["t_1"] = {"status": ledger, "comments": []}


def test_an_archived_run_whose_pr_closed_unmerged_is_removed_and_its_topic_finished(world):
    from muster import conversation
    closed_unmerged(world, "archived")
    conversation.update("t_1", state="open", thread_id="41")
    window(world)
    assert removes(world) == [["herdr", "worktree", "remove", "--workspace", "wR"]]
    assert conversation.load("t_1")["state"] == "closing"
    assert not any("--force" in c for c in world.calls)


def test_an_untapped_design_review_or_a_retrying_wake_does_not_hold_an_archived_run(world):
    closed_unmerged(world, "archived")
    (core.config.data_dir() / "decisions").mkdir(parents=True)
    (core.config.data_dir() / "decisions" / "r1.json").write_text(
        json.dumps({"ledger": "t_1", "kind": "design", "status": "open"}))  # advisory: nothing waits on it
    (runs.run_dir("t_1") / "outbox").mkdir(parents=True)  # nor a design wake still retrying
    (runs.run_dir("t_1") / "outbox" / "1-design.json").write_text("{}")
    window(world)
    assert removes(world) == [["herdr", "worktree", "remove", "--workspace", "wR"]]


@pytest.mark.parametrize("case", ["done", "blocked", "missing", "open-pr", "decision", "unreadable-decision",
                                  "outbox", "pulls-error", "pulls-not-json", "pulls-flat", "ps-fails", "other-workspace"])
def test_an_archived_run_is_kept_on_any_owner_or_unknown_evidence(world, capsys, case):
    closed_unmerged(world, "archived")
    if case in ("done", "blocked"):
        world.cards["t_1"]["status"] = case
    elif case == "missing":
        del world.cards["t_1"]
    elif case == "open-pr":
        world.pulls[0]["state"] = "open"
    elif case == "decision":
        (core.config.data_dir() / "decisions").mkdir(parents=True)
        (core.config.data_dir() / "decisions" / "r1.json").write_text(json.dumps({"ledger": "t_1", "status": "answered"}))
    elif case == "unreadable-decision":
        (core.config.data_dir() / "decisions").mkdir(parents=True)
        (core.config.data_dir() / "decisions" / "r1.json").write_text("{not json")
    elif case == "outbox":
        (runs.run_dir("t_1") / "outbox").mkdir(parents=True)
        (runs.run_dir("t_1") / "outbox" / "1.json").write_text("{}")
    elif case == "pulls-error":
        world.fail_on = {("gh", "api")}
    elif case == "pulls-not-json":
        world.pulls_out = "<html>rate limited</html>"
    elif case == "pulls-flat":
        world.pulls_out = json.dumps(world.pulls)  # not the slurped list of pages
    elif case == "ps-fails":
        world.fail_on = {("ps",)}
    elif case == "other-workspace":
        world.card["workspace"] = "wOTHER"  # herdr handed the id to another checkout
        world._write_card()
    codes = {main([])} | {tick(world, 5) for _ in range(6)}
    assert codes == ({1} if case in ("pulls-error", "ps-fails") else {0})  # a failed command is reported
    assert removes(world) == []
    assert world.cards.get("t_1", {}).get("status") != "archived" or case not in ("done", "blocked")


def test_an_archived_run_held_by_a_launch_or_recover_is_kept_and_its_quiet_clock_kept(world, capsys):
    closed_unmerged(world, "archived")
    (core.intake_dir() / "t_1").mkdir(parents=True)
    window(world, runs=5)
    with core.launch_lock(core.intake_dir() / "t_1"):
        tick(world, 5)
    assert removes(world) == [] and "a launch or recover holds the run" in capsys.readouterr().out
    tick(world, 5)
    assert len(removes(world)) == 1  # the quiet window was not restarted


def test_an_archived_run_with_work_it_cannot_preserve_warns_and_is_never_removed(world):
    closed_unmerged(world, "archived")
    world.status = "UU a.py\n"
    window(world)
    assert removes(world) == []
    assert [c["title"] for k, c in world.cards.items() if k != "t_1"] == ["Workspace kept · unsaved work in r"]
    [warning] = [c["reason"] for k, c in world.cards.items() if k != "t_1"]
    assert "Resolve that in the workspace" in warning and "Commit and push" not in warning


def test_unarchiving_the_ledger_during_the_window_keeps_the_worktree(world):
    closed_unmerged(world, "archived")
    window(world, runs=5)
    world.cards["t_1"]["status"] = "done"
    tick(world, 5)
    assert removes(world) == []


@pytest.mark.parametrize("reason", ["not_planned", "duplicate"])
def test_an_issue_closed_as_not_planned_or_duplicate_retires_the_run_then_removes_it(world, reason):
    closed_unmerged(world)
    world.issue.update(state="closed", state_reason=reason)
    core.save_json(core.intake_dir() / "t_1" / "launch.json", {"card": "t_1", "repo": REPO, "issue": 15, "event": 7,
                                                               "launch": {"step": "done"}})
    window(world)
    assert world.cards["t_1"]["status"] == "archived" and removes(world) == []
    [comment] = world.cards["t_1"]["comments"]
    assert comment.startswith("retired t_1:") and f"{REPO}#15 closed ({reason})" in comment
    assert "PR #16 closed unmerged" in comment and "branch kept" in comment
    retired = json.loads((core.intake_dir() / "t_1" / "launch.json").read_text())["retired"]
    assert retired["reason"] == reason
    window(world)  # a second quiet window under the archived gate
    assert len(removes(world)) == 1 and len(world.cards["t_1"]["comments"]) == 1
    from muster import hold
    assert hold.resolved(REPO, 15, 7)  # a reopen with the same approval never relaunches


@pytest.mark.parametrize("case", ["completed", "open-pr", "open-issue"])
def test_a_completed_closure_or_an_open_pr_retires_nothing(world, case):
    closed_unmerged(world)
    world.issue.update(state="closed", state_reason="not_planned")
    if case == "completed":
        world.issue["state_reason"] = "completed"
    elif case == "open-pr":  # #494's shape: its issue closed, its PR #497 the canonical one
        world.pulls[0]["state"] = "open"
    else:
        world.issue.update(state="open", state_reason=None)
    window(world)
    assert world.cards["t_1"]["status"] == "done" and world.cards["t_1"]["comments"] == []
    assert removes(world) == []


def test_a_failing_retire_archive_is_retried_and_comments_once(world):
    closed_unmerged(world)
    world.issue.update(state="closed", state_reason="not_planned")
    world.fail_on = {("hermes", "kanban", "--board", config.settings["board"], "archive")}
    window(world)
    assert world.cards["t_1"]["status"] == "done"
    world.fail_on = set()
    tick(world, 5)
    assert world.cards["t_1"]["status"] == "archived" and len(world.cards["t_1"]["comments"]) == 1


def test_a_stranded_run_gets_one_notice_to_archive_its_ledger(world):
    """#493's shape: its PR #496 closed unmerged, its issue still open (closed later by another branch's PR)."""
    closed_unmerged(world)
    window(world)
    window(world)
    notices = [c for k, c in world.cards.items() if k != "t_1"]
    assert len(notices) == 1 and notices[0]["title"] == "Workspace kept · pull request closed in r"
    assert "PR #16 closed unmerged" in notices[0]["body"] and "archive ledger t_1" in notices[0]["body"]
    assert removes(world) == [] and world.cards["t_1"]["status"] == "done"
    world.cards["t_1"]["status"] = "archived"  # the human retires it
    window(world)
    assert len(removes(world)) == 1 and notices[0]["status"] == "archived"


def test_a_dry_run_retires_nothing(world):
    closed_unmerged(world)
    world.issue.update(state="closed", state_reason="not_planned")
    window(world, argv=["--dry-run"])
    assert world.cards["t_1"] == {"status": "done", "comments": []} and removes(world) == []


# vanished (#33): a done run whose worktree is already gone, removed by anyone, is retired too -------------------

def vanished(world, adhoc=False, state="open"):
    """#480's shape: ledger done, no pull request, the checkout gone from herdr and disk, its topic still open."""
    from muster import conversation
    ledger = "t_1"
    if adhoc:
        as_run(world)
        ledger = "t_run1"
    else:
        core.save_json(core.intake_dir() / "t_1" / "launch.json", {
            "card": "t_1", "repo": REPO, "issue": 15, "event": 7, "launch": {
                "clone": str(world.clone), "branch": world.branch, "base": "main", "path": world.path,
                "workspace": "wR", "pane": "wR:p2", "step": "done"}})
    world.removed, world.pulls = True, []
    world.cards[ledger] = {"status": "done", "comments": []}
    conversation.update(ledger, state=state, chat_id="c", thread_id="624", cards=[])
    return ledger


@pytest.mark.parametrize("adhoc,state", [(False, "open"), (False, "closed"), (True, "open")])
def test_a_done_run_whose_worktree_is_gone_is_retired_once(world, adhoc, state):
    ledger = vanished(world, adhoc, state)
    assert main([]) == 0
    assert world.cards[ledger]["status"] == "archived" and removes(world) == []
    [comment] = world.cards[ledger]["comments"]
    assert comment.startswith(f"retired {ledger}:") and world.path in comment and "branch kept" in comment
    assert tick(world, 5) == 0 and len(world.cards[ledger]["comments"]) == 1
    if not adhoc:
        from muster import hold
        assert hold.resolved(REPO, 15, 7)  # a reopen with the same approval never relaunches


def test_a_dry_run_retires_no_vanished_run(world, capsys):
    vanished(world)
    assert main(["--dry-run"]) == 0
    assert world.cards["t_1"] == {"status": "done", "comments": []}
    assert "would retire ledger t_1" in capsys.readouterr().out


@pytest.mark.parametrize("case", ["blocked", "ready", "missing", "listed", "on-disk", "list-fails", "open-pr",
                                  "pulls-not-json", "decision", "outbox", "locked", "no-record", "never-launched",
                                  "fallback", "deleted"])
def test_a_vanished_run_is_kept_on_any_owner_or_unknown_evidence(world, case):
    from muster import conversation
    vanished(world)
    if case in ("blocked", "ready"):
        world.cards["t_1"]["status"] = case
    elif case == "missing":
        del world.cards["t_1"]
    elif case == "listed":  # still a checkout herdr knows, even with no open workspace
        world.removed, world.no_workspace = False, True
    elif case == "on-disk":
        Path(world.path).mkdir()
    elif case == "list-fails":
        world.fail_on = {("herdr", "worktree", "list")}
    elif case == "open-pr":
        world.pulls = [dict(world._pr(), state="open", merged_at=None)]
    elif case == "pulls-not-json":
        world.pulls_out = "<html>rate limited</html>"
    elif case == "decision":
        (config.data_dir() / "decisions").mkdir(parents=True)
        (config.data_dir() / "decisions" / "r1.json").write_text(json.dumps({"ledger": "t_1", "status": "open"}))
    elif case == "outbox":
        (runs.run_dir("t_1") / "outbox").mkdir(parents=True)
        (runs.run_dir("t_1") / "outbox" / "1.json").write_text("{}")
    elif case == "no-record":
        (core.intake_dir() / "t_1" / "launch.json").unlink()
    elif case == "never-launched":  # a launch that failed before it made the worktree
        core.save_json(core.intake_dir() / "t_1" / "launch.json", {"card": "t_1", "repo": REPO, "issue": 15, "launch": {
            "clone": str(world.clone), "branch": world.branch, "path": world.path, "workspace": None}})
    elif case in ("fallback", "deleted"):
        conversation.update("t_1", state=case)
    if case == "locked":
        with core.launch_lock(core.intake_dir() / "t_1"):
            code = main([])
    else:
        code = main([])
    assert code == (1 if case == "list-fails" else 0)  # a failed command is reported
    assert world.cards.get("t_1", {}).get("status") != "archived" and not world.cards.get("t_1", {}).get("comments")
    assert removes(world) == []


@pytest.mark.parametrize("bad", ["{oops", "[]"])
def test_an_unreadable_record_keeps_the_run_and_the_pass_goes_on(world, bad):
    vanished(world)
    (core.intake_dir() / "t_1" / "launch.json").write_text(bad)
    assert main([]) == 1  # reported, and the state file still saved
    assert world.cards["t_1"]["status"] == "done" and cleanup.state_path().is_file()


def test_a_run_that_changes_before_the_lock_is_checked_again_under_it(world, monkeypatch):
    import contextlib
    vanished(world)
    real = cleanup.ledger_lock

    @contextlib.contextmanager
    def flip(ledger):
        world.cards[ledger]["status"] = "blocked"  # the human reopened it between the check and the lock
        with real(ledger):
            yield
    monkeypatch.setattr(cleanup, "ledger_lock", flip)
    assert main([]) == 0
    assert world.cards["t_1"] == {"status": "blocked", "comments": []}


# resolved work (lifecycles): the topic closes on the merge, whatever keeps the worktree ---------------------------

def resolvable(world, state="open"):
    """Issue 15's run, its ledger done, its PR merged, its worktree still checked out, its topic in `state`."""
    from muster import conversation
    core.save_json(core.intake_dir() / "t_1" / "launch.json", {
        "card": "t_1", "repo": REPO, "issue": 15, "event": 7, "launch": {
            "clone": str(world.clone), "branch": world.branch, "base": "main", "path": world.path,
            "workspace": "wR", "pane": "wR:p2", "step": "done"}})
    world.cards["t_1"] = {"status": "done", "comments": []}
    conversation.update("t_1", state=state, chat_id="c", thread_id="790", cards=[])


def topic(ledger="t_1"):
    from muster import conversation
    return conversation.load(ledger)


@pytest.mark.parametrize("blocker", ["unpreservable", "agent-working", "child", "ownership", "reused-id"])
def test_resolved_work_closes_its_topic_and_keeps_a_blocked_worktree(world, blocker):
    resolvable(world)
    if blocker == "unpreservable":
        world.status = "UU a.py\n"
    elif blocker == "agent-working":
        world.agent_status["wR:p2"] = "working"
    elif blocker == "child":
        world.extra_ps = [(77, world._shell("wR:p1"), "python serve.py")]
    elif blocker == "ownership":
        world.card["worktree"] = "/elsewhere"
        world._write_card()
    elif blocker == "reused-id":  # herdr gave the run's workspace id to another workspace
        world.workspace = "wR2"
    assert main([]) == 0
    ref = topic()
    assert ref["state"] == "closing" and ref["resolved_at"] == BASE and removes(world) == []
    window(world)
    assert removes(world) == [] and topic()["state"] == "closing"


def test_an_open_pull_request_is_still_in_review_and_keeps_the_topic_open(world):
    resolvable(world)
    world.pulls = [dict(world._pr(), state="open", merged_at=None)]
    window(world)
    assert topic()["state"] == "open" and "resolved_at" not in topic()


def test_a_newer_pull_request_closed_unmerged_does_not_resolve(world):
    resolvable(world)
    world.pulls.append(dict(world._pr(number=20), state="closed", merged_at=None))
    assert main([]) == 0
    assert topic()["state"] == "open"


@pytest.mark.parametrize("status", ["ready", "blocked", "archived"])
def test_only_a_done_ledger_resolves_here(world, status):
    resolvable(world)
    world.cards["t_1"]["status"] = status
    assert main([]) == 0
    assert topic()["state"] == "open" and "resolved_at" not in topic()


def test_resolving_ends_the_saved_approvals_but_not_the_agents_question(world):
    resolvable(world)
    ids = {kind: decisions.create(kind, "t_1")["id"] for kind in ("build", "feedback", "design", "question")}
    other = decisions.create("build", "t_other")["id"]
    assert main([]) == 0
    got = {kind: decisions.load(rid)["status"] for kind, rid in ids.items()}
    assert got == {"build": "stale", "feedback": "stale", "design": "stale", "question": "open"}
    assert decisions.load(ids["build"])["outcome"] == "The work is resolved: PR #16 merged"
    assert decisions.load(other)["status"] == "open"


def test_a_human_closed_topic_is_marked_resolved_and_stays_closed(world):
    resolvable(world, state="closed")
    assert main([]) == 0
    assert topic()["state"] == "closed" and topic()["resolved_at"] == BASE


def test_resolving_is_idempotent_and_a_dry_run_changes_nothing(world, capsys):
    resolvable(world)
    rid = decisions.create("build", "t_1")["id"]
    assert main(["--dry-run"]) == 0
    assert topic()["state"] == "open" and decisions.load(rid)["status"] == "open"
    assert "would resolve: PR #16 merged" in capsys.readouterr().out
    assert main([]) == 0 and tick(world, 5) == 0
    assert topic()["resolved_at"] == BASE  # the first pass's time stays


def test_a_resolved_runs_unsaved_work_warning_goes_to_the_main_chat(world):
    resolvable(world)
    world.status = "UU a.py\n"
    window(world)
    assert world.subscribed and world.ledgers == [None]


def test_a_merged_run_whose_worktree_is_gone_closes_then_retires(world):
    vanished(world)
    world.pulls = [world._pr()]
    assert main([]) == 0
    assert topic()["state"] == "closing" and world.cards["t_1"]["status"] == "done"
    from muster import conversation
    conversation.swap("t_1", ("closing",), state="closed")  # the gateway confirmed the close
    assert tick(world, 5) == 0
    assert world.cards["t_1"]["status"] == "archived"


def test_two_runs_resolve_independently(world):
    resolvable(world)
    from muster import conversation
    conversation.update("t_2", state="open", chat_id="c", thread_id="791", cards=[])
    world.cards["t_2"] = {"status": "ready", "comments": []}  # still running
    assert main([]) == 0
    assert topic()["state"] == "closing" and topic("t_2")["state"] == "open"


# cleanup overdue: a resolved run's worktree kept a day later gets one card per blocker -------------------------

def overdue_cards(world):
    return {k: v for k, v in world.cards.items() if k.startswith("t_warn")}


def test_a_blocked_worktree_is_escalated_once_a_day_after_resolution(world):
    resolvable(world)
    world.agent_status["wR:p2"] = "working"
    assert main([]) == 0
    tick(world, 60 * 23)
    assert overdue_cards(world) == {}
    tick(world, 61)
    [(card, rec)] = overdue_cards(world).items()
    assert "Cleanup overdue" in rec["body"] and "pane wR:p2: claude is working" in rec["body"]
    assert "ledger t_1" in rec["body"] and world.path in rec["body"] and world.ledgers == [None]
    assert world.blocks == {card: 1}
    for _ in range(5):
        tick(world, 5)
    assert list(overdue_cards(world)) == [card] and world.blocks == {card: 1}  # deduplicated
    assert topic()["state"] == "closing"  # never reopened


def test_a_new_blocker_replaces_the_overdue_card_and_a_cleared_one_archives_it(world):
    resolvable(world)
    world.agent_status["wR:p2"] = "working"
    assert main([]) == 0
    tick(world, 60 * 25)
    [first] = overdue_cards(world)
    world.agent_status["wR:p2"] = "idle"
    world.extra_ps = [(77, world._shell("wR:p1"), "python serve.py")]
    tick(world, 5)
    assert world.cards[first]["status"] == "archived" and len(overdue_cards(world)) == 2
    second = max(overdue_cards(world))
    world.extra_ps = []
    tick(world, 5)
    assert world.cards[second]["status"] == "archived"


def test_an_overdue_card_for_an_ownership_mismatch_names_the_run_by_path(world):
    resolvable(world)
    world.card["worktree"] = "/elsewhere"
    world._write_card()
    assert main([]) == 0
    tick(world, 60 * 25)
    [rec] = overdue_cards(world).values()
    assert "ledger t_1" in rec["body"] and "card names" in rec["body"]


def test_an_unresolved_runs_blocker_is_never_escalated_and_a_dry_run_warns_nobody(world, capsys):
    resolvable(world)
    world.agent_status["wR:p2"] = "working"
    world.pulls = [dict(world._pr(), state="open", merged_at=None)]
    assert main([]) == 0
    tick(world, 60 * 48)
    assert overdue_cards(world) == {}
    world.pulls = [world._pr()]
    assert main([]) == 0  # resolved now
    world.t[0] += 86400 + 60
    assert main(["--dry-run"]) == 0
    assert overdue_cards(world) == {} and "would warn the human" in capsys.readouterr().out


def test_an_overdue_card_for_a_merge_gate_stays_until_the_gate_clears(world):
    resolvable(world)
    world.pulls[0]["base"]["ref"] = "other"  # a gate, not a NotReady: "PR #16 is not merged from ... into main"
    assert main([]) == 0
    tick(world, 60 * 25)
    window(world)  # the gate shows only once the quiet window has passed
    [card] = overdue_cards(world)
    for _ in range(4):
        tick(world, 5)
    assert world.cards[card]["status"] == "blocked" and list(overdue_cards(world)) == [card]
    world.pulls[0]["base"]["ref"] = "main"
    tick(world, 5)
    assert world.cards[card]["status"] == "archived"


def test_a_resolved_runs_topic_reopened_by_a_request_closes_again_but_a_human_resume_stays(world):
    from muster import conversation
    resolvable(world)
    assert main([]) == 0
    conversation.update("t_1", state="open")  # reopened for a late request, or remade
    tick(world, 5)
    assert topic()["state"] == "closing"
    conversation.update("t_1", state="open", resumed=True)  # the human reopened it
    tick(world, 5)
    assert topic()["state"] == "open"


# preserved removal: finished work kept only by unsaved state is backed up, verified, then removed ------------------

ME = ["-c", "user.name=t", "-c", "user.email=t@t"]


def finished_with_work(world):
    """A real run worktree whose PR merged, then: a commit past the PR head pushed and its remote branch deleted,
    staged and unstaged edits to one file, a binary file staged then changed, a tracked deletion, a rename, tracked and
    untracked symlinks, untracked files in a new directory, and an ignored .env and node_modules."""
    main = real_checkouts(world)
    wt, origin = Path(world.path), Path(world.path).parent / "origin.git"
    git(wt.parent, "init", "-q", "--bare", str(origin))
    git(main, "remote", "add", "origin", str(origin))
    git(main, "push", "-q", "origin", "main")
    put(wt, ".gitignore", ".env\nnode_modules/\n")
    put(wt, "f.txt", "a\nb\n")
    put(wt, "gone.txt", "old\n")
    put(wt, "ren.txt", "r\n")
    Path(wt, "link").symlink_to("f.txt")
    git(wt, "add", ".gitignore", "f.txt", "gone.txt", "ren.txt", "link")
    git(wt, *ME, "commit", "-qm", "the merged work")
    world.head_sha = world.pulls[0]["head"]["sha"] = git(wt, "rev-parse", "HEAD").strip()
    put(wt, "f.txt", "a\nb\nc\n")
    git(wt, *ME, "commit", "-qam", "after the merge")
    git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{world.branch}")
    git(wt, "push", "-q", "origin", f":refs/heads/{world.branch}")  # no remote ref holds it any more
    put(wt, "f.txt", "a\nb\nc\nstaged\n")
    git(wt, "add", "f.txt")
    put(wt, "f.txt", "a\nb\nc\nstaged\nunstaged\n")
    Path(wt, "bin.dat").write_bytes(b"\x00\x01\xff")
    git(wt, "add", "bin.dat")
    Path(wt, "bin.dat").write_bytes(b"\x00\x01\xff\x00\x02")
    git(wt, "rm", "-q", "gone.txt")
    git(wt, "mv", "ren.txt", "ren2.txt")
    put(wt, "notes.txt", "my notes\n")
    put(wt, "d/new.txt", "new\n")
    Path(wt, "link2").symlink_to("bin.dat")
    put(wt, ".env", "SECRET=1\n")
    put(wt, "node_modules/m.js", "x\n")
    return main, origin


def state(path):
    """Everything a restore must give back, read through git and the files themselves."""
    return (git(path, "status", "--porcelain", "--untracked-files=all"), git(path, "diff", "--cached", "--binary"),
            git(path, "diff", "--binary"), git(path, "log", "--format=%H"), git(path, "branch", "--show-current"),
            Path(path, "bin.dat").read_bytes(), Path(path, "notes.txt").read_text(), Path(path, "d/new.txt").read_text(),
            os.readlink(Path(path, "link2")), os.readlink(Path(path, "link")))


def backup_of(world):
    [backup] = [b for b in cleanup.backups_dir().iterdir() if not b.name.startswith(".")]
    return backup, json.loads((backup / "manifest.json").read_text())


def test_finished_work_is_preserved_removed_and_restored_without_the_clone(world, capsys):
    main, origin = finished_with_work(world)
    before = state(world.path)
    window(world)
    assert removes(world) == [["herdr", "worktree", "remove", "--workspace", "wZ", "--force"]] and world.cards == {}
    assert not Path(world.path).exists() and git(main, "branch", "--list", world.branch).strip()  # the branch stays
    assert not git(main, "for-each-ref", cleanup.REFS).strip()  # its temporary refs are gone
    backup, manifest = backup_of(world)
    assert str(backup) in capsys.readouterr().out
    assert (manifest["worktree"], manifest["branch"], manifest["head"]) == (world.path, world.branch,
                                                                            before[3].split()[0])
    assert {"notes.txt", "d/new.txt", "link2"} <= set(manifest["untracked"])
    assert not any(".env" in rel or "node_modules" in rel for rel in manifest["untracked"])
    assert sorted(p.name for p in backup.iterdir()) == ["manifest.json", "repo.bundle"]
    shutil.rmtree(main)
    shutil.rmtree(origin)  # restoring needs neither the clone's objects nor a remote
    subprocess.run(manifest["restore"], shell=True, check=True, capture_output=True)
    assert state(world.path) == before
    assert not Path(world.path, ".env").exists() and not Path(world.path, "node_modules").exists()
    every = git(world.path, "rev-list", "--all")
    assert not any(name in (".env", "m.js") for c in every.split()
                   for name in (Path(f).name for f in git(world.path, "ls-tree", "-r", "--name-only", c).split()))
    again = subprocess.run(manifest["restore"], shell=True, capture_output=True)  # the destination exists now
    assert again.returncode != 0 and state(world.path) == before


def test_head_past_the_merged_head_with_everything_pushed_is_preserved(world):
    real_checkouts(world)  # muster-112's shape: only scaffolding copies, HEAD not the newest merged PR's head
    world.pulls.append(world._pr(number=30, sha="newer", merged_at="2026-09-28T17:30:00Z"))
    window(world)
    assert removes(world)[0][-1] == "--force"
    _, manifest = backup_of(world)
    assert manifest["head"] == world.head_sha


def test_local_commits_alone_restore(world):
    main = real_checkouts(world)
    for rel in COPIES + [".codex/config.toml"]:
        Path(world.path, rel).unlink()
    put(world.path, "app.py", "print(2)\n")
    git(world.path, *ME, "commit", "-qam", "local only")
    head = git(world.path, "rev-parse", "HEAD").strip()
    window(world)
    _, manifest = backup_of(world)
    assert manifest["untracked"] == []
    shutil.rmtree(main)
    subprocess.run(manifest["restore"], shell=True, check=True, capture_output=True)
    assert git(world.path, "rev-parse", "HEAD").strip() == head and not git(world.path, "status", "--porcelain")


def test_an_archived_run_with_unsaved_work_is_preserved(world):
    real_checkouts(world)
    world.pulls[0].update(state="closed", merged_at=None)
    world.cards["t_run1"] = {"status": "archived", "comments": []}
    put(world.path, "notes.txt", "mine\n")
    window(world)
    assert removes(world)[0][-1] == "--force" and "notes.txt" in backup_of(world)[1]["untracked"]


def test_a_failed_backup_keeps_the_worktree_and_leaves_nothing(world):
    main, _ = finished_with_work(world)
    world.fail_on = {("git", "-C", world.path, "bundle")}
    window(world)
    assert world.removed is False and Path(world.path, "notes.txt").exists()
    assert list(cleanup.backups_dir().iterdir()) == [] and not git(main, "for-each-ref", cleanup.REFS).strip()
    world.fail_on = set()
    window(world)  # the next window tries again
    assert world.removed and backup_of(world)


def test_a_bundle_that_does_not_read_back_keeps_the_worktree(world, monkeypatch, capsys):
    finished_with_work(world)
    real = world.run

    def wrong(argv, env=None):
        out = real(argv, env)
        return "0" * 40 + "\n" + out if argv[-1].endswith("wip^3^{tree}") else out
    monkeypatch.setattr(core, "run", wrong)
    window(world)
    assert world.removed is False and list(cleanup.backups_dir().iterdir()) == []
    assert "the bundle does not read back" in capsys.readouterr().out


def test_a_change_right_after_the_backup_keeps_the_worktree_and_the_backup(world, monkeypatch, capsys):
    finished_with_work(world)
    real = os.replace

    def write_then_replace(src, dst):
        real(src, dst)
        if Path(dst).parent == cleanup.backups_dir():  # not the state file's replace
            put(world.path, "late.txt", "written during the backup\n")
    monkeypatch.setattr(cleanup.os, "replace", write_then_replace)
    window(world)
    assert world.removed is False and Path(world.path, "late.txt").exists()
    assert backup_of(world) and "changed right after preservation" in capsys.readouterr().out


def test_cleanup_interrupted_after_the_backup_preserves_again_and_removes(world):
    finished_with_work(world)
    world.fail_on = {("herdr", "worktree", "remove")}  # killed between the backup and the remove
    window(world)
    assert world.removed is False and len(list(cleanup.backups_dir().iterdir())) == 1
    world.fail_on = set()
    window(world)
    assert world.removed and len(list(cleanup.backups_dir().iterdir())) == 2


def conflict(world, main):
    put(main, "app.py", "print(2)\n")
    git(main, *ME, "commit", "-qam", "main moved")
    put(world.path, "app.py", "print(3)\n")
    git(world.path, *ME, "commit", "-qam", "mine")
    subprocess.run(["git", "-C", world.path, *ME, "merge", "-q", "main"], capture_output=True)


@pytest.mark.parametrize("case, reason", [
    ("conflict", "app.py has a merge conflict"),
    ("intent-to-add", "x.txt is only marked to add"),
    ("nested-repo", "emb/ is a repository inside the checkout"),
    ("submodules", "the checkout has submodules"),
])
def test_what_cannot_be_preserved_keeps_the_worktree_and_says_why(world, case, reason):
    main = real_checkouts(world)
    if case == "conflict":
        conflict(world, main)
    elif case == "intent-to-add":
        put(world.path, "x.txt", "x\n")
        git(world.path, "add", "-N", "x.txt")
    elif case == "nested-repo":
        git(world.path, "init", "-q", "emb")
        put(world.path, "emb/z", "z\n")
    else:
        put(world.path, ".gitmodules", '[submodule "s"]\n\tpath = s\n')
    window(world)
    assert world.removed is False and not cleanup.backups_dir().exists()
    [(card, data)] = world.cards.items()
    assert f"cannot preserve: {reason}" in data["reason"]


@pytest.mark.parametrize("flag, tag", [("--assume-unchanged", "h"), ("--skip-worktree", "S")])
@pytest.mark.parametrize("trigger", ["untracked", "head"])
def test_edits_git_status_hides_keep_the_worktree_and_its_index_flags(world, flag, tag, trigger):
    real_checkouts(world)  # issue #49: git status and `git add -u` skip a flagged file, so the backup would miss it
    git(world.path, "update-index", flag, "app.py")
    put(world.path, "app.py", "UNSAVED IMPORTANT WORK\n")
    if trigger == "untracked":
        put(world.path, "notes.txt", "mine\n")
    else:  # preservation chosen only because HEAD is not the newest merged head; porcelain looks clean
        world.pulls.append(world._pr(number=30, sha="newer", merged_at="2026-09-28T17:30:00Z"))
    window(world)
    assert world.removed is False and not cleanup.backups_dir().exists()
    [(card, data)] = world.cards.items()
    assert "cannot preserve: app.py is marked" in data["reason"]
    assert git(world.path, "ls-files", "-v", "app.py").startswith(f"{tag} ")
    assert Path(world.path, "app.py").read_text() == "UNSAVED IMPORTANT WORK\n"


@pytest.mark.parametrize("owner", ["question", "outbox"])
def test_an_unfinished_approval_or_event_keeps_finished_work(world, owner):
    finished_with_work(world)
    if owner == "question":
        decisions.root().mkdir(parents=True, exist_ok=True)
        (decisions.root() / "q1.json").write_text(json.dumps({"id": "q1", "kind": "question", "ledger": "t_run1",
                                                                "status": "open"}))
    else:
        (runs.run_dir("t_run1") / "outbox").mkdir(parents=True)
        (runs.run_dir("t_run1") / "outbox" / "1-proposal.json").write_text("{}")
    window(world)
    assert world.removed is False and not cleanup.backups_dir().exists()


def test_dry_run_preserves_nothing(world, capsys):
    finished_with_work(world)
    window(world, argv=["--dry-run"])
    assert world.removed is False and not cleanup.backups_dir().exists()
    assert "after preserving it" in capsys.readouterr().out
