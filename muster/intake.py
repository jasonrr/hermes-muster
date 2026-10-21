"""intake: the issue route. An open issue labeled with the approving label BY THE APPROVER gets one ledger card and one herdr pane.
So does a bot's own issue that an auto_approvers entry approves (automatic()).

Cron runs `hermes muster tick` every minute. GitHub is read through the operator's own `gh` login.
The card lives on the configured kanban board with NO assignee, so no dispatcher ever runs it: it
is the ledger and the event carrier. It is keyed by the label event, so one approval makes one card
and one pane. A card made this tick is subscribed notify+wake to the configured chat: on every
blocked or completed event the gateway pings the human and queues a fresh agent turn. Then a herdr
worktree of the repository's local clone gets a new tab in which an interactive coding agent runs.
The pane's own agent settings carry the hooks that move the card. The card is subscribed first, so
a later launch step that fails blocks it and pings the human; a failed subscribe or block is only in
the cron log. Nothing is retried until the approver labels again.
"""

import contextlib
import fcntl
import json
import sys
import time
import urllib.parse
from pathlib import Path

from . import config, core, launcher


def approval(events):
    """The newest approving label event, if the approver applied it; otherwise None.

    Newest wins: a label removed and re-applied by anyone else after the
    approver's label refuses the issue, and a label the approver never applied
    never passes. Login (any case) and id must both match.
    """
    s = config.settings
    labeled = [event for event in events
               if event.get("event") == "labeled" and ((event.get("label") or {}).get("name") or "").lower() == s["label"].lower()]
    if not labeled:
        return None
    newest = labeled[-1]
    actor = newest.get("actor") or {}
    if actor.get("id") != int(s["approver_id"]) or str(actor.get("login", "")).lower() != s["approver_login"].lower():
        return None
    return newest


def same_account(user, entry):
    return (user or {}).get("id") == entry["id"] and str((user or {}).get("login", "")).lower() == entry["login"].lower()


def automatic(repo, issue, events):
    """(the newest approving label event, who) if an auto_approvers entry approves the issue; otherwise None.

    First entry wins; all must hold: the repo is the entry's, the entry's account opened the issue, the
    issue still carries the entry's label, and that account made the newest event of both labels.
    """
    for entry in config.settings["auto_approvers"]:
        who = {"login": entry["login"], "id": entry["id"], "label": entry.get("label", config.AUTO_LABEL)}
        names = {config.settings["label"].lower(), who["label"].lower()}
        if (repo.lower() not in {str(r).lower() for r in entry["repos"]}
                or not same_account(issue.get("user"), who)
                or not any((label.get("name") or "").lower() == who["label"].lower() for label in issue.get("labels", []))):
            continue
        newest = {}
        for event in events:
            name = ((event.get("label") or {}).get("name") or "").lower()
            if event.get("event") == "labeled" and name in names:
                newest[name] = event
        if len(newest) == len(names) and all(same_account(e.get("actor"), who) for e in newest.values()):
            return newest[config.settings["label"].lower()], who
    return None


def approve(repo, issue, events):
    """(approving event, who) — who is None for the human approver; (None, None) when nothing approves."""
    event = approval(events)
    if event is not None:
        return event, None
    return automatic(repo, issue, events) or (None, None)


def is_bug(issue):
    return any(label.get("name") == config.settings["bug_label"] for label in issue.get("labels", []))


def approved_by(repo, number, auto=None):
    """Who approved, for the brief and the card. An automatic approval never says the approver labeled it."""
    s = config.settings
    if auto:
        return (f"Issue {repo}#{number} was approved automatically. {auto['login']} (id {auto['id']}) opened it "
                f"with labels `{s['label']}` and `{auto['label']}`, under {s['approver_login']}'s standing rule. "
                f"{s['approver_login']} did not label it.")
    return f"{s['approver_login']} approved issue {repo}#{number} for work by labeling it `{s['label']}`."


