"""launcher: the pane launch and its recovery, shared by the issue route (muster.intake) and the ad-hoc route
(muster.adhoc). ensure() brings a launch record from wherever it stopped to a delivered first prompt: a trusted
herdr worktree, a tab, a Claude started with no task, then the brief. A failed launch blocks its card as setup
trouble (report_failure), and `hermes muster recover` resumes it (recoverable).
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from . import config, core


def startup_prompt(text):
    """Preserve the brief exactly without shell-rejected control characters in argv."""
    return config.prompt("launch-envelope", brief=json.dumps(text, ensure_ascii=True))


def pane_env():
    """The pane's env pairs: the hermes home always; the bot's gh login, with token vars blanked, when configured.
    gh prefers a token env var over GH_CONFIG_DIR, so both are blanked."""
    env = [f"HERMES_HOME={os.environ['HERMES_HOME']}"]
    if os.environ.get("HERMES_KANBAN_HOME", os.environ["HERMES_HOME"]) != os.environ["HERMES_HOME"]:
        env.append(f"HERMES_KANBAN_HOME={os.environ['HERMES_KANBAN_HOME']}")
    if config.settings["gh_config_dir"]:
        env += [f"GH_CONFIG_DIR={core.gh_config_dir()}", "GH_TOKEN=", "GITHUB_TOKEN="]
    return env


def setup_trouble(step, error):
    """A launch failure's block reason: kind capability, so the ping says "Setup trouble", not a decision."""
    return (f"The coding agent did not start (the {step} step failed). This is setup trouble, not a product "
            f"decision.\nDetails: {' '.join(str(error).split()) or type(error).__name__}")


# --- The pane launch, shared with ad-hoc runs --------------------------------------------------------
#
# Herdr 0.9.3 (docs agent-automation.mdx) launches an agent in two phases. `agent start` returns
# only once Claude is detected AND idle-ready; a task passed as Claude's own argument starts work
# before that, so start times out while Claude works. So: start with no task, confirm the agent,
# then `agent prompt --wait`. A timeout or agent_prompt_stalled does not prove nothing arrived
# (same page): the evidence is read before anything is sent again, and an unknown is never resent
# without a person's --resend.
#
# The launch record, a dict the caller persists with `save`, is written before every side effect:
#   owner, repo, clone, branch, base, path, label   the planned worktree (path is passed as --path,
#                                                   so a crash right after create still finds it)
#   workspace, pane                                 once herdr returned them
#   name, model, settings, env, tab                 the agent and its tab
#   step                                            the side effect last begun: worktree|tab|agent|prompt|done
#   phase                                           the step being checked, for a failure's words
#   text, sha256                                    the exact startup prompt, fixed on first build
#   prompt {state, at, seq}                         sending|working|blocked|done|not-sent|unknown
#   reused                                          what an adopted checkout already held
#   evidence                                        the last diagnostics, newest last
#   adopt, resend                                   set only by a person's --adopt / --resend

AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # herdr's rule for agent names
READY = ("idle", "done")
DELIVERED = ("working", "blocked", "done")
OWNER_FILE = "muster-launch-owner.json"


class LaunchFailure(core.LaunchError):
    """A classified launch failure: kind is refused (nothing was changed that a retry must undo),
    start (no agent appeared), readiness (an agent shows a dialog before its prompt), busy (an agent
    exists but is not ready),
    delivery-unknown (the prompt may or may not have arrived) or setup (a command failed)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def agent_name(prefix, tail):
    """A herdr agent name ([a-z][a-z0-9_-]{0,31}) from a fixed prefix and any tail, the same every time.
    A tail that does not fit is cut and given a hash of the whole, so two long tails stay apart."""
    raw = re.sub(r"[^a-z0-9_-]+", "-", f"{prefix}-{tail}".lower()).strip("-_")
    if AGENT_NAME.fullmatch(raw):
        return raw
    return raw[:25].rstrip("-_") + "-" + hashlib.sha256(raw.encode()).hexdigest()[:6]


def worktree_path(clone, branch):
    return str(core.worktrees_dir() / Path(clone).name / branch.replace("/", "-"))


def base_of(clone, configured=None):
    """(base branch, None), or ("main", why) when git cannot name origin's default branch."""
    if configured:
        return configured, None
    head = ["git", "-C", str(clone), "symbolic-ref", "refs/remotes/origin/HEAD"]
    try:
        return core.run(head).strip().removeprefix("refs/remotes/origin/"), None
    except core.CommandError:
        pass
    try:  # origin/HEAD is set at clone time; a clone made otherwise may lack it
        core.run(["git", "-C", str(clone), "remote", "set-head", "origin", "-a"])
        return core.run(head).strip().removeprefix("refs/remotes/origin/"), None
    except core.CommandError as error:
        return "main", f"origin/HEAD is unset and `git remote set-head origin -a` failed ({error}); launched from main"


