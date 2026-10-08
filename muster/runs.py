"""runs: an ad-hoc herdr coding run gets a subscribed ledger card before it launches. Its waits, its
session end and a turn end with a finished pull request become verified kanban events that ping the
human and wake the agent.

  launch --cwd <clone> --branch <type>/<name> --title <t> --brief <file> [--base <branch>] [--model m]
      (--base defaults to the repos entry's @base, else origin's default branch, else main)
      the ledger card (key run:<repo>:<branch>), then <data dir>/runs/<card>/run.json, then the
      notify+wake subscription, then core.ensure: a trusted herdr worktree, an agent pane (its own
      --settings carry the hooks) started with no task, then its brief as the first prompt (brief.md
      keeps a copy for audit; run.json's "launch" is the resumable record)
  recover <card> [--resend] [--adopt]   resume a failed launch where its record stops
  hook --card <card> <event>            the pane's hooks: save the event to the run's outbox, then deliver it
  flush                                 cron, every minute: deliver or ack every saved event; find missed hooks

A turn end is not success. `stop` completes the ledger only when exactly one open or merged,
non-draft pull request from the run's branch into its base has the worktree's HEAD as its head and
no tracked file is changed; otherwise it pages no one, because a turn also ends while background
work runs. The human is paged when the agent asks (a permission prompt or AskUserQuestion, as in
events), when its session ends unfinished, and when herdr shows it idle for IDLE_AFTER with no
finished pull request (the flush). Unknown (a failed gh, git or hermes call) moves nothing: the event
stays in outbox/ and the next hook or flush tries it again. A delivered event is kept in sent/ until
every notify+wake subscription of the card it moved has pinged the human and claimed the wake past
that card's newest blocked or completed event. The claim is the gateway's, before the wake runs: it
proves the human was told, not that the agent's turn ran.

One run is the directory <data dir>/runs/<card>/: run.json, lock, the wait marker (core.WAIT_KIND),
brief.md, settings.json, outbox/<ns>-<event>.json and sent/<ns>-<event>.json. An issue run's card
(events.hook) has only lock and outbox/: its entries carry their git_dir and link, and the flush
replays them through events.replay.
"""

import contextlib
import fcntl
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from . import claude, config, core, events

UNACKED_AFTER = 15 * 60
IDLE_AFTER = 10 * 60
BRANCH = re.compile(r"(feat|fix|chore|deps)/[a-z0-9][a-z0-9._-]*")
ORIGIN = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
ERRORS = (core.CommandError, core.LaunchError, OSError, ValueError, KeyError, TypeError,
          AttributeError, IndexError, sqlite3.Error)


def runs_dir():
    return config.data_dir() / "runs"


def log_path():
    return core.log_path("runs")


def log(line):
    core.log("runs", line)


def run_dir(card):
    return runs_dir() / card


def load(card):
    return json.loads((run_dir(card) / "run.json").read_text())


def enqueue(card, event, detail, **issue):
    """Save one event before any kanban call, so a failed or killed delivery is tried again. Returns its path.

    An issue run's event (events.hook) has no run.json: it carries its git_dir and link instead.
    """
    if "git_dir" not in issue and not (run_dir(card) / "run.json").is_file():
        raise core.LaunchError(f"no run {card}")
    ns = time.time_ns()
    path = run_dir(card) / "outbox" / f"{ns}-{event}.json"
    # The key is fixed here, so a redelivery finds the wait card the first attempt made.
    core.save_json(path, {"event": event, "detail": detail, "key": f"{card}:wait:{ns}", "at": int(time.time()),
                          **issue})
    return path


def pending(card):
    """Saved events not yet delivered, oldest first."""
    return sorted((run_dir(card) / "outbox").glob("*.json"))


def sent(card):
    """Delivered events still waiting for the gateway's ack."""
    return sorted((run_dir(card) / "sent").glob("*.json"))


def wait_card(directory):
    path = directory / core.WAIT_KIND
    return (path.read_text().strip() or None) if path.is_file() else None