def brief(repo, number, bug, base="main", auto=None):
    """What the pane agent reads first. Fixed text and numbers only: issue text never enters it."""
    s = config.settings
    bot = (f" `gh` and `git push` act as the login configured in `{core.gh_config_dir()}`." if s["gh_config_dir"] else "")
    return config.prompt("issue-dispatch", repo=repo, number=number, approved_by=approved_by(repo, number, auto),
                         bot=bot, branch=f"{s['branch_prefix']}{number}", base=base, kind="bug" if bug else "feature",
                         hermes=config.hermes_bin(), workflow=config.workflow_text(),
                         ask=config.prompt("ask-the-human", hook=f"{config.hermes_bin()} muster hook"),
                         production=config.production_note(repo)) + "\n"


def card_argv(repo, issue, event, auto=None):
    number = issue["number"]
    return [
        "hermes", "kanban", "--board", config.settings["board"], "create",
        "--body", f"{repo}#{number}: https://github.com/{repo}/issues/{number}\n"
                  + (f"{approved_by(repo, number, auto)} Label event {event['id']}.\n" if auto else "")
                  + core.PROVENANCE,
        "--idempotency-key", f"{repo}#{number}@{event['id']}",
        "--created-by", core.CREATED_BY,
        "--json",
        # "--" so a title such as "-hotfix" is never read as a flag by argparse.
        "--", issue["title"],
    ]