def plan(owner, repo, clone, branch, base, label, name, model, settings, tab, env=()):
    """A new launch record. Nothing outside the caller's record changes."""
    if not AGENT_NAME.fullmatch(name):
        raise LaunchFailure("refused", f"agent name {name!r} is not [a-z][a-z0-9_-]{{0,31}}")
    # git's own rule, run locally: a pure check, so not through the run() seam.
    if subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True, check=False).returncode:
        raise LaunchFailure("refused", f"{branch!r} is not a valid branch name")
    if subprocess.run(["git", "check-ref-format", "--branch", base], capture_output=True, check=False).returncode:
        raise LaunchFailure("refused", f"{base!r} is not a valid base branch name")
    return {"owner": owner, "repo": repo, "clone": str(clone), "branch": branch,
            "base": base, "path": worktree_path(clone, branch), "planned": worktree_path(clone, branch), "label": label, "name": name, "model": model,
            "settings": str(settings), "tab": tab, "env": list(env), "step": None, "workspace": None,
            "pane": None, "prompt": None, "evidence": []}


def note(rec, what):
    rec["evidence"] = (rec.get("evidence") or [])[-19:] + [f"{time.strftime('%Y-%m-%dT%H:%M:%S')} "
                                                           + " ".join(str(what).split())[:300]]


def herdr_result(*args):
    return json.loads(core.run(["herdr", *args]))["result"]


def agent_at(target):
    """herdr's agent for a pane or name, or None when herdr says there is none. Raises when herdr fails."""
    try:
        return herdr_result("agent", "get", target)["agent"]
    except core.CommandError as error:
        if "agent_not_found" in str(error) or "pane_not_found" in str(error):
            return None
        raise


def pane_at(pane):
    try:
        return herdr_result("pane", "get", pane)["pane"]
    except core.CommandError as error:
        if "pane_not_found" in str(error):
            return None
        raise


def pull_requests(repo, branch):
    """Open or merged pull requests from the branch: work a relaunch must not run over."""
    prs = json.loads(core.run(["gh", "pr", "list", "-R", repo, "--head", branch, "--state", "all", "--json", "url,state"]))
    return [pr["url"] for pr in prs if pr.get("state") in ("OPEN", "MERGED")]


