# Plan: herdr-core (hc) as an optional terminal host

**Goal:** muster runs every route (issue runs, ad-hoc runs, `hermes muster open` analysis workspaces, holds,
decisions, cleanup) on a third host, `host: herdr-core`. That host is the Herdr desktop app's terminal core
(`hc`). herdr (now called "herdr TUI" in docs) stays the default, and orca is unchanged.

**Design:** `docs/decisions/2026-11-19-herdr-core-host.md` (issue #79)

**Branch:** `feat/add-herdr-core-hc-the-herdr-desktop-app`. This is the current worktree; do not create another.

**Verify the whole feature:**

```bash
PY="${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}"
PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q && hermes plugins validate .
HERMES_AGENT_DIR=~/.hermes/hermes-agent ~/.cache/muster-acceptance/venv/bin/python tests/acceptance/gate.py
HC_LIVE=1 PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q -s tests/test_herdr_core_live.py
```

## Deliberate departures from the design

These were decided while planning, from the live check below:
- **`open -g -a Herdr`, not bare `herdr-app`, opens a missing window.** `open` returns at once and does not
  steal focus. Bare `herdr-app` opens *or raises* the window, and how long it blocks is unknown.
- **Quiet covers every live hc terminal inside the checkout**, not only muster's labelled ones. That includes
  the shell that `space.new` opens with a space, and a person's terminals. Keeping a worktree because of
  someone else's terminal is the safe side.
- **The space whose folder is the checkout is closed on remove.** This holds even when a person opened that
  space, because the folder is about to be deleted and quiet has just passed for every terminal in it.
- **Untrusted checkouts are not trusted by muster.** They show Claude's trust dialog (`blocked`), which is
  launch readiness, a person-wait (learnings L30), as on Orca.
- **`hc send --wait --timeout 10`, not 30.** The timeout (seconds) only bounds how long a prompt call blocks.
  `started: true` arrives either way, and hold and decision prompts should not block for 30 s.
- **Quiet passes a checkout with no live terminal** (empty evidence). herdr and Orca raise there, but hc's remove
  is several steps, and a remove that fails at `git worktree remove` after ending the terminals must be retried
  on the next tick, not kept forever.
- **Terminals are created and then labelled into the space** (`hc label`), never created with the label. Only
  labelling a live terminal was checked live, and the reuse path makes the same call.
- **`agent()` requires a running foreground program** besides hc's status. Whether hc clears the status when
  Claude exits was not checked, and a stale status would let a brief be typed into a bare shell.

## Live check (done 2026-11-19, during planning, on this machine; hc 0.1.20)

hc was driven with hidden throwaway terminals, plus one `space.new` that was closed straight away. The fake and
the code below encode these facts. Do not re-spike them. Task 9 re-checks them live.

| Question | What hc does |
|---|---|
| Binaries | `hc` and `herdr-app` are in `~/.local/bin`, which `core.prepare_env` already puts on PATH. `open` is `/usr/bin/open`. |
| Errors | Exit non-zero, with `{"error": {"kind", "message", "hint"}}` on **stderr** (core.run keeps stderr). Kinds seen: `not_found` (exit 3), `name_taken` (exit 1), `blocked` (exit 9), `usage`. |
| `hc run --bg --json --no-parent --outlive-parent --keep --name N --cwd P --label k=v` (no program) | Starts a macOS login shell. Prints `{"address":"t4j","agent":"zsh","cwd":P,"name":N,"state":"live","turn":0}`. A name already live gives `name_taken`. |
| `hc get <name or address> --json` | `{address, argv, cwd, labels:{…}, name, pid (the root shell), state: live\|finished, status: null \| {app, state, msg?, kind?}, status_ms, turn, keep, exit_code, reason, …}`. `foreground: {argv, cwd, name, pid}` is **present only while a program runs** in the shell. An unknown name gives `not_found`. |
| Claude typed into the shell | `status.app = "claude-code"`. A fresh Claude reads `state: idle`. After a turn it reads `done`, and `turn` goes up by 1 per finished turn. Mid-turn it reads `working` (with `msg`). A trust dialog reads `{"state":"blocked","kind":"permission","msg":"trust this folder"}`. `foreground.name` is Claude's version (`2.1.328`); `foreground.argv[0]` is `claude`. `status_ms` changes with every status change. |
| `hc send --json -- <t> <text>` into a bare shell | Types the line and Enter: `{"started":null,"status":null,"turn":0}`. `--` before the positionals works. |
| `hc send --wait --timeout N --json -- <t> <text>` to Claude | `{"started":true,"status":<before>,"turn":n,"wait":{"status":"working"\|"done"\|…,"reason":"timeout"\|"status","ended":bool,"turn":…}}`. `started: true` is the turn proof. If Claude shows a dialog it is refused before typing: `blocked` error, exit 9. |
| `hc ls --json [--all] [--label k=v]` | `{"terminals":[{address, agent, cwd, name, state, turn}], "hidden_finished": n}`. Only live terminals are listed unless `--all`. Rows carry **no labels and no status**. |
| `hc label <t> k=v --json` on a live terminal | Adds the label and prints the terminal. `herdr.space=<id>` moves it into that space as a new tab at once. The app adds a `where` label. |
| `hc read <t>` (no `--json`) | Plain screen text, returned at once. |
| `hc kill <t> --json`, then `hc rm <t> --json` | kill prints the terminal (`state: finished`); rm prints `{"removed":"t4j"}`. rm of an unknown name gives `not_found`. |
| `herdr-app status` with a window | Exit 0 with JSON. **Not checked: exit code and output with no window** (Task 9, needs a human). |
| `herdr-app ls` | `{"spaces":[{id, name, folder, machine: null, active, tabs:[{id, name, terminals:[{terminal, pane, status, app, name}]}]}]}` |
| `herdr-app do space.new --name N --folder F` | `{"space":"sp…","tab":"tb…","terminal":"t4m"}`. It **opens a plain shell** with cwd F, listed by `hc ls`. |
| `herdr-app do space.close --id S --confirmed true` | `{"ok": true}`. It ends the space's terminals. |
| Core restart | **Not probed** (it would park every terminal on this machine, the planner's included). hc's manual: `--keep` terminals come back as a fresh shell and the program is never re-run. Task 9 lists it as a human check. |
| Idle Claude | `status_ms` and `turn` are unchanged over 5 s with no activity, so both are safe as `state_change_seq` and in quiet's evidence. |
| Name reuse | After `hc kill X` (no rm), `hc run --name X` succeeds. `hc get X` then returns the live terminal; `hc ls --all` lists both. |
| `--timeout` units | Seconds (`hc send --help`). |
| A turn that stops at a dialog after the brief was typed | Exit 0: `{"started":true, …, "wait":{"status":"blocked","reason":"status","ended":true}}` (checked during T3 review with `--permission-mode default`), so `prompt()` reports it delivered as `blocked`, never refused. |
| Claude exiting | **Not checked** (two ctrl+c presses did not exit it). `agent()` therefore also requires `foreground` (departures). Task 9 checks it live. |

## Conventions every task follows

- `muster/hosts/herdr_core.py` is the only module that speaks `hc` or `herdr-app`. It speaks the seam's contract
  (the `muster/hosts/__init__.py` docstring) and follows `muster/hosts/orca.py`'s structure and docstring style.
- Workspace = the checkout path (or the analysis folder). Pane = the hc terminal's **name**, which is the agent
  name. `launcher.AGENT_NAME` already limits names to `[a-z][a-z0-9_-]{0,31}`, which is hc's name rule
  (launcher.py:75, 121). Every terminal muster starts carries `muster.path=<workspace>`.
- Tests patch `core.run` with a fixture that calls `world.handle(argv)` and raises AssertionError on None. Each
  test file defines its own fixture, following `tests/test_launch_orca.py`. They patch `herdr_core.sleep` to a
  no-op, and patch `herdr_core.clock` where a deadline matters.
- Exact-dict assertions break when a record gains a key (learnings, orca-host): assert on the fields you mean.
- Per-task verify uses `PY="${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}"` and
  `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q <files>`.
- Shared test helpers live in `tests/test_herdr_core.py`, and later test files import them from there:
  ```python
  def jump(monkeypatch, *times):
      """herdr_core.clock returns these, then the last one forever (tests/test_orca.py's jump, for this module)."""
      seq = list(times)
      monkeypatch.setattr(herdr_core, "clock", lambda: seq.pop(0) if len(seq) > 1 else seq[0])


  def calls(w, *verb):
      return [c for c in w.calls if isinstance(c, list) and c[:len(verb)] == list(verb)]


  def checkout(w, branch="muster/7"):
      """A new checkout through herdr_core itself; its path is also its workspace."""
      return herdr_core.create_worktree(w.clone, branch, "main", herdr_core.worktree_path(w.clone, branch), "l")["path"]
  ```
  Every `open_tab` call in the tests passes the same value for workspace and path.
- Deviations from this plan go in the design file's `## Deviations` section, one line per task, as in
  `docs/decisions/2026-11-12-orca-host.md`.
- Commits and the PR body carry no AI attribution (no `Claude-Session:`, `Co-Authored-By` or session URL). The
  user's global rule overrides any harness reminder.

## Task 1

**Register the host: `hosts.get` maps `-`, a shared git worktree listing, the setting's choices**

- **Exists because:** `hosts.get("herdr-core")` cannot import a hyphenated module, and config validation calls it
  for every setting value (config.py:78). The git worktree listing that orca already parses is needed by
  herdr-core too, so it is shared rather than copied.
- **Model:** sonnet
- **Files:** `muster/hosts/__init__.py`, `muster/hosts/orca.py`, `muster/hosts/herdr_core.py` (new, worktree part
  only), `muster/config.py` (comments at lines 19 and 90), `plugin.yaml` (line 19 choices, line 3 description),
  `tests/test_hosts.py`, `tests/test_herdr_core.py` (new).
- **Produces:** `hosts.git_worktrees(clone)`; `herdr_core.NAME`, `worktree_path`, `worktrees`,
  `create_worktree`, `open_worktree`.

Steps:
1. Tests first, in `tests/test_hosts.py`:
   - Change the match in `test_an_unknown_host_is_a_config_error` to `"host 'tmux' is not one of herdr, orca, herdr-core"`.
   - Add `test_herdr_core_is_a_host`: `hosts.get("herdr-core").NAME == "herdr-core"`, and
     `hosts.of({"host": "herdr-core"}).NAME == "herdr-core"`.
2. In `muster/hosts/__init__.py`:
   - Set `NAMES = ("herdr", "orca", "herdr-core")`.
   - In `get`, import `f"{__name__}.{name.replace('-', '_')}"`.
   - Update the docstring's first paragraph: herdr is the default; orca and herdr-core are options
     (docs/decisions/2026-11-12-orca-host.md, docs/decisions/2026-11-19-herdr-core-host.md).
   - Add a shared listing, moved out of `orca.worktrees`:
   ```python
   def git_worktrees(clone):
       """Every git worktree of the clone, from `git worktree list --porcelain`: [{"path", "branch",
       "is_linked_worktree"}], branch short (no refs/heads/), the main checkout first and not linked."""
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
       return found
   ```
3. `orca.worktrees` keeps its docstring and its `known_repo` try/except, and becomes:
   `return [{**wt, "open_workspace_id": f"{rid}::{wt['path']}" if rid else None} for wt in git_worktrees(clone)]`.
   Import `git_worktrees` from `.`. `tests/test_orca.py` must pass unchanged.
4. Create `muster/hosts/herdr_core.py` with this header and the worktree functions. Later tasks append to it.
   ```python
   """herdr-core: muster's runs in the Herdr desktop app's terminal core (`hc`): a git checkout, an hc shell named
   after the agent, Claude typed into it, the brief sent with `hc send --wait`.

   Every shape here was checked live on 2026-11-19 (docs/plans/2026-11-19-herdr-core-host.md, "Live check"). The
   workspace is the checkout path and a pane is the hc terminal's name (the agent name): names are unique among live
   terminals, so a live name is the identity and a taken name is the crash-recovery check. Every terminal muster
   starts is labelled muster.path=<workspace>. A window shows a terminal only when it is labelled herdr.space=<id>;
   with no window muster opens one in the background, and when none comes up the run proceeds hidden.
   """

   import time

   from .. import core
   from . import git_worktrees, herdr

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
   ```
   Imports grow with the tasks that use them, so no task commits an unused import (ruff F401):
   - Task 3 adds `import json`, `import shlex`, `from pathlib import Path`, and `READY, Blocked, program` from `.`.
   - Task 4 adds `import hashlib`, and `QUIET, NotReady, helper, processes, under` from `.`.
   - Task 5 adds `import secrets`.

   The `from . import herdr` import is a sibling host module. That is fine, because `herdr.py` imports nothing
   from herdr_core.
5. `config.py:19`: change the comment to `# terminal host for new runs: herdr, orca or herdr-core (muster.hosts)`.
   `config.py:90`: change "both hosts" to "every host".
   `plugin.yaml:19`: set `choices: [herdr, orca, herdr-core]`.
   `plugin.yaml:3`: change "herdr or Orca" to "herdr, Orca or the Herdr app's core".
6. `tests/test_herdr_core.py`: create it with the fixture below. Until Task 2 lands it uses `OrcaWorld`; Task 2
   switches it to `HcWorld`. Add these tests:
   - `test_worktrees_are_open_at_their_path`: with `p = str(w.tmp / "wt" / "b")`, run `w.worktree(w.clone, "b", p)`.
     Then `herdr_core.worktrees(w.clone)` includes `{"path": p, "branch": "b", "open_workspace_id": p,
     "is_linked_worktree": True}`, and the main checkout's entry has `is_linked_worktree` False.
   - `test_create_makes_a_new_branch_from_origin`: the git call is
     `["git", "-C", str(clone), "worktree", "add", "--no-track", "-b", "feat/x", path, "origin/main"]`, and the
     return value is `{"workspace": path, "path": path}`.
   - `test_create_checks_out_an_existing_branch_as_it_is`: with `w.branches[(str(w.clone), "fix/x")] = "sha_x"`,
     the call is `["git", "-C", str(clone), "worktree", "add", path, "fix/x"]`.
   ```python
   @pytest.fixture
   def w(tmp_path, monkeypatch):
       world = OrcaWorld(tmp_path)   # Task 2: HcWorld

       def run(argv, env=None):
           out = world.handle(argv)
           if out is None:
               raise AssertionError(argv)
           return out
       monkeypatch.setattr(core, "run", run)
       monkeypatch.setattr(herdr_core, "sleep", lambda s: None)
       world.clone = tmp_path / "clone"
       world.clone.mkdir()
       return world
   ```
   Imports at the top of the test file: `import pytest`, `import muster.core as core`,
   `import muster.hosts.herdr_core as herdr_core`, `from tests.fake_orca import OrcaWorld`. OrcaWorld's `git`
   handles `worktree add <path> <branch>`, not the `--no-track -b` form, so until Task 2 the fixture uses this local
   subclass (`world = LocalWorld(tmp_path)`); Task 2 deletes it:
   ```python
   class LocalWorld(OrcaWorld):
       def git(self, where, args):
           if args[:4] == ["worktree", "add", "--no-track", "-b"]:
               self.worktree(where, args[4], args[5])
               return ""
           return super().git(where, args)
   ```

**Verify:** `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/test_hosts.py tests/test_herdr_core.py tests/test_orca.py tests/test_config.py`

## Task 2

**`tests/fake_herdr_core.py`: hc and herdr-app as the live check found them**

- **Exists because:** every later task tests against it. Real hc cannot run in CI, and its shapes (stderr errors,
  `foreground` only while a program runs, labels missing from `ls` rows) are exactly where a guess goes wrong.
- **Model:** sonnet
- **Files:** `tests/fake_herdr_core.py` (new); `tests/test_herdr_core.py` (switch the fixture to `HcWorld`, delete
  the Task 1 local subclass).
- **Produces:** `HcWorld(tmp_path)`, with knobs `window`, `opens`, `hc_start`, `hc_send`, `hc_after` and `hc_late`, and
  helpers `term`, `hc_claude`, `hc_live`, `hc_restart`. State lives in `hc`, `spaces`, `typed`, `submitted` and
  `procs`.

Complete code (adjust only if a test proves a line wrong, and record that as a deviation):

```python
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
```

Steps:
1. Write the file above.
2. Switch `tests/test_herdr_core.py`'s fixture to `HcWorld` and delete the Task 1 local subclass. Task 1's tests
   must still pass.
3. Add `test_the_fake_answers_like_the_live_check(w)`, calling `core.run` directly, which pins the fake against
   the table:
   - `core.run(["hc", "run", "--bg", "--no-parent", "--outlive-parent", "--keep", "--name", "a", "--cwd", "/x",
     "--json"])` twice: the second raises `core.CommandError` with `"name_taken"` in it;
   - `json.loads(core.run(["hc", "get", "a", "--json"]))` has no `"foreground"` key;
   - after `w.hc_claude(address, "blocked")`, `core.run(["hc", "send", "--wait", "--timeout", "10", "--json", "--",
     "a", "x"])` raises with `"blocked"`;
   - `json.loads(core.run(["hc", "ls", "--json"]))["terminals"][0]` has no `"labels"` key.

**Verify:** `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/test_herdr_core.py`

## Task 3

**Terminals, agent and delivery: `open_tab`, `pane`, `panes`, `agent`, `start_agent`, `prompt`**

- **Exists because:** these are how a launch puts Claude in a named hc shell, finds it again after a crash, and
  proves delivery of the brief. Without them `launcher.ensure` cannot run on herdr-core.
- **Model:** sonnet
- **Files:** `muster/hosts/herdr_core.py`, `tests/test_herdr_core.py`.
- **Consumes:** Task 2's `HcWorld`. **Produces** the seam functions named in the title, plus `up`, `window` and
  `space`, which Task 4 uses.

Append this code to `herdr_core.py`:

```python
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
```

`start_agent`'s second parameter is `kind_` because `kind` is the module's error helper. Callers pass it
positionally (launcher.py:343).

Tests first, in `tests/test_herdr_core.py`. Each must fail before the code exists. `path = checkout(w)` unless
stated otherwise:
- `test_open_tab_starts_a_labelled_shell_then_labels_it_into_a_new_space`: the `hc run` argv carries `--name run-x`,
  `--cwd <path>` and `--label muster.path=<path>`, and no `herdr.space` label. It is followed by
  `["hc", "label", "run-x", "herdr.space=<sid>", "--json"]`, and `w.spaces[sid]["folder"] == path`.
- `test_open_tab_reuses_the_space_already_open_on_the_folder`: put `w.spaces["sp9"] = {"name": "x", "folder": path}`
  first. No `space.new` call is made, and the label call names `herdr.space=sp9`.
- `test_open_tab_opens_a_window_when_none_is_up`: with `w.window = False`, the calls include
  `["open", "-g", "-a", "Herdr"]`, and the terminal ends up labelled into a space.
- `test_open_tab_runs_hidden_when_no_window_comes_up`: set `w.window = w.opens = False`, then
  `jump(monkeypatch, 0, 31)`. The terminal has no `herdr.space` label, `open_tab` returns `"run-x"`, and
  `core.log_path("hosts").read_text()` contains `"runs hidden"`.
- `test_open_tab_reuses_a_live_terminal_and_labels_it_into_a_space_later`: open with
  `w.window = w.opens = False` and `jump(monkeypatch, 0, 31)`, then set `w.window = True` and open again. Only one
  `hc run` call is made in total, and the second open makes one `hc label … herdr.space=<sid>` call (lesson 4).
- `test_open_tab_refuses_a_name_live_in_another_workspace`: `w.term("run-x", "/other", {"muster.path": "/other"})`,
  then `open_tab` raises `core.CommandError`.
- `test_agent_maps_hc_status_to_herdrs_shape`: `path, a = w.hc_live(w.clone, "muster/7", "run-x", state="done")`.
  Then `agent("run-x")` has `agent == "claude"`, `agent_status == "done"`, `workspace_id == path`,
  `pane_id == "run-x"` and `state_change_seq == w.hc[a]["status_ms"]`. After
  `w.hc[a]["status"]["state"] = "error"`, `agent_status == "hc-error"`.
- `test_agent_is_none_for_a_shell_a_finished_terminal_a_stale_status_and_after_a_core_restart`: four cases, each
  returning None:
  - `w.term(...)` with no Claude;
  - an `hc_live` terminal after `core.run(["hc", "kill", "run-x", "--json"])`;
  - an `hc_live` terminal with `w.hc[a]["foreground"] = None` (status left set);
  - an `hc_live` terminal after `w.hc_restart()`.
- `test_start_agent_types_env_and_claude_then_returns_when_ready`: `open_tab(path, path, "run", [], "run-x")`, then
  `start_agent("run-x", "claude", "run-x", ["--model", "opus"], ["A=1"])`. Then
  `w.typed == ["export A=1; claude --model opus"]`.
- `test_start_agent_returns_at_a_trust_dialog`: with `w.hc_start = "blocked"`, it returns, and
  `agent("run-x")["agent_status"] == "blocked"`.
- `test_start_agent_waits_for_a_booting_claude_without_typing`: `w.hc_late = 2`. Make `open_tab`, then
  `w.hc_claude(address, "idle")` (Claude already typed by a crashed launch, no status yet). `start_agent` returns,
  and `w.typed == []`.
- `test_start_agent_types_nothing_into_a_pane_running_something_else`: set the terminal's `foreground` to
  `{"argv": ["vim"], "pid": 1}`. It raises `core.CommandError`, and `w.typed == []`.
- `test_start_agent_times_out`: with `w.hc_start = "none"` and `jump(monkeypatch, 0, 121)`, it raises
  `core.CommandError`.
- `test_prompt_delivers_with_turn_proof`: on an `hc_live` terminal (idle), `prompt("run-x", "the brief")` returns
  `"working"`, `w.submitted == ["the brief"]`, and the last call is
  `["hc", "send", "--wait", "--timeout", "10", "--json", "--", "run-x", "the brief"]`.
- `test_prompt_on_a_dialog_raises_blocked_and_sends_nothing`: an `hc_live(..., state="blocked")` terminal raises
  `hosts.Blocked`, and `w.submitted == []`.
- `test_prompt_without_turn_proof_is_unproven`: `w.hc_send = "not_started"` raises `core.CommandError` that is not a
  `hosts.Blocked`.
- `test_prompt_to_a_bare_shell_or_a_stale_status_types_nothing`: a `w.term(...)` with no Claude, and an `hc_live`
  terminal with `foreground = None`. Each raises `core.CommandError`, and `w.typed == w.submitted == []`.
- `test_panes_lists_muster_and_other_terminals_in_the_checkout`: build an `hc_live` terminal at `path`, plus
  `w.term(None, path + "/sub")` and `w.term(None, "/elsewhere")`. `panes(path)` lists the first two; the Claude one
  has `agent == "claude"` and the other has `agent` None.

**Verify:** `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/test_herdr_core.py`

## Task 4

**Cleanup: `quiet` and `remove_argv`**

- **Exists because:** cleanup removes a merged run's worktree only after quiet holds twice
  (cleanup.py:475/529). The argv it then runs has to end the run's terminals and space as well as the checkout.
  Without these, herdr-core worktrees are kept forever or removed under a working agent.
- **Model:** sonnet
- **Files:** `muster/hosts/herdr_core.py`, `tests/test_herdr_core.py`.
- **Consumes:** `terminals`, `get`, `app`, `up`, `labels` (Task 3).

Append:

```python
def quiet(workspace):
    """Each terminal's evidence. NotReady when one is busy or unknown: an agent not idle or done, a program other
    than a QUIET one in a shell with no agent, or anything under a terminal's shell but its foreground program and
    MCP/language servers (a job started with &, an agent's background command). Terminals: every live one in the
    checkout, muster's or not (the space's own shell, a person's). None at all is quiet (empty evidence): nothing
    runs in hc there, whether a person closed them or a remove ended them and then failed at git (retried)."""
    rows = terminals(workspace)
    if not rows:
        return []
    table, seen = processes(), []
    for row in sorted(rows, key=lambda r: r["address"]):
        address = row["address"]
        t = get(address)
        if t is None:
            raise NotReady(f"terminal {address} ended during the check")
        if t.get("pid") not in table:
            raise NotReady(f"terminal {address}: its shell {t.get('pid')} is not running")
        status, fg = t.get("status") or {}, t.get("foreground") or {}
        tree = under(t["pid"], table)
        busy = [c for p, depth, c in tree if not (depth == 1 and p == fg.get("pid")) and not helper(c)]
        if busy:
            raise NotReady(f"terminal {address} runs in the background: {busy[0][:80]}")
        if app(t):
            if STATE.get(status.get("state")) not in READY:
                raise NotReady(f"terminal {address}: {app(t)} is {status.get('state')}")
        elif fg and program((fg.get("argv") or [""])[0]) not in QUIET:
            raise NotReady(f"terminal {address} runs {' '.join(fg.get('argv') or [])[:80]}")
        screen = core.run(["hc", "read", address])
        seen.append([address, t.get("name"), app(t), status.get("state"), t.get("turn"), t.get("status_ms"),
                     fg.get("argv"), hashlib.sha256(screen.encode()).hexdigest(), [p for p, _, _ in tree]])
    return seen


# ponytail: the space whose folder is the checkout is closed even when a person opened it, ending a terminal of
# theirs in it whose cwd is elsewhere (quiet only sees terminals in the checkout); scope to muster-opened spaces
# (a label on the space's own shell) if that bites.
def remove_argv(workspace, path, force):
    """One command, named now (quiet has just passed): close the checkout's space if a window shows one (it ends
    the terminals in it), end and remove muster's terminals in the workspace, then `git worktree remove` from the
    main clone; the branch stays. With no window a space cannot be closed; the window still lists it, pointing at
    a deleted folder, until a person closes it."""
    clone = str(Path(core.run(["git", "-C", path, "rev-parse", "--path-format=absolute",
                               "--git-common-dir"]).strip()).parent)
    spaces = [s["id"] for s in json.loads(core.run(["herdr-app", "ls"]))["spaces"]
              if s.get("folder") == path and not s.get("machine")] if up() else []
    mine = [t["address"] for t in hc("ls", "--all", "--label", f"{LABEL}={workspace}")["terminals"]]
    steps = [f"herdr-app do space.close --id {shlex.quote(s)} --confirmed true >/dev/null" for s in spaces]
    # hc rm fails if space.close already removed the terminal; one that hc no longer knows is removed as wanted.
    steps += [f"{{ hc kill {shlex.quote(a)} --json >/dev/null 2>&1; hc rm {shlex.quote(a)} --json >/dev/null 2>&1 "
              f"|| ! hc get {shlex.quote(a)} --json >/dev/null 2>&1; }}" for a in mine]
    steps.append(shlex.join(["git", "-C", clone, "worktree", "remove", *(["--force"] if force else []), path]))
    return ["sh", "-c", " && ".join(steps)]
```

Tests first. `path, a = w.hc_live(w.clone, "muster/7", "run-x", state="done")` unless stated otherwise:
- `test_quiet_passes_an_idle_agent_and_the_spaces_shell`: `herdr_core.space(path)` makes the space and its shell.
  `quiet(path)` returns two entries, and `json.dumps` of the result works.
- `test_quiet_is_the_same_twice_when_nothing_changed`: two calls are equal (the cleanup fingerprint depends on it).
- `test_quiet_refuses_a_working_agent` (`state="working"`) and `test_quiet_refuses_a_dialog` (`state="blocked"`):
  both raise `NotReady`.
- `test_quiet_refuses_a_background_job`: `w.procs[999] = (w.hc[a]["pid"], "sleep 100")` raises `NotReady`. A helper
  `w.procs[998] = (w.hc[a]["foreground"]["pid"], "node /x/foo-mcp")` alone does not.
- `test_quiet_refuses_a_plain_program_in_a_shell`: on `b = w.term(None, path)`, set
  `w.hc[b]["foreground"] = {"argv": ["vim"], "pid": 997}` and `w.procs[997] = (w.hc[b]["pid"], "vim")`. That raises
  `NotReady`. With `["lazygit"]` and `"lazygit"` it passes.
- `test_quiet_passes_a_checkout_with_no_terminals`: after `core.run(["hc", "kill", "run-x", "--json"])`,
  `quiet(path) == []`.
- `test_remove_closes_the_space_ends_terminals_and_removes_the_checkout`: `open_tab(path, path, "run", [], "run-x")`
  (the reuse path labels the terminal into a new space), then `argv = remove_argv(path, path, False)` and
  `w.handle(argv)`. Then:
  - `w.spaces == {}`;
  - no `w.hc` value has `labels["muster.path"] == path`;
  - `path not in w.worktrees`;
  - `argv[2].endswith(shlex.join(["git", "-C", str(w.clone), "worktree", "remove", path]))`.
- `test_remove_with_force_passes_force_to_git`: the script ends with `worktree remove --force <path>`.
- `test_remove_without_a_window_still_removes_terminals_and_checkout`: with `w.window = False`, the script has no
  `space.close`, no `["open", "-g", "-a", "Herdr"]` call is made (remove never opens a window), and the checkout is
  removed.
- `test_a_failed_git_remove_is_retried_from_quiet`: run the argv with `w.git` patched (monkeypatch on the
  instance) to raise `core.CommandError` for `worktree remove`. Then restore it. `quiet(path) == []`, and a second
  `remove_argv` run removes the checkout.

**Verify:** `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/test_herdr_core.py`

## Task 5

**Analysis workspaces: `open_workspace`, `analysis_gone`, `analysis_check`, `close_argv`**

- **Exists because:** `hermes muster open` (cleanup.py:1067) and its cleanup (cleanup.py:997, 1005, 528) call
  these on the configured host. Without them that route fails on herdr-core.
- **Model:** sonnet
- **Files:** `muster/hosts/herdr_core.py`, `tests/test_herdr_core.py`.

Append:

```python
def open_workspace(cwd, label):
    """A shell muster owns in a folder, in the folder's space. label: the record's; the terminal name is unique per
    open, so two opens of one folder stay apart (and is safe as a filename: cleanup names files after it)."""
    name = f"muster-{secrets.token_hex(4)}"
    open_tab(cwd, cwd, label, [], name)
    return {"workspace": cwd, "terminal": name, "pane": name}


def analysis_gone(rec):
    """Its terminal was closed by hand."""
    return get(rec["pane"]) is None


def analysis_check(rec):
    t = get(rec["pane"])
    if t is None:
        raise NotReady(f"terminal {rec['pane']} is gone")
    if labels(t).get(LABEL) != rec["workspace"]:
        raise NotReady(f"terminal {rec['pane']} is not muster's terminal for {rec['workspace']}")


# ponytail: the folder's space (and the shell space.new opened in it) outlives the analysis; a later open of the
# same folder reuses it, so there is at most one per folder.
def close_argv(rec):
    """End and remove muster's own terminal; the folder's space and its other terminals stay."""
    if get(rec["pane"]) is None:
        raise NotReady(f"terminal {rec['pane']} is gone")
    a = shlex.quote(rec["pane"])
    return ["sh", "-c", f"{{ hc kill {a} --json >/dev/null 2>&1; hc rm {a} --json >/dev/null 2>&1 "
                        f"|| ! hc get {a} --json >/dev/null 2>&1; }}"]
```

Tests first:
- `test_open_workspace_starts_a_named_shell_in_the_folders_space`: the returned `terminal == pane`, which matches
  `^muster-[0-9a-f]{8}$`, and `workspace == cwd`. The terminal is labelled `muster.path=cwd` and is in a space.
- `test_two_opens_of_one_folder_are_two_terminals_in_one_space`.
- `test_analysis_gone_after_the_terminal_is_killed`.
- `test_analysis_check_refuses_a_terminal_relabelled_elsewhere`.
- `test_close_ends_only_musters_terminal`: after `w.handle(close_argv(rec))`, the space and its shell are still
  live, and muster's terminal is gone.

**Verify:** `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/test_herdr_core.py`

## Task 6

**Launch, ad-hoc, hold and decisions on herdr-core, end to end**

- **Exists because:** the host functions are unit-tested in isolation, but every route goes through
  `launcher.ensure`'s crash-resumable steps. The Orca build found real gaps only at this level (Orca deviations
  T4 to T6).
- **Model:** sonnet
- **Files:** `tests/test_launch_herdr_core.py` (new), following `tests/test_launch_orca.py` exactly. Production code
  changes only if a test exposes a gap; record each gap as a deviation.
- **Consumes:** `from tests.test_launch import ensure, last, new_rec`.

Fixture: as `test_launch_orca.w`, with `HcWorld`, `herdr_core.sleep` patched, and
`monkeypatch.setitem(config.settings, "host", "herdr-core")`. Imports: `from tests.test_launch import ensure, last,
new_rec` and `from tests.test_herdr_core import calls, jump`.

Crash cases set `w.crash_after = <predicate>`, run `ensure(w, new_rec(w))` under `pytest.raises(KeyboardInterrupt)`,
then `ensure(w, last(w))`, as `test_a_crash_after_tab_create_on_a_headless_orca_reuses_the_terminal` does. The
predicates:
- worktree: `lambda argv: argv[:1] == ["git"] and argv[3:5] == ["worktree", "add"]`
- tab: `lambda argv: argv[:2] == ["hc", "run"]`
- typing: `lambda argv: argv[:2] == ["hc", "send"] and "--wait" not in argv`
- brief: `lambda argv: argv[:2] == ["hc", "send"] and "--wait" in argv`

Tests:
- `test_a_fresh_launch_types_claude_then_delivers_the_brief`: `rec["host"] == "herdr-core"`,
  `rec["pane"] == rec["name"]`, `rec["workspace"] == rec["path"]`, `"--permission-mode auto --settings" in w.typed[0]`,
  and `w.submitted == [rec["text"]]`.
- `test_a_crash_after_<worktree|tab|typing|brief>_recovers_it`: four tests, one per predicate. Each ends with
  `len(calls(w, "hc", "run")) == 1`, `len(w.typed) == 1` and `len(w.submitted) == 1`. In the typing test, also set
  `w.hc_late = 3` before the first `ensure`, so recovery meets a Claude still booting and must neither type again
  nor fail.
- `test_a_launch_with_no_window_runs_hidden`: `w.window = w.opens = False`, `jump(monkeypatch, 0, 31)`. The launch
  delivers, and the terminal (`w.find(rec["name"])`) has no `herdr.space` label.
- `test_a_crash_after_hc_run_with_no_window_then_a_window_gets_a_space`: `w.window = w.opens = False`,
  `jump(monkeypatch, 0, 31)`, and the tab predicate. Then `w.window = True` and `ensure(w, last(w))`. One `hc run`
  call and one `hc label … herdr.space=` call are made, and `w.submitted` has one entry.
- `test_an_untrusted_checkout_stops_as_readiness`: `w.hc_start = "blocked"`. It raises `launcher.LaunchFailure` with
  kind `"readiness"`, and `w.submitted == []`.
- `test_an_existing_branch_with_commits_launches_on_gits_checkout_with_adopt`: as the Orca test of that name. The git
  call is `worktree add <path> fix/x` (no `-b`).
- `test_a_core_restart_after_delivery_reads_as_agent_gone`: deliver, then `w.hc_restart()`.
  `herdr_core.agent(rec["pane"])` is None (what every route's agent-gone handling reads, the same as a None from
  herdr), and `herdr_core.pane(rec["pane"])` is not None (the kept shell is still live).
- `test_a_blocked_prompt_is_not_sent` and `test_an_unproven_prompt_is_resolved_by_the_hook_evidence`: copies of the
  Orca tests of these names, with `w.hc_send = "blocked"` and `w.hc_send = "not_started"`.

Cut: separate hold and decisions tests. Both call `host.prompt` (hold.py:262, decisions.py:983), which Task 3
unit-tests. The Orca build added none either.

**Verify:** `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/test_launch_herdr_core.py tests/test_launch.py tests/test_launch_orca.py`

## Task 7

**Cleanup on herdr-core, end to end**

- **Exists because:** cleanup reads `worktrees`, `quiet` and `remove_argv` through `hosts.of(rec)` and
  `hosts.configured()` at several sites (cleanup.py:175, 450-458, 475, 529, 933, 947-960). A wrong
  `open_workspace_id` or a non-repeatable fingerprint keeps a worktree forever, and only the whole tick shows that.
- **Model:** sonnet
- **Files:** `tests/test_cleanup_herdr_core.py` (new), following `tests/test_cleanup_orca.py`.

Structure, following `OrcaCleanupWorld`:

```python
class HcCleanupWorld(World):
    """test_cleanup's World with the worktree in herdr-core: hc, herdr-app, sh, ps, `git worktree list --porcelain`
    and `git rev-parse --path-format=absolute --git-common-dir` go to an HcWorld that holds the same checkout."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.herdr = False  # a herdr-recorded worktree: ps is the parent's; discovery still runs through herdr-core
        self.inner = HcWorld(tmp_path / "hc")
        self.inner.worktrees[self.path] = {"branch": self.branch, "clone": str(self.clone),
                                           "git_dir": str(self.git_dir), "workspace": None}
        made = json.loads(self.inner.handle(["herdr-app", "do", "space.new", "--name", "x", "--folder", self.path]))
        self.address = self.inner.term("run-15", self.path, {"muster.path": self.path,
                                                             "herdr.space": made["space"]})
        self.inner.hc_claude(self.address, "done")
        self.card.update(host="herdr-core", workspace=self.path)
        self._write_card()

    def run(self, argv, env=None):
        to_inner = (argv[3:6] == ["worktree", "list", "--porcelain"]
                    or argv[3:] == ["rev-parse", "--path-format=absolute", "--git-common-dir"]
                    or argv[0] in ("hc", "herdr-app", "sh") or (not self.herdr and argv[0] == "ps"))
        if to_inner:
            self.calls.append(argv)
            if argv[:2] == ["sh", "-c"] and "worktree remove" in argv[2]:
                self.removed = True
            return self.inner.handle(argv)
        return super().run(argv, env)
```

The `world` fixture is `test_cleanup_orca.world` with `(tmp_path / "hc").mkdir()`, `HcCleanupWorld`, and
`config.settings["host"] = "herdr-core"`. Import `BASE, World, cleanup_state, main, tick, window` from
`tests.test_cleanup` (`cleanup_state` is autouse: `# noqa: F401`). `removes(world)` is
`[c for c in world.calls if c[:2] == ["sh", "-c"]]`.

Tests:
- `test_a_merged_quiet_worktree_is_removed_after_the_window`: after `window(world)`, `len(removes(world)) == 1`.
  Afterwards `world.inner.spaces == {}`, no live terminal in `world.inner.hc` carries `muster.path == world.path`,
  and `world.path not in world.inner.worktrees`.
- `test_a_working_agent_keeps_the_worktree`: with `world.inner.hc[world.address]["status"]["state"] = "working"`,
  `window(world)` gives `removes(world) == []`.
- `test_a_herdr_worktree_is_still_cleaned_after_switching_to_herdr_core`: a copy of
  `test_a_herdr_worktree_is_still_cleaned_after_switching_to_orca`, with `world.herdr = True`, `card.pop("host")` and
  `workspace = "wR"`. It asserts the single `herdr worktree remove --workspace wR` and `removes(world) == []`.
- `test_an_analysis_workspace_closes_only_its_terminal`: make the `notes` folder, run
  `main(["open", "--cwd", str(folder), "--label", "notes"]) == 0`, and read the record as the Orca test does
  (`rec["host"] == "herdr-core"`). Then `window(world)`. The last entry of `removes(world)` names `rec["pane"]`, and
  `rec["pane"]` is not in the inner world's live terminals. The folder's space and its shell are still live.

**Verify:** `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/test_cleanup_herdr_core.py tests/test_cleanup.py tests/test_cleanup_orca.py`

## Task 8

**Acceptance: one PR run on herdr-core**

- **Exists because:** the acceptance gate drives the real Hermes and gateway with the fake world. A host the
  gate never runs can break the cron, profile or PATH wiring unseen, as the Orca build's T9 found
  (HERMES_AGENT_DIR).
- **Model:** sonnet
- **Files:** `tests/acceptance/world.py`, `tests/acceptance/shim/sitecustomize.py`, `tests/acceptance/conftest.py`,
  `tests/acceptance/test_lifecycle.py`.

Steps:
1. `world.py`: `AcceptanceWorld` subclasses `HcWorld` (from `tests.fake_herdr_core`) instead of `OrcaWorld`.
   `HcWorld` keeps OrcaWorld's knobs untouched (its own are `hc_*`), so the orca case is unchanged.
2. `shim/sitecustomize.py`: `WORLD = ("herdr", "orca", "hc", "herdr-app", "open", "lsof", "ps", "gh", "git")`.
   muster calls `open` only as `open -g -a Herdr` (herdr_core.window), which `HcWorld` models, and the world's
   window is up, so it is never called here. `sh` is deliberately left out: the run's PR is recorded and never
   merges, so cleanup never reaches `remove_argv` in this test. `open` gets no refusing stub (it is
   `/usr/bin/open`, never shadowed).
3. `conftest.py`: add `"hc"` and `"herdr-app"` to both stub-name tuples in `scripts()` (lines ~110 and ~113) and
   to the preflight `for name in (...)` (line ~196).
4. `test_lifecycle.py`:
   - In `to_pr_card` (~line 299), after the `orca_agents` loop, add:
     ```python
     for t in rig.world.hc.values():  # what hc then reports: Claude's turn is over
         if t["status"]:
             t["status"]["state"] = "done"
     ```
   - In `prompts(rig)`, add
     `elif argv[:2] == ["hc", "send"] and "--wait" in argv: found.append({**e, "text": argv[-1]})`.
   - Add `test_pr_merge_on_herdr_core`, a copy of `test_pr_merge_on_orca` with `settings={"host": "herdr-core"}`,
     `all(p["argv"][0] == "hc" for p in prompts(rig))`, and
     `run["worktree"].startswith(str(tmp_path.resolve()))` (the sandbox's `worktrees` setting is under tmp).

**Verify:** `HERMES_AGENT_DIR=~/.hermes/hermes-agent ~/.cache/muster-acceptance/venv/bin/python tests/acceptance/gate.py`.
Also run
`HERMES_AGENT_DIR=~/.hermes/hermes-agent ~/.cache/muster-acceptance/venv/bin/python -m pytest -q tests/acceptance/test_lifecycle.py -k "orca or herdr_core"`.
If `tests/acceptance/README.md` gives a different invocation, use the README's (learnings, orca-host).

## Task 9

**Opt-in live test, and the human checks**

- **Exists because:** every shape the fake encodes was seen once, by hand. The live test re-checks them on each
  hc upgrade. The window-closed, cron and core-restart behaviours cannot be checked without disrupting the human
  (Orca lesson 1).
- **Model:** sonnet
- **Files:** `tests/test_herdr_core_live.py` (new), following `tests/test_orca_live.py`.

`core.prepare_env()` runs at import time, so `~/.local/bin` is on PATH. The module is skipped unless `HC_LIVE=1` and
`shutil.which("hc")`.

It uses **one fixed root**, `ROOT = Path.home() / ".cache" / "muster-hc-live"` (Orca lesson 6). Under it:
- `origin.git`;
- `clone`, made once if missing with test_orca_live's `clone` fixture commands (bare init, clone, empty commit,
  push `HEAD:main`, `remote set-head origin main`);
- the worktree, always at `ROOT / "worktrees" / "muster-live-1"` (passed explicitly; the sandbox's `worktrees`
  setting is not used).

A fixture `leftovers` runs before and after each test. It must succeed even when there is nothing to remove:
- `herdr-app do space.close --id <id> --confirmed true` for each space in `herdr-app ls` whose `folder` starts with
  `str(ROOT)`, only when `herdr_core.up()`;
- `hc kill` + `hc rm` of each terminal in `hc ls --all --json` whose `cwd` starts with `str(ROOT)`, errors ignored;
- `git -C clone worktree remove --force <path>` if the path exists, then `git -C clone worktree prune`;
- `git -C clone branch -D muster-live/1` if it exists.

`test_a_live_run_end_to_end`:
1. `create_worktree(clone, "muster-live/1", "main", path, "live")`, then `open_tab(path, path, "run", [], "muster-live-1")`.
2. If `up()`, assert placement through `herdr-app ls`: some space with `folder == path` has a tab listing the
   terminal's address.
3. `quiet(path)` returns (the bare shell and the space's shell). Read `hc get` twice, 2 s apart: `status_ms`,
   `turn` and the `hc read` hash are equal.
4. `start_agent("muster-live-1", "claude", "muster-live-1", ["--model", "haiku"], [])`. The fresh clone is
   untrusted, so assert `agent(...)["agent_status"] == "blocked"` (the trust-dialog mapping).
5. `quiet(path)` raises `NotReady`, and `prompt(...)` raises `hosts.Blocked`.
6. `core.run(["hc", "keys", "muster-live-1", "esc", "--json"])` answers the dialog with Claude's "No, exit". Wait up
   to 10 s for `hc get`'s `foreground` to disappear. Then assert `agent("muster-live-1") is None`, and record
   whether hc's `status` cleared, in the design's Deviations line for Task 9.
7. Run `core.run(remove_argv(path, path, True))`. Afterwards:
   - `get("muster-live-1") is None`;
   - `not Path(path).exists()`;
   - no space in `herdr-app ls` has that folder.

`test_a_killed_name_can_be_reused`: `open_tab`, then `hc kill` the terminal, then `open_tab` again with the same
name. That succeeds, and `get(name)["state"] == "live"`.

Human checks, written as `Needs human:` lines in the PR body (build/review-branch carry them):
- Quit Herdr (Cmd-Q) and run the live test. Record whether the window reopened (`open -g -a Herdr`) and gave the
  run a space, or the log says "runs hidden". Also record `herdr-app status`'s exit code and output with no window.
  If it exits 0 with no window, `up()` must check a positive field instead.
- Run one launch from cron (a `hermes muster` tick) with the window closed. Confirm hc reaches its core, and
  whether `open -g -a Herdr` works from cron's context.
- At a convenient moment, run `hc server restart`. A muster terminal should come back as a bare shell, and the run
  should read as agent-gone (not stalled).

**Verify:** run `HC_LIVE=1 PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q -s tests/test_herdr_core_live.py` with the
window open. Without `HC_LIVE`, `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/test_herdr_core_live.py` must
report it skipped.

## Task 10

**Docs: "Using herdr-core", host lists, and "herdr TUI" for the old host**

- **Exists because:** the setting is useless if nobody knows it exists. The dispatch prompt tells the builder
  agent how to inspect its own host. "herdr" now names two different products.
- **Model:** sonnet
- **Files:** `docs/setup.md`, `README.md`, `docs/reference.md`, `docs/design.md`, `docs/architecture.md`,
  `docs/channels.md`, `AGENTS.md`, `plugin.yaml` (descriptions only), `prompts/dispatch.md`, `muster/core.py`
  (docstring).

Steps:
1. `docs/setup.md`:
   - Requirements (line 9): "herdr TUI >= 0.9.3, Orca, or the Herdr desktop app (herdr-core, hc >= 0.1.20)".
   - Config table `host` row (line ~123): add `herdr-core`.
   - New `### Using herdr-core` beside `### Using Orca` (line ~192), covering:
     - `host: herdr-core`;
     - worktrees go where the `worktrees` setting says (default `~/.herdr/worktrees`, the app's own layout);
     - with no window, muster opens Herdr in the background, and a run that still finds no window runs hidden;
     - find a hidden run with `hc ls --label muster.path=<path>`, then `herdr-app do terminal.keep --terminal <name>`
       to give it a tab, or `hc attach <name>` (Ctrl+\ detaches);
     - Cmd-Q leaves terminals running, but a core restart (`hc server restart`, a reboot) leaves each run's terminal
       as a bare shell, and the run then reads as agent-gone (recover with
       `hermes muster recover <card> --resend`);
     - cron reachability as Task 9's human check found it. If that check has not been run yet, write "hc starts its
       core on demand (hc's manual); confirm with one cron tick" rather than claiming it.
2. Find the host mentions with `grep -n -w herdr README.md docs/*.md AGENTS.md plugin.yaml prompts/dispatch.md`.
   Where "herdr" names the terminal host as a product, say "herdr TUI". Where hosts are listed ("herdr or Orca"),
   list herdr-core too. Leave these unchanged:
   - the setting value `herdr`;
   - `herdr …` CLI commands;
   - file and module names;
   - the `~/.herdr/worktrees` path.
3. `prompts/dispatch.md`, three additions:
   - In "Never open a coding pane by hand", add `hc run` and `herdr-app do space.new`.
   - In the analysis-workspace paragraph, add: "On herdr-core the record has `"host": "herdr-core"`. Its `pane` is
     the hc terminal's name. Type the agent's command line into it with `hc send --json -- <pane> "claude"`."
   - After the Orca message steps, add "On a herdr-core run (`"host": "herdr-core"` in the links):", with four steps
     that mirror the Orca ones:
     1. The links' `pane` is the terminal name. `hc get <pane> --json` must show `state: live`, a `status.app`
        (the agent), a `foreground`, and `labels["muster.path"]` equal to the run's worktree. Otherwise tell the
        human what you found and never pick another target.
     2. Encode the message as in the herdr steps, then run
        `hc send --wait --timeout 10 --json -- <pane> "<the same envelope>"`.
     3. `"started": true` means hc submitted it, nothing more. Tell the human that.
     4. A `blocked` error is the blocker: the agent waits on a question. Report any other result as hc gave it. Never
        send keys, interrupt or relaunch.
4. `muster/core.py` `prepare_env` docstring: "find hermes, herdr, orca, hc, herdr-app and lsof under cron's bare
   PATH". No code change: `~/.local/bin` is already first.

**Verify:** `PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/test_prompts.py tests/test_startup_prompt.py && hermes plugins validate .`