def timeline(repo, number):
    out = core.run(["gh", "api", "--paginate", "--jq", ".[]",
               f"repos/{repo}/issues/{number}/timeline?per_page=100"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def agent_settings():
    """The pane's own agent settings: the muster hooks. Nothing global changes."""
    return core.adapter().hook_settings(core.hook_cmd())


def project_status(repo, number):
    """Set the issue's Status in the configured GitHub Project; one line saying what happened, or None when unset.
    Ids are resolved every launch, never hard-coded."""
    s = config.settings
    if not (s["project_owner"] and s["project_number"]):
        return None
    where = [str(s["project_number"]), "--owner", s["project_owner"]]
    project = json.loads(core.run(["gh", "project", "view", *where, "--format", "json"]))
    fields = json.loads(core.run(["gh", "project", "field-list", *where, "-L", "100", "--format", "json"]))["fields"]
    field = next((f for f in fields if f.get("name") == s["project_status_field"]), None)
    option = next((o for o in (field or {}).get("options") or [] if o.get("name") == s["project_status_value"]), None)
    if option is None:
        return "field/option not found"
    # ponytail: the first 300 items only, as rc_intake did; page with --limit if a Project outgrows it.
    items = json.loads(core.run(["gh", "project", "item-list", *where, "-L", "300", "--format", "json"]))["items"]
    url = f"https://github.com/{repo}/issues/{number}".lower()  # a draft item has no url
    item = next((i for i in items if str((i.get("content") or {}).get("url", "")).lower() == url), None)
    if item is None:
        return f"not in {'the first 300 items of ' if len(items) >= 300 else ''}Project #{s['project_number']}"
    core.run(["gh", "project", "item-edit", "--project-id", project["id"], "--id", item["id"],
         "--field-id", field["id"], "--single-select-option-id", option["id"]])
    return s["project_status_value"]


def intake_known(repo, number):
    """A checkout of the same issue from before launch records: its links file names it."""
    def known(git_dir):
        path = Path(git_dir) / core.CARD_FILE
        link = json.loads(path.read_text()) if path.is_file() else {}
        if link.get("repo") == repo and link.get("issue") == number:
            return f"muster card {link.get('card')}"
        return None
    return known


def links(record, rec):
    return {"card": record["card"], "repo": record["repo"], "issue": record["issue"], "title": record["title"],
            "pane": rec["pane"], "workspace": rec["workspace"], "worktree": rec["path"], "base": rec["base"], "branch": rec["branch"],
            "launch_dir": str(core.intake_dir() / record["card"])}


def relaunch(record, directory, at):
    """Every step after the card is subscribed and checked, safe to repeat. Returns (prompt state, pane)."""
    repo, number, card = record["repo"], record["issue"], record["card"]

    def save(rec):
        record["launch"] = rec
        core.save_json(directory / "launch.json", record)

    at["step"] = "pull requests"
    prs = launcher.pull_requests(repo, record["launch"]["branch"])
    if prs:
        raise launcher.LaunchFailure("refused", f"{prs[0]} is already open or merged from {record['launch']['branch']}; "
                                       f"nothing to relaunch")

    def prepare(rec, git_dir):
        text = brief(repo, number, record["bug"], rec["base"], record.get("auto"))  # no "auto" before #2
        (git_dir / core.BRIEF_FILE).write_text(text)  # the audit copy of what the agent was told
        Path(rec["settings"]).write_text(json.dumps(agent_settings(), indent=2))
        (git_dir / core.CARD_FILE).write_text(json.dumps(links(record, rec)))
        return text

    at["step"] = "launch"
    state = launcher.ensure(record["launch"], save, directory, prepare, intake_known(repo, number))
    at["step"] = "links"
    core.kanban("comment", card, f"{core.LINKS_PREFIX} " + json.dumps(links(record, record["launch"])))
    # Never fails the launch: what happened is one card comment and one log line.
    try:
        moved = project_status(repo, number)
    except (core.CommandError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        moved = f"failed: {' '.join(str(error).split())}"
    if moved:
        print(f"{repo}#{number} project: {moved}")
        with contextlib.suppress(core.CommandError, OSError):
            core.kanban("comment", card, f"project: {moved}")
    return state, record["launch"]["pane"]


def step_of(at, record):
    return (record["launch"].get("phase") or "launch") if at["step"] == "launch" and record else at["step"]


def launch(repo, issue, card, event=None, auto=None):
    """Open the pane for a card made this tick. Returns (prompt state, pane), or None after blocking the card."""
    number = issue["number"]
    clone, short, configured = config.repos()[repo]
    directory = core.intake_dir() / card
    at, record = {"step": "record"}, None
    try:
        with launcher.launch_lock(directory):
            base, why = launcher.base_of(clone, configured)
            record = {"card": card, "repo": repo, "issue": number, "title": issue["title"],
                      "event": (event or {}).get("id"), "auto": auto, "bug": bool(auto) or is_bug(issue),
                      "created": issue.get("created_at"),
                      "launch": launcher.plan(card, repo, clone, f"{config.settings['branch_prefix']}{number}", base,
                                     f"{short}#{number}", launcher.agent_name("muster", f"{short}-{number}"),
                                     config.settings["agent_model"], directory / core.SETTINGS_FILE, "muster", launcher.pane_env())}
            if why:
                launcher.note(record["launch"], why)
            core.save_json(directory / "launch.json", record)
            # Subscribe first, so a block at any later step pings the human. The topic comes just before: the
            # subscription names it. topic() never raises.
            launcher.topic(card, repo, issue["title"], issue=number)
            at["step"] = "subscribe"
            core.subscribe(card, card)
            at["step"] = "card readback"
            task = json.loads(core.kanban("show", card, "--json"))["task"]
            if task.get("status") != "ready" or task.get("assignee"):
                raise core.LaunchError(f"card is {task.get('status')} assigned to {task.get('assignee')}")
            if config.settings["gh_config_dir"]:
                at["step"] = "bot identity"
                hosts = core.gh_config_dir() / "hosts.yml"
                if not hosts.is_file():
                    raise core.LaunchError(f"gh-bot login missing: {hosts}")
            return relaunch(record, directory, at)
    except (core.CommandError, core.LaunchError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        step = step_of(at, record)
        print(f"{repo}#{number}: launch failed at {step}: {' '.join(str(error).split())}")
        if record:
            with contextlib.suppress(OSError):
                core.save_json(directory / "launch.json", record)
        try:
            launcher.report_failure(card, step, error)
        except (core.CommandError, OSError, ValueError, KeyError) as block_error:
            print(f"{repo}#{number}: could not block {card}: {block_error}")
        return None


def start():
    """What tick and recover do first: the env, the config, and the lock file's directory."""
    core.prepare_env()
    config.require()
    core.lock_path("tick").parent.mkdir(parents=True, exist_ok=True)


def recover(args):
    """Resume a failed intake launch of one card, under its current approval. 0 once its brief is delivered."""
    start()
    with open(core.lock_path("tick"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # never beside a cron tick: both may touch the issue branch
        core.board_exists()
        if (config.data_dir() / "runs" / args.card / "run.json").is_file():  # an issue card may have an outbox there
            from . import adhoc
            return adhoc.recover(args.card, args.resend, args.adopt)
        from . import hold
        if hold.path(args.card).is_file() and not (core.intake_dir() / args.card / "launch.json").is_file():
            try:
                return hold.release(args.card)
            except (hold.FAILURES) as error:
                print(f"recover {args.card}: {' '.join(str(error).split())}", file=sys.stderr)
                return 1
        return recover_card(args.card, args.resend, args.adopt)


def recover_card(card, resend=False, adopt=False):
    directory = core.intake_dir() / card
    if not (directory / "launch.json").is_file():
        print(f"recover {card}: no launch record {directory / 'launch.json'}. A card from before launch records "
              f"cannot be resumed: remove and re-apply {config.settings['label']}; the new card's launch reuses "
              f"the old checkout", file=sys.stderr)
        return 1
    at, record = {"step": "record"}, None
    try:
        with launcher.launch_lock(directory):
            loaded = json.loads((directory / "launch.json").read_text())
            if not isinstance(loaded.get("launch"), dict):  # bound only once valid: the handler below reads it
                print(f"recover {card}: {directory / 'launch.json'} has no launch record; remove and re-apply "
                      f"{config.settings['label']}", file=sys.stderr)
                return 1
            record = loaded
            repo, number = record["repo"], record["issue"]
            at["step"] = "card"
            status = launcher.recoverable(card)
            at["step"] = "approval"
            label = config.settings["label"]
            issue = json.loads(core.run(["gh", "api", f"repos/{repo}/issues/{number}"]))
            if issue.get("state") != "open" or not any(lb.get("name") == label for lb in issue.get("labels", [])):
                raise launcher.LaunchFailure("refused", f"{repo}#{number} is {issue.get('state')} or no longer labeled {label}")
            current, auto = approve(repo, issue, timeline(repo, number))
            if current is None or current.get("id") != record["event"]:
                raise launcher.LaunchFailure("refused", f"the approval of {repo}#{number} changed: this card's label event is "
                                               f"{record['event']}, the current approving one is "
                                               f"{(current or {}).get('id', 'none (revoked)')}")
            record["auto"] = auto
            # The recorded env pairs were fixed at launch; the hermes homes may have moved since.
            record["launch"]["env"] = launcher.pane_env()
            if resend:
                record["launch"]["resend"] = True
            if adopt:
                record["launch"]["adopt"] = True
            state, pane = relaunch(record, directory, at)
            if status == "blocked":
                core.kanban("unblock", card)  # once: recover never blocks again (report_failure)
            print(f"{repo}#{number} task {card} pane {pane} recovered (first prompt: {state})")
            return 0
    except (core.CommandError, core.LaunchError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        step = step_of(at, record)
        print(f"recover {card}: failed at {step}: {' '.join(str(error).split())}", file=sys.stderr)
        if record:
            record["launch"].pop("resend", None)
            with contextlib.suppress(OSError):
                core.save_json(directory / "launch.json", record)
        if getattr(error, "kind", None) != "busy" and at["step"] not in ("record", "card"):
            try:
                launcher.report_failure(card, step, error)
            except (core.CommandError, OSError, ValueError, KeyError) as report_error:
                print(f"recover {card}: could not note the failure on the card: {report_error}", file=sys.stderr)
        return 1


def intake(repo, dry=False):
    """One card and one pane per newly approved open issue in one repository; an automatic approval soon after
    another is held instead (muster.hold). True if anything failed."""
    from . import hold

    clone = config.repos()[repo][0]
    if not (clone / ".git").exists():
        print(f"{repo}: skipped, clone {clone} has no .git")
        return False
    failed = hold.sweep(repo, dry)
    label = config.settings["label"]
    # REST, not `gh issue list`: that one is GraphQL, whose quota is far smaller.
    # The REST issues endpoint also returns pull requests; those carry `pull_request`.
    out = core.run(["gh", "api", "--paginate", "--jq", ".[]",
               f"repos/{repo}/issues?labels={urllib.parse.quote(label)}&state=open&per_page=100"])
    issues = [i for i in (json.loads(line) for line in out.splitlines() if line.strip())
              if "pull_request" not in i]
    # Oldest first: an issue is judged against the runs launched before it.
    for issue in sorted(issues, key=lambda item: (item.get("created_at") or "", item["number"])):
        number = issue["number"]
        try:
            event, auto = approve(repo, issue, timeline(repo, number))
            if event is None:
                print(f"{repo}#{number} skipped: newest {label} label is not {config.settings['approver_login']}'s "
                      f"and no auto_approvers entry approves it")
                continue
            if hold.resolved(repo, number, event["id"]):
                print(f"{repo}#{number} skipped: approval event {event['id']} already resolved; a new label event "
                      f"is needed")
                continue
            if dry and auto and hold.live(repo):
                print(f"{repo}#{number} would judge against "
                      + ", ".join(f"#{c['issue']}" for c in hold.live(repo)))
                continue
            if dry:
                print(f"{repo}#{number} would launch{' automatically' if auto else ''}: {repo}#{number}@{event['id']} "
                      f"({'bug' if auto or is_bug(issue) else 'feature'})")
                continue
            before = int(time.time())
            task = json.loads(core.run(card_argv(repo, issue, event, auto)))
            # The create is idempotent: an existing card comes back with its old created_at,
            # and only a card made just now gets a pane.
            # ...unless a tick was killed between that create and launch()'s first save.
            if hold.path(task["id"]).is_file():
                continue  # hold.sweep owns it
            if task["created_at"] < before and (task.get("status") != "ready"
                                                or (core.intake_dir() / task["id"] / "launch.json").is_file()):
                print(f"{repo}#{number} task {task['id']} ({task.get('status')}) card exists")
                continue
            unjudged = None
            cands = hold.live(repo, task["id"]) if auto else []
            if cands:
                try:
                    duplicate, reason = hold.judge(repo, issue, cands)
                except hold.Unjudged as why:
                    duplicate, unjudged = None, str(why)
                if duplicate is not None:
                    cand = next(c for c in cands if c["issue"] == duplicate)
                    hold.hold(repo, issue, task["id"], event, auto, cand, reason)
                    print(f"{repo}#{number} task {task['id']} held: possible duplicate of #{duplicate}")
                    continue
            launched = launch(repo, issue, task["id"], event, auto)
            if unjudged:  # launched as before; the card says why it was not judged
                print(f"{repo}#{number}: dedupe check failed: {unjudged}")
                with contextlib.suppress(core.CommandError, OSError):
                    core.kanban("comment", task["id"], f"dedupe check failed: {unjudged}; launched without it")
            if launched is None:
                failed = True
                continue
            state, pane = launched
            print(f"{repo}#{number} task {task['id']} pane {pane} (first prompt: {state})")
        except (core.CommandError, core.LaunchError, OSError, ValueError, KeyError, AttributeError, TypeError) as error:
            # One bad issue is named and the rest of the repository still runs.
            print(f"{repo}#{number}: {error}")
            failed = True
    return failed


def tick(args):
    start()
    with open(core.lock_path("tick"), "w") as lock:
        # Kanban's idempotency check is not atomic, and a launch can outlast a minute:
        # a second tick must not race the first one's create.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        core.board_exists()
        failed = False
        for repo in config.repos():
            try:
                failed = intake(repo, dry=args.dry_run) or failed
            except (core.CommandError, OSError, ValueError, KeyError) as error:
                print(f"{repo}: {error}")
                failed = True
    return 1 if failed else 0