@contextlib.contextmanager
def launch_lock(directory):
    """One launch or recover of a record at a time; a second one is refused, never queued."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    with open(Path(directory) / "launch.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LaunchFailure("busy", "another launch or recover of this card is running") from None
        yield


# How Claude Code hands a long paste to UserPromptSubmit (sessions 43300d37, dae8bcbc): this exact shape only.
PASTED = re.compile(r'\n\n<pasted_content id="([0-9a-f]+)">\n(.*)\n</pasted_content id="\1">\n', re.S)


def prompt_seen(directory, payload):
    """A UserPromptSubmit hook's evidence that a prompt reached Claude: its sha256, kept beside the record,
    and for a prompt Claude wrapped as a paste, the sha256 of the text inside the wrapper as `inner`."""
    if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), str):
        return
    record = {"sha256": hashlib.sha256(payload["prompt"].encode()).hexdigest()}
    paste = PASTED.fullmatch(payload["prompt"])
    if paste:
        record["inner"] = hashlib.sha256(paste.group(2).encode()).hexdigest()
    path = Path(directory) / "prompt-seen.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab+") as out:
        out.seek(0, os.SEEK_END)
        if out.tell():
            out.seek(-1, os.SEEK_END)
            torn = out.read(1) != b"\n"  # a crash mid-append: start a fresh line, never glue onto it
        else:
            torn = False
        out.write(("\n" if torn else "").encode() + json.dumps({**record, "at": int(time.time()),
                                                                     "session": payload.get("session_id")}).encode() + b"\n")


def seen(directory, sha):
    return seen_at(directory, sha) is not None


def seen_at(directory, sha):
    """When the UserPromptSubmit hook first saw this prompt (its record's `at`), or None."""
    path = Path(directory) / "prompt-seen.jsonl"
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
            if sha and sha in (record.get("sha256"), record.get("inner")):
                return record.get("at", 0)
        except (ValueError, AttributeError):  # a line torn by a crash, or not a record: not evidence
            continue
    return None


def owner_of(path):
    git_dir = Path(core.run(["git", "-C", path, "rev-parse", "--absolute-git-dir"]).strip())
    marker = git_dir / OWNER_FILE
    return git_dir, (json.loads(marker.read_text()) if marker.is_file() else None)


def take_worktree(rec, save, known):
    """The record's checkout, made or adopted. Raises LaunchFailure("refused") for anything not ours."""
    rec["phase"] = "worktree"
    clone, branch, base = rec["clone"], rec["branch"], rec["base"]
    listing = herdr_result("worktree", "list", "--cwd", clone)["worktrees"]
    here = next((w for w in listing if w.get("path") == rec["path"]), None)
    other = next((w for w in listing if w.get("branch") == branch and w.get("path") != rec["path"]), None)
    if other:
        # The branch is checked out elsewhere (an earlier launch's default path): judged below like any checkout.
        note(rec, f"branch {branch} is already checked out at {other['path']}")
        rec["path"], here = other["path"], other
    if here is None:
        if (rec.get("prompt") or {}).get("state") in DELIVERED:
            raise LaunchFailure("refused", f"the worktree {rec['path']} is gone after the brief was delivered; "
                                           f"start a new run")
        if Path(rec["path"]).exists():
            raise LaunchFailure("refused", f"{rec['path']} exists but is not a worktree of {clone}; move it away")
        core.run(["git", "-C", clone, "fetch", "origin", base])
        tip = core.run(["git", "-C", clone, "rev-parse", f"origin/{base}"]).strip()
        try:
            head = core.run(["git", "-C", clone, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]).strip()
        except core.CommandError:
            head = None
        if head:
            ahead = int(core.run(["git", "-C", clone, "rev-list", "--count", f"origin/{base}..refs/heads/{branch}"]).strip())
            if ahead and not rec.get("adopt"):
                raise LaunchFailure("refused", f"branch {branch} already exists with {ahead} commits not on origin/{base} "
                                               f"and no worktree. Check `git log origin/{base}..{branch}`, then recover "
                                               f"with --adopt to work on it as is")
            if ahead:
                rec["reused"] = {"commits": ahead, "uncommitted": 0}
            elif head != tip:
                # No commit of its own, so nothing is lost: fast-forward it, refused by git if it moved meanwhile.
                core.run(["git", "-C", clone, "update-ref", f"refs/heads/{branch}", tip, head])
                note(rec, f"branch {branch} had no commits of its own; fast-forwarded {head[:12]} -> {tip[:12]}")
        rec["step"] = "worktree"
        save(rec)
        made = herdr_result("worktree", "create", "--cwd", clone, "--branch", branch, "--base", f"origin/{base}",
                            "--path", rec["path"], "--label", rec["label"], "--no-focus", "--trust-repository")
        rec["workspace"] = made["workspace"]["workspace_id"]
        git_dir, _ = owner_of(made["worktree"]["path"])
        core.save_json(git_dir / OWNER_FILE, {"owner": rec["owner"], "branch": branch})
        save(rec)
        return git_dir
    git_dir, owner = owner_of(rec["path"])
    current = core.run(["git", "-C", rec["path"], "rev-parse", "--abbrev-ref", "HEAD"]).strip()
    if here.get("branch") != branch or current != branch:
        raise LaunchFailure("refused", f"{rec['path']} is on {current}, not {branch}")
    ours = owner and owner.get("owner") == rec["owner"]
    # A create that crashed before its marker: the path was planned and its intent saved before create.
    ours = ours or (owner is None and rec["path"] == rec["planned"] and rec.get("step") == "worktree"
                    and not rec.get("workspace"))
    if not ours:
        prior = None if owner else (known(git_dir) if known else None)
        if not (rec.get("adopt") or prior):
            whose = f"launch {owner.get('owner')}" if owner else "no launcher record"
            raise LaunchFailure("refused", f"{rec['path']} belongs to {whose}. Check it, then recover with --adopt "
                                           f"to reuse it")
        workspace = here.get("open_workspace_id")
        if workspace:
            busy = [p["pane_id"] for p in herdr_result("pane", "list", "--workspace", workspace)["panes"]
                    if p.get("agent") and p["pane_id"] != rec.get("pane")]
            if busy:
                raise LaunchFailure("refused", f"{rec['path']} has a live agent in pane {busy[0]}; it is not free to reuse")
        dirty = core.run(["git", "-C", rec["path"], "status", "--porcelain", "--untracked-files=all"]).splitlines()
        ahead = int(core.run(["git", "-C", rec["path"], "rev-list", "--count", f"origin/{rec['base']}..HEAD"]).strip())
        rec["reused"] = {"commits": ahead, "uncommitted": len(dirty)}
        note(rec, f"adopted {rec['path']} from {owner or prior or 'a person (--adopt)'}: {ahead} commits, "
                  f"{len(dirty)} uncommitted or untracked files kept")
        core.save_json(git_dir / OWNER_FILE, {"owner": rec["owner"], "branch": branch, "previous": owner or prior})
    workspace = here.get("open_workspace_id")
    if not workspace:
        rec["step"] = "worktree"
        save(rec)
        workspace = herdr_result("worktree", "open", "--cwd", rec["clone"], "--path", rec["path"], "--label",
                                 rec["label"], "--no-focus", "--trust-repository")["workspace"]["workspace_id"]
    if workspace != rec.get("workspace"):
        rec["workspace"], rec["pane"] = workspace, (rec["pane"] if rec.get("workspace") == workspace else None)
    save(rec)
    return git_dir


def take_pane(rec, save):
    rec["phase"] = "tab"
    if rec.get("pane"):
        pane = pane_at(rec["pane"])
        if pane and pane.get("workspace_id") == rec["workspace"]:
            return
        if (rec.get("prompt") or {}).get("state") in DELIVERED:
            raise LaunchFailure("refused", f"pane {rec['pane']} is gone after the brief was delivered: that session "
                                           f"ended. Start a new run")
        note(rec, f"recorded pane {rec['pane']} is gone; opening a new tab")
        rec["pane"] = None
    agent = agent_at(rec["name"])
    if agent:
        if agent.get("workspace_id") != rec["workspace"]:
            raise LaunchFailure("refused", f"agent name {rec['name']} already runs in pane {agent.get('pane_id')}, "
                                           f"outside this launch's workspace {rec['workspace']}")
        rec["pane"] = agent["pane_id"]
        note(rec, f"adopted live agent {rec['name']} in pane {rec['pane']}")
        save(rec)
        return
    rec["step"] = "tab"
    save(rec)
    # ponytail: a crash between this create and the save below leaves one empty shell tab behind.
    # A fresh shell tab, not the layout's own claude pane: its start races any scripted exit.
    argv = ["herdr", "tab", "create", "--workspace", rec["workspace"], "--cwd", rec["path"], "--label", rec["tab"]]
    for pair in rec["env"]:
        argv += ["--env", pair]
    rec["pane"] = json.loads(core.run([*argv, "--no-focus"]))["result"]["root_pane"]["pane_id"]
    save(rec)


def check_agent(rec, agent):
    if core.adapter().is_ours(agent, rec["name"], Path(rec["path"])):
        return
    if agent.get("agent") != core.adapter().KIND or agent.get("name") != rec["name"]:
        raise LaunchFailure("refused", f"pane {rec['pane']} runs {agent.get('agent')} named {agent.get('name')}, "
                                       f"not {core.adapter().KIND} named {rec['name']}")
    cwd = agent.get("foreground_cwd") or agent.get("cwd") or ""
    raise LaunchFailure("refused", f"agent {rec['name']} runs in {cwd}, not in {rec['path']}")


def take_agent(rec, save):
    """A Claude in the pane, ready for its first prompt: started here with no task, or adopted."""
    rec["phase"] = "agent"
    agent = agent_at(rec["pane"])
    if agent is None:
        elsewhere = agent_at(rec["name"])
        if elsewhere:
            raise LaunchFailure("refused", f"agent name {rec['name']} already runs in pane {elsewhere.get('pane_id')}")
        rec["step"] = "agent"
        save(rec)
        try:
            core.run(["herdr", "agent", "start", rec["name"], "--kind", core.adapter().KIND, "--pane", rec["pane"],
                 "--timeout", "120000", "--", *core.adapter().launch_args(rec["model"], rec["settings"])])
        except core.CommandError as error:
            note(rec, f"agent start: {error}")
            save(rec)
            agent = agent_at(rec["pane"])
            if agent is None:
                raise LaunchFailure("start", f"Claude did not start in pane {rec['pane']}: "
                                                 f"{' '.join(str(error).split())}") from None
        else:
            agent = agent_at(rec["pane"])
            if agent is None:
                raise LaunchFailure("readiness", f"herdr reported a start but shows no agent in pane {rec['pane']}")
    else:
        note(rec, f"adopted the agent already in pane {rec['pane']} ({agent.get('agent_status')})")
    check_agent(rec, agent)
    status = agent.get("agent_status")
    if status in READY:
        return agent
    if status == "blocked":
        raise LaunchFailure("readiness", f"Claude in pane {rec['pane']} shows a dialog before its first prompt. "
                                         f"Answer it in the pane, then recover")
    raise LaunchFailure("busy", f"Claude in pane {rec['pane']} is {status}, not ready, and this launch has not sent "
                                f"its prompt. Check the pane, then recover")


def delivered(rec, directory):
    """What the evidence says happened to a prompt whose sending was not confirmed: a delivered state, or None."""
    agent = agent_at(rec["pane"])
    status = agent.get("agent_status") if agent else None
    if status in ("working", "blocked"):
        return status
    completion = agent.get("completion_seq") if agent else None
    if completion is not None and completion > (rec["prompt"].get("seq") or 0):
        return "done"  # a turn finished after the send without herdr seeing it work (agent-automation.mdx)
    if seen(directory, rec["sha256"]):
        return "done" if status in READY else "working"
    return None


def send_prompt(rec, save, directory, agent):
    rec["phase"] = "prompt"
    prompt = rec.get("prompt") or {}
    if prompt.get("state") in ("sending", "unknown"):
        state = delivered(rec, directory)
        if state:
            rec["prompt"] = {**prompt, "state": state}
            note(rec, f"the earlier send arrived: {state}")
            save(rec)
            return state
        if not rec.get("resend"):
            rec["prompt"] = {**prompt, "state": "unknown"}
            save(rec)
            raise LaunchFailure("delivery-unknown", f"the brief may or may not have reached Claude in pane {rec['pane']}. "
                                                    f"Look at the pane: if it shows no brief, recover with --resend")
        note(rec, "resending: a person chose --resend")
    rec.pop("resend", None)
    rec["step"] = "prompt"
    rec["prompt"] = {"state": "sending", "at": int(time.time()), "seq": agent.get("state_change_seq")}
    save(rec)
    try:
        got = herdr_result("agent", "prompt", rec["pane"], rec["text"], "--wait", "--until", "working",
                           "--until", "blocked", "--timeout", "30000")["agent"]
        state = got.get("agent_status")
        if state not in ("working", "blocked"):
            raise core.CommandError(f"herdr agent prompt: matched {state}")
    except core.CommandError as error:
        note(rec, f"agent prompt: {error}")
        if "agent_blocked" in str(error):  # refused before any input was sent (agent-automation.mdx)
            rec["prompt"] = {**rec["prompt"], "state": "not-sent"}
            save(rec)
            raise LaunchFailure("readiness", f"Claude in pane {rec['pane']} showed a dialog, so its brief was not sent. "
                                             f"Answer it in the pane, then recover") from None
        state = delivered(rec, directory)
        if not state:
            rec["prompt"] = {**rec["prompt"], "state": "unknown"}
            save(rec)
            raise LaunchFailure("delivery-unknown", f"herdr could not confirm the brief reached Claude in pane "
                                                    f"{rec['pane']} ({' '.join(str(error).split())[:200]}). Look at the "
                                                    f"pane: if it shows no brief, recover with --resend") from None
    rec["prompt"] = {**rec["prompt"], "state": state}
    save(rec)
    return state


def reuse_note(reused):
    return "\n\n" + config.prompt("reused-worktree", commits=reused["commits"], uncommitted=reused["uncommitted"]) + "\n"


def ensure(rec, save, directory, prepare, known=None):
    """Bring a launch from wherever its record stops to a delivered first prompt. Returns the prompt's state
    (working, blocked or done). Raises LaunchFailure, CommandError or OSError; every step is safe to repeat.

    prepare(rec, git_dir) writes the caller's files and returns the brief; it runs until the prompt is fixed.
    known(git_dir) names an earlier owner this caller accepts for a checkout without our marker, or None.
    """
    from . import progress  # progress imports core

    state = (rec.get("prompt") or {}).get("state")
    if state in DELIVERED:
        progress.launched(rec["owner"], directory, rec.get("sha256"), again=True)  # a crash may have come before the first
        return state
    git_dir = take_worktree(rec, save, known)
    take_pane(rec, save)
    text = prepare(rec, git_dir)  # rewrites the caller's files (links name the pane) every attempt
    if not rec.get("text"):  # fixed once: a retry sends, and checks evidence against, the same bytes
        rec["text"] = startup_prompt(text + (reuse_note(rec["reused"]) if rec.get("reused") else ""))
        rec["sha256"] = hashlib.sha256(rec["text"].encode()).hexdigest()
        save(rec)
    if state in ("sending", "unknown"):
        agent = agent_at(rec["pane"])
        if agent:
            check_agent(rec, agent)
        elif rec.get("resend"):
            agent = take_agent(rec, save)
        else:
            raise LaunchFailure("delivery-unknown", f"pane {rec['pane']} has no agent, and the brief may have been "
                                                    f"sent before. Look at the pane, then recover with --resend")
    else:
        agent = take_agent(rec, save)
    state = send_prompt(rec, save, directory, agent)
    rec["step"] = "done"
    save(rec)
    progress.launched(rec["owner"], directory, rec.get("sha256"))
    return state


def block_kind(card):
    """The kind of the card's current block, read-only from the board db (show --json does not carry it)."""
    db = core.board_db()
    with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT block_kind FROM tasks WHERE id = ?", (card,)).fetchone()
    return row[0] if row else None


def card_status(card):
    return json.loads(core.kanban("show", card, "--json"))["task"]["status"]


def recoverable(card):
    """The ledger card's status, if a recover may relaunch it: ready, or blocked by a launch failure.
    A needs_input block is a session that ended after its launch, not a launch to resume."""
    status = card_status(card)
    if status == "ready" or (status == "blocked" and block_kind(card) == "capability"):
        return status
    raise LaunchFailure("refused", f"card {card} is {status}" + (f" ({block_kind(card)})" if status == "blocked" else "")
                        + "; only a ready card or one blocked by a launch failure is recovered")


def report_failure(card, step, error):
    """Tell the human once. A ready card blocks (capability: setup trouble). A card already blocked gets a comment:
    hermes routes a second same-kind block after an unblock to triage, a dead end (test_kanban_contract.py)."""
    text = trouble(step, error)
    try:
        status = card_status(card)
    except (core.CommandError, ValueError, KeyError, TypeError):
        status = "ready"  # unreadable: try the block; hermes itself refuses a card that is not ready
    if status == "ready":
        core.kanban("block", "--kind", "capability", card, text)
    else:
        core.kanban("comment", card, "Recovery failed again. " + text)


def trouble(step, error):
    """A launch failure's words for the human, true to what herdr showed: never "did not start" when an agent exists."""
    detail = " ".join(str(error).split()) or type(error).__name__
    kind = getattr(error, "kind", None)
    if kind == "delivery-unknown":
        return (f"The coding agent started, but its first prompt may not have arrived. Nothing was resent. This is "
                f"setup trouble, not a product decision.\nDetails: {detail}")
    if kind in ("readiness", "busy"):
        return (f"The coding agent did not become ready for its brief (the {step} step). This is setup trouble, not a "
                f"product decision.\nDetails: {detail}")
    if kind == "refused":
        return (f"The launch was refused before the agent got its brief (the {step} step). This is setup trouble, not "
                f"a product decision.\nDetails: {detail}")
    return setup_trouble(step, error)


def topic(card, repo, title, issue=None, branch=None):
    """Ask for the run's topic (muster.conversation); a fallback is said once on the card. Never raises."""
    from . import conversation

    ref = conversation.request(card, conversation.name(repo, title, issue=issue, branch=branch))
    if ref and ref.get("state") == "fallback" and not ref.get("commented"):
        with contextlib.suppress(core.CommandError, OSError):
            core.kanban("comment", card, f"topic: not created: {ref.get('why')}; this run stays in the main chat")
            conversation.update(card, commented=True)
