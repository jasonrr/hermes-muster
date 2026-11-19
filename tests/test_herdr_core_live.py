"""Opt-in check against the real Herdr app's core (HC_LIVE=1): the shapes muster/hosts/herdr_core.py relies on.

Everything lives under ~/.cache/muster-hc-live: a scratch origin and clone, one fixed worktree, and a space named
after it in the open Herdr window (closed again). Claude runs with `--model haiku` in an untrusted checkout, so it
stops at the trust dialog. Core restart, cron and a closed window are human checks (the plan's Task 9).
"""

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from muster import core, hosts
from muster.hosts import herdr_core

pytestmark = pytest.mark.skipif(
    os.environ.get("HC_LIVE") != "1" or not (shutil.which("hc") or Path.home().joinpath(".local/bin/hc").exists()),
    reason="Opt-in live Herdr core run; set HC_LIVE=1 with the Herdr window open",
)

ROOT = Path.home() / ".cache" / "muster-hc-live"
PATH = str(ROOT / "worktrees" / "muster-live-1")
NAME = "muster-live-1"
BRANCH = "muster-live/1"


def git(*argv, cwd):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def sh(*argv):
    return subprocess.run(argv, check=False, capture_output=True, text=True)


def under(path):
    return bool(path) and (path == str(ROOT) or path.startswith(str(ROOT) + "/"))


@pytest.fixture
def clone():
    core.prepare_env()  # the sandbox fixture rewrote PATH; this puts ~/.local/bin back
    path = ROOT / "clone"
    if not path.exists():
        ROOT.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "--bare", "origin.git", cwd=ROOT)
        git("clone", "-q", "origin.git", str(path), cwd=ROOT)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=path)
        git("push", "-q", "origin", "HEAD:main", cwd=path)
        git("remote", "set-head", "origin", "main", cwd=path)
    return path


def clean(clone):
    if herdr_core.up():
        for s in json.loads(core.run(["herdr-app", "ls"]))["spaces"]:
            if under(s.get("folder")):
                sh("herdr-app", "do", "space.close", "--id", s["id"], "--confirmed", "true")
    for t in json.loads(core.run(["hc", "ls", "--all", "--json"]))["terminals"]:
        if under(t.get("cwd")):
            sh("hc", "kill", t["address"], "--json")
            sh("hc", "rm", t["address"], "--json")
    if Path(PATH).exists():
        sh("git", "-C", str(clone), "worktree", "remove", "--force", PATH)
        shutil.rmtree(PATH, ignore_errors=True)
    sh("git", "-C", str(clone), "worktree", "prune")
    sh("git", "-C", str(clone), "branch", "-D", BRANCH)


@pytest.fixture(autouse=True)
def leftovers(clone):
    clean(clone)
    yield
    clean(clone)


def screen_hash():
    return hashlib.sha256(core.run(["hc", "read", NAME]).encode()).hexdigest()


def placed(address):
    return any(s["folder"] == PATH and any(address == term["terminal"] for tab in s["tabs"] for term in tab["terminals"])
               for s in json.loads(core.run(["herdr-app", "ls"]))["spaces"])


def snapshot():
    t = herdr_core.get(NAME)
    return t["status_ms"], t["turn"], screen_hash()


def test_a_live_run_end_to_end(clone):
    made = herdr_core.create_worktree(clone, BRANCH, "main", PATH, "live")
    assert made == {"workspace": PATH, "path": PATH}
    pane = herdr_core.open_tab(PATH, PATH, "run", [], NAME)
    address = herdr_core.get(pane)["address"]
    if herdr_core.up():
        deadline = time.time() + 10  # the label moves the terminal into its space a moment later, not at once
        while not placed(address) and time.time() < deadline:
            time.sleep(0.5)
        assert placed(address)
    herdr_core.quiet(PATH)
    first = snapshot()
    time.sleep(2)
    assert snapshot() == first

    herdr_core.start_agent(NAME, "claude", NAME, ["--model", "haiku"], [])
    assert herdr_core.agent(NAME)["agent_status"] == "blocked"  # the untrusted checkout's trust dialog
    with pytest.raises(hosts.NotReady):
        herdr_core.quiet(PATH)
    with pytest.raises(hosts.Blocked):
        herdr_core.prompt(NAME, "hello")

    core.run(["hc", "keys", NAME, "esc", "--json"])  # the dialog's "No, exit"
    deadline = time.time() + 10
    while time.time() < deadline and herdr_core.get(NAME)["foreground"]["argv"][0] != "/bin/zsh":
        time.sleep(0.5)
    t = herdr_core.get(NAME)
    print("LIVE foreground after exit:", t.get("foreground"))
    print("LIVE status after exit:", t.get("status"))
    assert herdr_core.agent(NAME) is None

    core.run(herdr_core.remove_argv(PATH, PATH, True))
    assert herdr_core.get(NAME) is None
    assert not Path(PATH).exists()
    if herdr_core.up():
        assert not any(s.get("folder") == PATH for s in json.loads(core.run(["herdr-app", "ls"]))["spaces"])


def test_a_killed_name_can_be_reused(clone):
    herdr_core.create_worktree(clone, BRANCH, "main", PATH, "live")
    herdr_core.open_tab(PATH, PATH, "run", [], NAME)
    core.run(["hc", "kill", NAME, "--json"])
    herdr_core.open_tab(PATH, PATH, "run", [], NAME)
    assert herdr_core.get(NAME)["state"] == "live"