def verify(run):
    """(pull request, None) when the run's pull request is its finished result, else (None, why).

    Raises CommandError when git or gh cannot answer: unknown is never success.
    """
    tree = run["worktree"]
    head = core.run(["git", "-C", tree, "rev-parse", "HEAD"]).strip()
    dirty = core.run(["git", "-C", tree, "status", "--porcelain", "--untracked-files=no"]).strip()
    prs = [pr for pr in json.loads(core.run(
        ["gh", "pr", "list", "-R", run["repo"], "--head", run["branch"], "--state", "all",
         "--json", "url,state,headRefOid,baseRefName,isDraft"])) if pr["state"] in ("OPEN", "MERGED")]
    if len(prs) != 1:
        return None, ("there is no open pull request yet" if not prs
                      else f"{len(prs)} pull requests are open from this branch")
    pr = prs[0]
    if pr["baseRefName"] != run["base"]:
        return None, f"{pr['url']} targets {pr['baseRefName']}, not {run['base']}"
    if pr["isDraft"]:
        return None, f"{pr['url']} is still a draft"
    if pr["headRefOid"] != head:
        return None, f"the newest commits are not pushed to {pr['url']}"
    if dirty:
        return None, "some changes are not committed"
    return pr, None


def complete(run, pr):
    card = run["card"]
    if events.status(card) != "done":
        # One line (the board keeps a completion's first summary line only), stating only what gh said.
        # Provenance stays on the card's links comment and in run.json.
        summary = (f"Merged: {pr['url']} (not checked live)" if pr["state"] == "MERGED"
                   else f"Ready for review: {pr['url']} (open; not merged or deployed)")
        core.kanban("complete", card, "--summary", summary)
        events.expect(card, "done", "complete")
    events.close_wait(run_dir(card), "complete")
    return card


def clear_dead_claim(directory):
    """An empty wait marker is a claim whose creator died before recording its card. Under the run
    lock no other hook can be mid-create, so clear it; the entry's fixed key finds that card."""
    path = directory / core.WAIT_KIND
    if path.is_file() and not path.read_text().strip():
        path.unlink()


def deliver(run, entry):
    """One saved event's kanban moves, each read back. Returns the card whose notifier event must be
    acked, or None when the move raised none. Safe to repeat: every move reads state first."""
    card, event, directory = run["card"], entry["event"], run_dir(run["card"])
    ledger = events.status(card)
    if ledger == "archived":
        return None
    if event == "proposal":
        events.post_proposal(card, entry["proposal"])
        return None  # a comment is no notifier event: nothing to ack
    if event == "prompt":
        events.close_wait(directory, event)
        return None
    clear_dead_claim(directory)
    if event == "notification":
        events.open_wait(directory, run, entry["detail"], entry["key"], entry.get("ask"), entry.get("proposal"))
        return wait_card(directory)
    if ledger == "done":
        return card  # a late hook, or a redelivery after a kill: its completion still needs its ack
    pr, why = verify(run)
    if pr:
        return complete(run, pr)
    if event == "stop":
        return None  # a turn end is not success, and not a wait: the flush pages if it stays idle
    if event == "idle":
        events.open_wait(directory, run, f"{entry['detail']} and is not finished: {why}.", entry["key"])
        return wait_card(directory)
    # session-end: the agent is gone without a finished pull request.
    events.close_wait(directory, event)
    if ledger == "ready":
        core.kanban("block", "--kind", "needs_input", card,
                    f"The agent's session ended without a finished pull request: {why}.\n"
                    f"Check Herdr pane {run['pane']}.")
        events.expect(card, "blocked", event)
    return card


def last_event(card):
    """Id of the card's newest blocked or completed event, read-only from the board db; 0 if none."""
    with contextlib.closing(sqlite3.connect(f"file:{core.board_db()}?mode=ro", uri=True)) as db:
        row = db.execute("SELECT max(id) FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'completed')",
                         (card,)).fetchone()
    return row[0] or 0


def acked(card):
    """True once every notify+wake subscription pinged the human and claimed the wake past the card's newest event.

    No event found is not acked: the move happened, so a missing event means we cannot tell. The
    notifier delivers an archived card's pending events, then drops its subscriptions, so an archived
    card with none left was delivered (or its chat was dropped as dead after 12 failed sends).
    """
    want = last_event(card)
    subs = [s for s in json.loads(core.kanban("notify-list", card, "--json"))
            if s.get("delivery_mode") == "notify+wake"]
    if not subs:
        return events.status(card) == "archived"
    return bool(want) and all(
        s.get("last_event_id", 0) >= want and s.get("last_ping_event_id", 0) >= want for s in subs)


def note(card, path, entry, error):
    """Log a failure once per distinct message, and keep it on the entry for debugging."""
    text = " ".join(str(error).split())
    if entry.get("error") != text:
        log(f"{card} {entry.get('event')}: {text}")
        with contextlib.suppress(OSError):
            core.save_json(path, {**entry, "error": text})


def read_entry(card, path):
    """A saved event, or None after dropping a file a crash cut short (or that is no event): it would wedge the queue."""
    try:
        entry = json.loads(path.read_text())
        if isinstance(entry, dict) and "event" in entry:
            return entry
        error = "not an event"
    except ValueError as caught:
        error = caught
    log(f"{card} {path.parent.name}/{path.name}: unreadable, dropped: {error}")
    path.unlink(missing_ok=True)
    return None


def drain(card, wait=False):
    """Deliver every saved event of one run in order, then check the acks of the delivered ones.

    A delivery failure stops the queue: a prompt must not overtake the wait it closes. A process
    killed between a move and its record redelivers; every move reads state first, so none repeats.
    An issue run's events (no run.json) are replayed by events.replay and need no ack. False when
    another hook or the flush held the lock (`wait` blocks for it instead).
    """
    directory = run_dir(card)
    with open(directory / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            return False  # the holder or the next flush delivers what is left
        run = load(card) if (directory / "run.json").is_file() else None
        for path in pending(card):
            entry = read_entry(card, path)
            if entry is None:
                continue
            try:
                ack = deliver(run, entry) if run else events.replay(entry)
                if ack:
                    core.save_json(directory / "sent" / path.name,
                                   {**entry, "ack": ack, "moved_at": int(time.time()), "error": None})
                path.unlink()
            except ERRORS as error:
                note(card, path, entry, error)
                if entry["event"] == "stop":
                    continue  # it only checks for a finished PR: a later ask must not wait behind it
                return True
            log(f"{card} {entry['event']}: delivered, ack {ack}")
        for path in sent(card):
            entry = read_entry(card, path)
            if entry is None:
                continue
            try:
                if acked(entry["ack"]):
                    path.unlink()
                    log(f"{card} {entry['event']}: card {entry['ack']} acked by the gateway")
                elif time.time() - entry["moved_at"] > UNACKED_AFTER and not entry.get("warned"):
                    core.save_json(path, {**entry, "warned": True})
                    log(f"{card} {entry['event']}: gateway has not acked card {entry['ack']} after 15 min")
            except ERRORS as error:
                note(card, path, entry, error)
    return True


def reconcile(run):
    """The backstop for a hook that never ran, and the page for an agent that stopped unfinished.

    Only while the ledger is ready, nothing is queued and no wait card is open. herdr shows the
    pane's agent: gone -> session-end; on a dialog -> notification; newly idle -> stop (completes it
    if its pull request is finished, else silent); idle and unchanged for IDLE_AFTER -> idle (pages).
    """
    card, directory = run["card"], run_dir(run["card"])
    if not run.get("launched") or pending(card) or (directory / core.WAIT_KIND).exists():
        return
    if events.status(card) != "ready":
        return
    try:
        agent = json.loads(core.run(["herdr", "agent", "get", run["pane"]]))["result"]["agent"]
    except core.CommandError as error:
        if "agent_not_found" not in str(error):
            raise  # herdr itself failed: unknown, try at the next flush
        enqueue(card, "session-end", "the pane's agent is gone")
        return
    if agent.get("agent") != core.adapter().KIND:
        enqueue(card, "session-end", f"the pane runs {agent.get('agent')}, not {core.adapter().KIND}")
        return
    state, idle_file = agent.get("agent_status"), directory / "idle.json"
    if state == "blocked":
        enqueue(card, "notification", "The agent is waiting on a dialog.")
    if state not in core.READY:
        idle_file.unlink(missing_ok=True)
        return
    # herdr keeps no idle-since time: the first flush that sees this state change starts the clock.
    try:
        idle = json.loads(idle_file.read_text()) if idle_file.is_file() else {}
    except ValueError:  # cut short by a crash: restart the clock
        idle = {}
    if not isinstance(idle, dict):
        idle = {}
    if idle.get("seq") != agent.get("state_change_seq"):
        core.save_json(idle_file, {"seq": agent.get("state_change_seq"), "since": int(time.time())})
        enqueue(card, "stop", "the agent is idle")
    elif time.time() - idle["since"] >= IDLE_AFTER and not idle.get("paged"):
        enqueue(card, "idle", f"The agent has been idle for {IDLE_AFTER // 60} minutes")
        core.save_json(idle_file, {**idle, "paged": True})  # after: a crash between the two pages twice, not never


def closed(card):
    """Nothing left to do: the ledger is done or archived, nothing queued, every ack seen or given up."""
    return (not pending(card) and all(json.loads(p.read_text()).get("warned") for p in sent(card))
            and events.status(card) in ("done", "archived"))


def flush(args=None):
    core.prepare_env()
    runs_dir().mkdir(parents=True, exist_ok=True)
    with open(runs_dir() / "flush.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        for directory in sorted(p for p in runs_dir().iterdir() if p.is_dir()):
            card = directory.name
            try:
                if not (directory / "run.json").is_file():
                    if pending(card):
                        drain(card)  # an issue run's hook events (events.hook): nothing to reconcile
                    continue
                if load(card).get("closed") and not pending(card):
                    continue
                drain(card)
                reconcile(load(card))
                drain(card)
                if closed(card):
                    with core.launch_lock(directory):  # relaunch saves run.json under it; busy: the next flush
                        core.save_json(directory / "run.json", {**load(card), "closed": True})
            except ERRORS as error:
                log(f"{card} flush: {error}")
    return 0


def footer(run):
    return f"""
## This run: {run['card']}

You are in a herdr worktree of {run['repo']} on branch `{run['branch']}`, cut from origin/{run['base']}.
Kanban card {run['card']} tracks this run. You never report it yourself: each time you end a turn,
the run checks GitHub. It is finished only when exactly one open (or merged), non-draft pull request
from `{run['branch']}` into `{run['base']}` has your HEAD as its head and no tracked file has
uncommitted changes. Then the human is told, with the pull request's link.

When you need a decision or a fact you cannot read, ask the human with the AskUserQuestion tool and
wait: that pings them. A question in plain text does not. Before you ask the human to approve a design
or plan, write all of it to a file (approach, scope and non-goals, safety boundaries, trade-offs, the
test plan, the decision you need; no secrets) and run
`{config.hermes_bin()} muster hook --card {run['card']} propose <file>`, again after every revision.
Then ask with AskUserQuestion, giving the approval question the header `Approval`: without a saved
proposal that question is refused. The human reviews from the cards, not your pane. Never push to {run['base']}, merge, deploy
or force-push.
"""


def relaunch(run, prepare_text):
    """Every launch step after the card and its subscription, safe to repeat: the run with its pane."""
    card, directory = run["card"], run_dir(run["card"])

    def save(rec):
        run["launch"] = rec
        run.update(workspace=rec["workspace"], worktree=rec["path"], pane=rec["pane"])
        core.save_json(directory / "run.json", run)

    def prepare(rec, git_dir):
        run.update(workspace=rec["workspace"], worktree=rec["path"], pane=rec["pane"])
        prompt = prepare_text.rstrip() + "\n" + footer(run)
        (directory / "brief.md").write_text(prompt)  # the audit copy of what the agent was told
        Path(rec["settings"]).write_text(json.dumps(core.adapter().hook_settings([*core.hook_cmd(), "--card", card]), indent=2))
        return prompt

    def known(git_dir):
        """A run record from before launch records names its own worktree: that checkout is this run's."""
        old = run.get("worktree")
        if old and Path(old).is_dir() and core.owner_of(old)[0] == Path(git_dir):
            return f"run {card}"
        return None

    run["state"] = core.ensure(run["launch"], save, directory, prepare, known)
    run["launched"] = True
    save(run["launch"])
    # The pane runs now: a failed comment must not block it. run.json holds the same links.
    try:
        core.kanban("comment", card, f"{core.LINKS_PREFIX} " + json.dumps(
            {k: run[k] for k in ("card", "repo", "branch", "pane", "workspace", "worktree")}))
    except ERRORS as error:
        log(f"{card} launch: links comment failed: {error}")
    return run


def launch_run(clone, branch, title, brief, base=None, model=None):
    """Register the run (card, run.json, subscription), then open its pane. Returns run.json's content.

    Raises LaunchError. After the card exists, a failed step blocks it, which pings the human and wakes the agent.
    """
    clone = Path(clone).expanduser().resolve()
    if not BRANCH.fullmatch(branch):
        raise core.LaunchError(f"branch {branch!r} is not <feat|fix|chore|deps>/<name>")
    if not title.strip():
        raise core.LaunchError("the title is empty")
    text = Path(brief).read_text()
    if not text.strip():
        raise core.LaunchError(f"the brief {brief} is empty")
    origin = core.run(["git", "-C", str(clone), "remote", "get-url", "origin"]).strip()
    match = ORIGIN.search(origin)
    if not match:
        raise core.LaunchError(f"{clone}: origin {origin} is not a GitHub repository")
    repo = match.group(1)
    configured = next((b for slug, (_, _, b) in config.repos().items() if slug.lower() == repo.lower()), None)
    base, why = core.base_of(clone, base or configured)
    # Every input is checked before the card: the plan refuses a bad branch or agent name.
    name = core.agent_name("run", f"{repo.split('/', 1)[1]}-{branch.split('/', 1)[1]}")  # one branch, two repos
    plan = core.plan(None, repo, clone, branch, base, title[:40], name,
                     model or config.settings["agent_model"], "", "run", core.pane_env())
    if why:
        core.note(plan, why)
    runs_dir().mkdir(parents=True, exist_ok=True)
    with open(runs_dir() / "launch.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # hermes' idempotency check is not atomic
        before = int(time.time())
        task = json.loads(core.kanban(
            "create", "--body", f"ad-hoc run {repo} {branch}\n{core.PROVENANCE}",
            "--idempotency-key", f"run:{repo}:{branch}", "--created-by", core.CREATED_BY, "--json", "--", title))
        card, directory = task["id"], run_dir(task["id"])
        # A card made before this call, or in the same second by a launch that already registered it.
        if task["created_at"] < before or (directory / "run.json").exists():
            raise core.LaunchError(f"{repo} {branch} already has run {card} ({task.get('status')}); "
                                   f"use a new branch, or `hermes muster recover {card}`")
        plan.update(owner=card, settings=str(directory / "settings.json"))
        run = {"card": card, "repo": repo, "clone": str(clone), "branch": branch, "base": base, "title": title,
               "launch": plan}
        core.save_json(directory / "run.json", run)
        (directory / "caller-brief.md").write_text(text)  # what a recover rebuilds the brief from
    step = "subscribe"
    try:
        with core.launch_lock(directory):
            core.subscribe(card)
            step = "launch"
            run = relaunch(run, text)
    except BaseException as error:  # a killed launch (Ctrl-C, a caller's timeout) still blocks its card
        step = run["launch"].get("phase") or step if step == "launch" else step
        reason = f"launch failed at {step}: {' '.join(str(error).split()) or type(error).__name__}"
        try:
            core.report_failure(card, step, error)
        except ERRORS as block_error:
            reason += f"; could not block {card}: {' '.join(str(block_error).split())}"
        if not isinstance(error, ERRORS):
            raise
        raise core.LaunchError(reason) from None
    return run


def launch(args):
    core.prepare_env()
    config.require()
    try:
        run = launch_run(args.cwd, args.branch, args.title, args.brief, args.base,
                         args.model)
    except ERRORS as error:
        print(f"muster launch: {' '.join(str(error).split())}", file=sys.stderr)
        return 1
    print(json.dumps(run))
    return 0


def caller_brief(directory):
    """The caller's own brief: saved at launch, or for an older run the audit copy without its footer."""
    if (directory / "caller-brief.md").is_file():
        return (directory / "caller-brief.md").read_text()
    return (directory / "brief.md").read_text().split("\n## This run: ", 1)[0]


def recover(card, resend=False, adopt=False):
    """Resume a failed launch of one run. 0 once its brief is delivered; the reason on stderr otherwise."""
    directory = run_dir(card)
    if not (directory / "run.json").is_file():
        print(f"recover {card}: no run {directory}", file=sys.stderr)
        return 1
    step = "record"
    try:
        with core.launch_lock(directory):
            run = load(card)
            step = "card"
            status = core.recoverable(card)
            if "launch" not in run:
                # A run from before launch records: what it delivered is not recorded. Only a person may
                # adopt it, and a live agent in its pane then counts as an unknown delivery. Such a run was
                # named before names carried the repo.
                if not adopt:
                    raise core.LaunchFailure("refused", f"run {card} predates launch records. Check its "
                                                        f"worktree and pane, then recover with --adopt")
                plan = core.plan(card, run["repo"], run["clone"], run["branch"], run["base"], run["title"][:40],
                                 core.agent_name("run", run["branch"].split("/", 1)[1]), config.settings["agent_model"],
                                 directory / "settings.json", "run", core.pane_env())
                live = core.agent_at(run["pane"]) if run.get("pane") else None
                plan.update(workspace=run.get("workspace"), pane=run.get("pane") if live else None,
                            prompt={"state": "unknown", "at": None, "seq": None} if live else None)
                run["launch"] = plan
            step = "pull requests"
            prs = core.pull_requests(run["repo"], run["branch"])
            if prs:
                raise core.LaunchFailure("refused", f"{prs[0]} is already open or merged from {run['branch']}; "
                                                    f"nothing to relaunch")
            # The recorded env pairs were fixed at launch; the hermes homes may have moved since.
            run["launch"]["env"] = core.pane_env()
            if resend:
                run["launch"]["resend"] = True
            if adopt:
                run["launch"]["adopt"] = True
            step = "launch"
            text = caller_brief(directory)
            run = relaunch(run, text)
            if status == "blocked":
                core.kanban("unblock", card)  # once: recover never blocks again (report_failure)
    except ERRORS as error:
        if step == "launch":
            step = run["launch"].get("phase") or step
        print(f"recover {card}: failed at {step}: {' '.join(str(error).split())}", file=sys.stderr)
        if step not in ("record", "card") and getattr(error, "kind", None) != "busy":
            with contextlib.suppress(*ERRORS):
                core.report_failure(card, step, error)
        return 1
    print(json.dumps({k: run.get(k) for k in ("card", "pane", "worktree", "state")}))
    return 0


def hook(args):
    """A hook of the run's pane. Never fails the agent; prints only a denied approval request's decision."""
    card, event = args.card, args.event
    if event == "done":  # an ad-hoc run finishes by its pull request; reading stdin here would hang
        print("done: an ad-hoc run is finished by its pull request, nothing to report", file=sys.stderr)
        return 1
    if event == "propose":  # run by the agent from its shell: no hook payload on stdin
        if not (run_dir(card) / "run.json").is_file():
            print(f"propose card {card}: no run {card}", file=sys.stderr)
            return 1
        core.prepare_env()
        return events.propose(card, args.url)
    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if claude.ignore(event, payload):
            return 0  # /clear ends a session, but the agent keeps working in the same pane
        if event == "prompt":
            core.prompt_seen(run_dir(card), payload)  # the launch's evidence that its brief arrived
        if event == "prompt" and not (run_dir(card) / core.WAIT_KIND).exists() and not pending(card):
            return 0  # every PostToolUse lands here: nothing open, nothing queued, nothing to do
        core.prepare_env()
        try:
            pin, why = events.gate(card, run_dir(card), event, payload,
                                   f"{config.hermes_bin()} muster hook --card {card} propose <file>")
        except Exception as error:  # noqa: BLE001 - fail closed, and say why
            pin, why = None, f"muster could not check this approval request: {' '.join(str(error).split())}"
        if why:
            log(f"{card} hook {event}: approval request denied: {why}")
            return events.deny(why)
        queued = pending(card)
        if not (event == "prompt" and queued and queued[-1].name.endswith("-prompt.json")):
            events.enqueue(card, event, claude.detail(payload), payload, pin)  # one queued prompt is enough
        drain(card)
    except Exception as error:  # a hook must never crash the agent; the flush retries what was saved
        with contextlib.suppress(Exception):
            log(f"{card} hook {event}: {error}")
    return 0
