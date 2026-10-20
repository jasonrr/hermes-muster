"""events: agent hooks in a muster pane turn waits and endings into kanban events.

Registered only by the pane's own --settings file (intake.agent_settings); a no-op in any checkout
without <git dir>/muster-card.json. The gateway notifier turns each block or completion into a
Telegram ping for the human and a queued agent turn.

  notification   open a WAIT card (subscribed notify+wake, or none when muster pages it itself; blocked "<message>")
                 for a permission prompt or an AskUserQuestion (hook matchers in claude.hook_settings)
                 unless one is open or the ledger is archived (after done the pull request is in
                 review, so questions still page); its id is kept in <git dir>/muster-wait
  prompt         archive the open wait card (silent): the human answered, or (via PostToolUse,
                 registered on the same event) the agent resumed on its own
  session-end    block the ledger card, if it is still ready (not on /clear)
  done <PR url>  complete the ledger card (from ready or blocked), archive any open wait card;
                 refused unless gh names the run's branch as the pull request's head
  propose <file> save the agent's design as the next version, Proposal v<n> <sha256[:12]>,
                 in <data dir>/runs/<card>/proposals/ and as one ledger comment; once it reads back
                 there it is armed for the next approval request (an AskUserQuestion headed
                 `Approval`), whose wait card carries that version, its full text and every option.
                 The approval request is denied before its dialog shows (PreToolUse) while nothing
                 is armed or another wait card is open; the armed version is consumed only once a
                 wait card carries it
                 Each version also queues a `design` event: a design review card, subscribed `wake` in the run's
                 topic and completed, so the coordinator reviews that version (review; muster:escalation)

Every block reason and completion summary is written for the human to read whole: the board's
"human_notices" setting makes the gateway's Telegram ping lead with the card's title and show the
full text. Provenance (pane, branch, worktree, issue) stays on the card: its body and its links comment.

A card per wait because hermes routes a second same-kind block of one card to triage, where
unblock, block and complete all fail (tests/test_kanban_contract.py). The ledger card is blocked
at most once and completed once. Each event is saved to <data dir>/runs/<card>/outbox/ before any
kanban call and delivered from there in order (runs.drain, then replay here); one that does not land
in two tries stays queued, and the flush (runs.flush, every minute) delivers it. A hook never fails
the agent: the error goes to <data dir>/logs/events.log and it exits 0. Only `done` and `propose`
(run by the agent, not by a hook) print or exit non-zero, because a UserPromptSubmit hook's stdout
reaches the agent's context; the one other print is a denied approval request's PreToolUse decision.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import bridge, claude, config, core, decisions, launcher, progress

STALE_CLAIM = 120  # s: an empty wait marker this old is from a hook killed at its 30 s timeout
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/\d+")
# Bytes. Each text rides in one argv (the ledger comment, the wait card body), and Linux caps one argument at
# 128 KB: a body over it would fail every retry and hold the run's queue. 48 + 16 previews x 2 KB stays under.
PROPOSAL_MAX = 48 * 1024
PREVIEW_MAX = 2 * 1024


def log_path():
    return core.log_path("events")


def context(cwd):
    """(git dir, links) inside a muster worktree, else None."""
    try:
        git_dir = Path(core.run(["git", "-C", cwd, "rev-parse", "--absolute-git-dir"]).strip())
    except core.CommandError:
        return None
    path = git_dir / core.CARD_FILE
    return (git_dir, json.loads(path.read_text())) if path.is_file() else None


def status(card):
    return json.loads(core.kanban("show", card, "--json"))["task"]["status"]


def expect(card, want, event):
    got = status(card)
    if got != want:
        raise core.CommandError(f"{event}: card {card} read back {got}, wanted {want}")


class Unfinished(core.CommandError):
    """A revised build that is not a finished pull request yet: told to the agent once, never retried."""


def where(link):
    """(title prefix, body reference) of a wait card: a muster pane's issue, an ad-hoc run's branch."""
    if "issue" in link:
        return (f"{link['repo']}#{link['issue']}",
                f"issue https://github.com/{link['repo']}/issues/{link['issue']}")
    return f"{link['repo']} {link['branch']}", f"branch {link['branch']}"




def is_bridged(event, payload):
    """True when this hook event is a dialog the bridge can answer from the channel: an AskUserQuestion, or a
    permission prompt. Saved on the outbox entry so a redelivery decides the same way."""
    return event == "notification" and (payload.get("tool_name") == "AskUserQuestion"
                                        or payload.get("notification_type") == "permission_prompt")


def muster_pages(directory, link, bridged):
    """True when muster pages the human itself (its gateway sends the question with buttons), so the wait card takes
    no subscription: a Hermes ping, or a woken agent's own message, would be a second message for one decision
    (seen live: the human replied to the agent's message instead of the question's).

    Without a subscription nothing else pings, so every condition guards against a page nobody sends:
    - bridged: only a question or permission prompt gets a decision request (and its message);
      idle, reconcile and other waits have none, so Hermes must ping them.
    - a known channel (muster.channels): the gateway's buttons and replies exist only there.
    - the pane carries the PermissionRequest hook: an older pane never creates the request.
    - Hermes reports its gateway up with the platform connected: else nothing would deliver the request.
    """
    from . import channels

    if not bridged or config.settings["notify_platform"] not in channels.NAMES:
        return False
    settings = (core.intake_dir() / link["card"] / core.SETTINGS_FILE if "issue" in link
                else directory / "settings.json")
    try:
        return '"PermissionRequest"' in settings.read_text() and decisions.gateway_up()
    except OSError:
        return False


def open_wait(git_dir, link, detail, key, ask=None, proposal=None, bridged=False):
    ledger = status(link["card"])
    if ledger not in ("ready", "blocked", "done"):  # done: its pull request is in review, questions still page
        return f"notification: ledger {link['card']} is {ledger}, no wait card"
    path = git_dir / core.WAIT_KIND
    try:
        # Claim the marker atomically: of two racing Notification hooks, only one opens a card.
        claim = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        try:
            card = path.read_text().strip()
            stale = not card and time.time() - path.stat().st_mtime > STALE_CLAIM
        except FileNotFoundError:  # another hook just reclaimed it
            return open_wait(git_dir, link, detail, key, ask, proposal, bridged)
        if stale:
            # The hook that claimed it was killed (hook timeout) before recording a card.
            # ponytail: two hooks reclaiming the same stale marker within milliseconds can open two wait cards;
            # rename-to-unique and re-check if that is ever seen.
            path.unlink(missing_ok=True)
            return open_wait(git_dir, link, detail, key, ask, proposal, bridged)
        # An empty marker is another hook mid-create. A recorded card still ready is one whose
        # subscribe or block failed: finish it rather than open a second.
        if not card or status(card) != "ready":
            return f"notification: wait card {card or '(being opened)'} already open"
    else:
        name, ref = where(link)
        try:
            # The ping leads with this title; a links file from before "title" was recorded falls back.
            title = link.get("title") or f"{name} waiting for you"
            card = json.loads(core.kanban(
                "create", "--body",
                f"Waiting for you. ledger {link['card']} | {ref} | pane {link['pane']} | "
                f"worktree {link['worktree']}\n{link.get('provenance', core.PROVENANCE)}"
                + asked(ask) + attached(link["card"], proposal),
                "--idempotency-key", key, "--created-by", core.CREATED_BY, "--json",
                "--", title))["id"]
            # Recorded before subscribe and block, so a failure there is finished or archived later.
            os.write(claim, card.encode())
        except BaseException:
            path.unlink()
            raise
        finally:
            os.close(claim)
    if status(card) == "ready":
        if not muster_pages(git_dir, link, bridged or bool(ask)):
            core.subscribe(card, link["card"])
        # "--": the agent's question may start with "--" (e.g. "--kind=..."); argparse would read it as a flag.
        seen = (f"\n{heading(proposal)}: full text on this card and ledger {link['card']}." if proposal else "")
        core.kanban("block", "--kind", "needs_input", "--", card,
                    f"{detail or 'The agent is waiting for you.'}{seen}")
    expect(card, "blocked", "notification")
    if proposal:
        disarm(link["card"], card, proposal)
    return f"notification: wait card {card} blocked"


def close_wait(git_dir, event):
    path = git_dir / core.WAIT_KIND
    if not path.is_file():
        return f"{event}: no wait card open"
    card = path.read_text().strip()
    if not card:
        # A hook killed mid-create left its claim empty; the agent resumed, so clear it.
        path.unlink()
        return f"{event}: empty wait marker cleared"
    if status(card) != "archived":
        core.kanban("archive", card)
    expect(card, "archived", event)
    path.unlink()
    return f"{event}: wait card {card} archived"


def proposals(card):
    """A ledger's proposal versions, v<n>.md, and `armed`: the version the next question carries.
    In the data dir, not the git dir: a version must still resolve after cleanup removes the worktree."""
    return config.data_dir() / "runs" / card / "proposals"


def versions(card):
    """The ledger's saved proposal version numbers, oldest first."""
    return sorted(int(p.stem[1:]) for p in proposals(card).glob("v*.md") if p.stem[1:].isdigit())


def heading(proposal):
    return f"Proposal v{proposal['version']} {proposal['sha']}"


def asked(questions):
    """Every question and option exactly as the agent wrote them, for the wait card body."""
    lines = []
    for q in questions or []:
        lines.append(f"\n\n## {q.get('question', '')}" + (" (any number of options)" if q.get("multiSelect") else ""))
        for option in q.get("options") or []:
            if isinstance(option, dict):
                lines.append(f"\n- {option.get('label', '')}: {option.get('description', '')}")
                if option.get("preview"):
                    preview = str(option["preview"])
                    cut = "\n[preview cut at 2 KB; the whole of it is in the pane]" if len(preview) > PREVIEW_MAX else ""
                    lines.append("\n" + "\n".join("    " + line for line in (preview[:PREVIEW_MAX] + cut).splitlines()))
    return "".join(lines)


def attached(ledger, proposal):
    """The pinned version's full text for the wait card body; nothing when the question carries none."""
    if not proposal:
        return ""
    text = (proposals(ledger) / f"v{proposal['version']}.md").read_text()
    return f"\n\n# {heading(proposal)} (also on ledger {ledger}, the comment headed so)\n\n{text}"


def post_proposal(ledger, proposal):
    """One ledger comment per version, read back. A redelivery finds its header and posts nothing."""
    head = heading(proposal)

    def posted():
        shown = json.loads(core.kanban("show", ledger, "--json"))
        return any(str(c.get("body") or "").startswith(head + "\n") for c in shown.get("comments") or [])
    if not posted():
        text = (proposals(ledger) / f"v{proposal['version']}.md").read_text()
        core.kanban("comment", "--", ledger, f"{head}\n\n{text}")
        if not posted():
            raise core.CommandError(f"proposal: {head} did not read back on ledger {ledger}")
    return f"proposal: {head} on ledger {ledger}"


def review(ledger, proposal, title=None):
    """Wake the coordinator to review this version (muster:escalation): one card per version, as decisions.rereview.

    Keyed by version and hash, so a replay or the same text proposed again finds the card done and wakes no one.
    Delivery mode `wake`: the run's status message says the review started (progress.state), and a ping would stay
    in the thread after the decision card. The version's pin (heading) is in the body.
    """
    head, n = heading(proposal), proposal["version"]
    who = config.settings.get("coordinator_name") or "Coordinator"
    card = json.loads(core.kanban(
        "create", "--body", f"Design review; {core.REVIEW_CUE}. ledger {ledger} | {head}\n{core.PROVENANCE}"
        + attached(ledger, proposal),
        "--idempotency-key", f"design:{ledger}:v{n}:{proposal['sha']}", "--created-by",
        core.CREATED_BY, "--json", "--", f"{title or ledger}: design v{n} review"))
    if card["status"] == "ready":
        core.subscribe(card["id"], ledger, mode="wake")
        core.kanban("complete", card["id"], "--summary", f"🔍 {who} is reviewing design v{n}. Nothing to decide yet.")
        expect(card["id"], "done", "design")
    return f"design: {head} review card {card['id']}"


def enqueue(card, event, detail, payload, pin=None, **issue):
    """Save one hook event. An AskUserQuestion also saves its questions, and an allowed approval
    request its pin (gate), so a revision made later never changes what this question was asked about."""
    from . import runs  # lazy: runs imports events at module level
    questions = claude.ask(payload) if event == "notification" else None
    return runs.enqueue(card, event, detail, ask=questions, proposal=pin, bridged=is_bridged(event, payload), **issue)


def gate(card, directory, event, payload, command):
    """(pin, None) to let an approval request ask, (None, why) to deny it; (None, None) for any other event.

    An approval request needs an armed proposal (propose read it back on the ledger) and no other open
    wait card, which would otherwise swallow it. Queued events are delivered first, so a close already
    on its way lands before the check. A denial leaves the proposal armed for the next request.
    """
    from . import runs  # lazy: runs imports events at module level
    # Only the ask itself (PreToolUse, saved as `notification`): its PostToolUse carries the same payload
    # as a `prompt`, and must still close the wait the human just answered.
    if event != "notification" or not claude.approval(payload):
        return None, None
    if runs.pending(card):
        runs.drain(card, wait=True)
    if any(p.name.endswith("-notification.json") for p in runs.pending(card)):
        # Not on the board yet (hermes down?): this request would land behind it and be swallowed.
        return None, ("An earlier question is not on the board yet, so this approval request would not reach "
                      "the human. Tell the human in plain text and ask again in a minute.")
    try:
        pin = json.loads((proposals(card) / "armed").read_text())
    except (OSError, ValueError):
        return None, (f"No proposal is saved on the ledger card for this approval request. Write the whole "
                      f"design to a file, run `{command}`, and ask again once it says the proposal is "
                      f"on the ledger.")
    marker = directory / core.WAIT_KIND
    try:
        open_card = marker.read_text().strip()
        dead = not open_card and time.time() - marker.stat().st_mtime > STALE_CLAIM  # open_wait reclaims it
    except FileNotFoundError:
        open_card, dead = None, True
    if not dead:
        open_card = open_card or "(being opened)"
        return None, (f"Wait card {open_card} for an earlier question is still open, so this approval request "
                      f"would not reach the human. {heading(pin)} stays saved; tell the human in plain text "
                      f"and ask again once that wait is answered.")
    return pin, None


def unsaved(pin, caught):
    """Why an allowed approval request is denied after all: its outbox entry could not be saved, so no
    wait card would ever show it. Nothing consumed the proposal: it stays armed for the retry."""
    return (f"muster could not save this approval request ({' '.join(str(caught).split())}), so it would not "
            f"reach the human. {heading(pin)} stays saved; ask again in a minute, or tell the human in plain text.")


def deny(why):
    """A PreToolUse decision: Claude Code cancels the tool call and shows the agent the reason."""
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": why}}))
    return 0


def disarm(ledger, wait, proposal):
    """Consume the armed version once a wait card carries it, read back; a newer one armed since stays."""
    shown = json.loads(core.kanban("show", wait, "--json"))
    if heading(proposal) not in str(shown["task"].get("body") or ""):
        return  # a card this request did not open (finished after a failed subscribe): keep it armed
    path = proposals(ledger) / "armed"
    with contextlib.suppress(OSError, ValueError):
        if json.loads(path.read_text()) == proposal:
            path.unlink()


def propose(card, file, **issue):
    """`hook propose <file>`: the agent's design as the next version, posted on the ledger.

    Run by the agent: prints its outcome, 0 once the comment is on the ledger, else 1. Only a text
    unlike the latest version makes a new one (a revert is a new revision). Every call queues a post,
    so running it again heals a kill between the save and the queue; the post skips a header it finds.
    """
    from . import runs  # lazy: runs imports events at module level
    try:
        if not file:
            raise ValueError("usage: hermes muster hook propose <file>")
        raw = Path(file).read_bytes()
        if len(raw) > PROPOSAL_MAX:
            raise ValueError(f"{file} is {len(raw)} bytes, over {PROPOSAL_MAX}: shorten it")
        text = core.SECRET.sub("[redacted]", raw.decode())
        if not text.strip():
            raise ValueError(f"{file} is empty")
        sha = hashlib.sha256(text.encode()).hexdigest()[:12]
        directory = proposals(card)
        directory.mkdir(parents=True, exist_ok=True)
        with open(runs.run_dir(card) / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # released before the drain, which takes it again
            saved = versions(card)
            version, new = saved[-1] if saved else 0, False
            if not version or (directory / f"v{version}.md").read_text() != text:
                version, new = version + 1, True
                (directory / f"v{version}.md").write_text(text)
            proposal = {"version": version, "sha": sha}
            path = runs.enqueue(card, "proposal", heading(proposal), proposal=proposal, **issue)
            # Its own event: a wake that fails must not keep the version from arming (propose waits on `path` only).
            runs.enqueue(card, "design", heading(proposal), proposal=proposal, **issue)
        runs.drain(card, wait=True)
        if path.exists():
            error = json.loads(path.read_text()).get("error") or "not delivered"
            raise core.CommandError(f"{error} (queued for the flush)")
        if status(card) == "archived":
            raise core.CommandError(f"ledger {card} is archived: not posted")
        core.save_json(directory / "armed", proposal)  # only now: an approval request needs it on the ledger
        if new:  # a repeat of the same version keeps its review's state
            progress.touch(card, "proposal", f"v{version}")
    except Exception as caught:  # noqa: BLE001 - the agent reads the reason; nothing may crash it
        line = f"propose card {card}: {' '.join(str(caught).split())}"
        log(line)
        print(line, file=sys.stderr)
        return 1
    print(f"proposal: {heading(proposal)} on ledger {card}")
    return 0


def move(event, git_dir, link, detail, key, ask=None, proposal=None, bridged=False):
    """This event's moves, each read back. Returns a report line; raises when a move did not land."""
    if event == "proposal":
        return post_proposal(link["card"], proposal)
    if event == "design":
        return review(link["card"], proposal, link.get("title"))
    if event == "notification":
        return open_wait(git_dir, link, detail, key, ask, proposal, bridged)
    if event == "prompt":
        return close_wait(git_dir, event)
    card = link["card"]
    now = status(card)
    if event == "session-end" and now == "ready":
        core.switch_mode(card, "wake", "notify+wake")  # a completion that switched it, then failed: this block pings
        core.kanban("block", "--kind", "needs_input", card,
                    "The agent's session ended before it opened a pull request.")
        expect(card, "blocked", event)
        return f"{event}: card {card} ready -> blocked"
    if event == "done":
        if now in ("ready", "blocked"):
            # One line: the board keeps a completion's first summary line only. The agent reports done
            # right after opening its pull request; nothing here has merged or deployed it. No ping: the status
            # message says the review started, and a ping would stay in the thread after the decision card.
            core.switch_mode(card, "notify+wake", "wake")
            core.kanban("complete", card, "--summary",
                        progress.opened(detail))
            expect(card, "done", event)
            progress.touch(card, "review", detail)
        elif now != "done":
            raise core.CommandError(f"done: card {card} is {now}; complete it by hand")
        else:  # already done: a revision after a send-back goes back to review
            why = decisions.rereview(card)[1]
            if why:
                raise Unfinished(f"done: {why}")
        close_wait(git_dir, event)
        return f"{event}: card {card} {now} -> done"
    return f"{event}: card {card} is {now}, nothing to do"


def replay(entry):
    """One saved event's moves (runs.drain), tried twice. Safe to repeat: every move reads state first."""
    git_dir, link, event = Path(entry["git_dir"]), entry["link"], entry["event"]
    if status(link["card"]) == "archived":
        close_wait(git_dir, event)  # nothing to move, but a wait card it opened still closes
        return
    if event == "notification" and not git_dir.is_dir():
        return  # the worktree is gone: no agent is left waiting
    for attempt in range(2):
        try:
            move(event, git_dir, link, entry["detail"], entry["key"], entry.get("ask"), entry.get("proposal"),
                 entry.get("bridged", False))
            return
        except Unfinished:
            raise
        except (core.CommandError, core.LaunchError, OSError, ValueError, KeyError):
            if attempt:
                raise


def log(line):
    core.log("events", line)


AGENT_RUN = ("done", "propose", "duplicate")  # run by the pane agent from its shell, never by Claude's hooks


def duplicate(link, number, verdict):
    """`hook duplicate <n> same|different`: this run's agent evaluated a held report muster sent it (muster.hold)."""
    from . import hold  # hold imports events

    if not (number or "").isdigit() or verdict not in ("same", "different") or link.get("issue") is None:
        print("usage: hermes muster hook duplicate <issue number> same|different (from an issue run's worktree)",
              file=sys.stderr)
        return 2
    try:
        print(hold.record_verdict(link, int(number), verdict))
        return 0
    except (core.LaunchError, core.CommandError, OSError, ValueError, KeyError, TypeError) as caught:
        line = f"duplicate {number} {verdict}: {' '.join(str(caught).split())}"
        log(line)
        print(line, file=sys.stderr)
        return 1


def hook(args):
    if args.card:
        if args.event == "duplicate":
            print("hook duplicate: run it from the issue run's worktree, without --card", file=sys.stderr)
            return 1
        from . import adhoc  # lazy: adhoc imports events at module level
        return adhoc.hook(args)
    event = args.event
    payload = {}
    if event not in AGENT_RUN:  # run by the agent from its shell: stdin is no hook payload
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
    if claude.ignore(event, payload):
        return 0  # /clear ends a session, but the agent keeps working in the same pane
    core.prepare_env()
    try:
        found = context(payload.get("cwd") or os.getcwd())
    except (OSError, ValueError) as caught:
        # An unreadable links file or a missing git binary: no card to retry against, but the
        # hook still must not crash the agent, and `done` still must not report false success.
        line = f"{event}: {' '.join(str(caught).split())}"
        log(line)
        if event in AGENT_RUN:
            print(line, file=sys.stderr)
            return 1
        return 0
    if found is None:
        if event in AGENT_RUN:
            cwd = payload.get("cwd") or os.getcwd()
            line = f"{event}: not inside a muster worktree (cwd {cwd})"
            log(line)
            print(line, file=sys.stderr)
            return 1
        return 0
    git_dir, link = found
    progress.hooked(link.get("card"), event, payload)  # the status message's evidence; never raises
    if event == "stop":
        return 0  # fires at every turn end; an issue run has nothing else to do with it
    if event == "duplicate":
        return duplicate(link, args.url, getattr(args, "verdict", None))
    if event == "propose":
        return propose(link["card"], args.url, git_dir=str(git_dir), link=link)
    if event == "permission":  # before the gate and the outbox: it waits, and prints only a decision
        return bridge.wait(git_dir, link, payload)
    if event == "prompt":
        try:  # before the early returns: the pane may have ended a dialog muster still holds open
            bridge.settle(link["card"], payload)
        except Exception as caught:  # noqa: BLE001 - a hook never fails the agent
            log(f"settle card {link['card']}: {' '.join(str(caught).split())}")
    if event == "prompt" and link.get("launch_dir"):
        with contextlib.suppress(OSError, ValueError):  # the launch's evidence that its brief arrived
            launcher.prompt_seen(link["launch_dir"], payload)
    if event == "done":
        match = PR_URL.fullmatch(args.url or "")
        if not match or match.group(1).lower() != link["repo"].lower():
            print(f"usage: hermes muster hook done https://github.com/{link['repo']}/pull/<n>", file=sys.stderr)
            return 2
        # A links file from before "branch" was recorded: the branch every intake launch uses.
        branch = link.get("branch") or f"{config.settings['branch_prefix']}{link['issue']}"
        try:
            head = json.loads(core.run(["gh", "pr", "view", args.url, "--json", "headRefName"]))["headRefName"]
            line = None if head == branch else f"done: {args.url} is from {head}, not {branch}"
        except (core.CommandError, ValueError, KeyError, TypeError) as caught:
            line = f"done: cannot read the head branch of {args.url}: {' '.join(str(caught).split())}"
        if line:
            log(line)
            print(line, file=sys.stderr)
            return 1
        detail = args.url
    else:
        detail = claude.detail(payload)
    from . import runs  # lazy: runs imports events at module level
    card = link["card"]
    try:
        pin, why = gate(card, git_dir, event, payload, f"{config.hermes_bin()} muster hook propose <file>")
    except Exception as caught:  # noqa: BLE001 - fail closed, and say why
        pin, why = None, f"muster could not check this approval request: {' '.join(str(caught).split())}"
    if why:
        log(f"{event} card {card}: approval request denied: {why}")
        return deny(why)
    if event == "notification" and claude.ask(payload) is not None:
        with contextlib.suppress(OSError):
            bridge.write_pin(git_dir, pin)  # the bridge's request for this question carries it
    queued = runs.pending(card)
    if event == "prompt" and (queued[-1].name.endswith("-prompt.json") if queued
                              else not (git_dir / core.WAIT_KIND).exists()):
        return 0  # every PostToolUse lands here: nothing open, or one queued prompt is enough
    try:
        # Saved before any move, so a hook killed mid-move leaves its event for the flush.
        try:
            path = enqueue(card, event, detail, payload, pin, git_dir=str(git_dir), link=link)
        except Exception as caught:
            if not pin:
                raise
            why = unsaved(pin, caught)  # an unsaved approval dialog would be one no card tracks
            log(f"{event} card {card}: approval request denied: {why}")
            return deny(why)
        drained = runs.drain(card, wait=event == "done")  # only done waits: the agent reads its answer
        if not path.exists():
            if event != "done":
                return 0
            now = status(card)  # an archived ledger's entry is dropped, not completed
            if now != "done":
                raise core.CommandError(f"done: card {card} is {now}; complete it by hand")
            print(f"done: card {card} -> done")
            return 0
        if not drained:
            return 0  # another hook or the flush holds the queue and delivers it
        # The queue stops at its oldest failure, which may be an earlier event's.
        first = json.loads(runs.pending(card)[0].read_text())
        error = first.get("error") or "not delivered"
        raise core.CommandError(error if first.get("unfinished") else f"{error} (queued for the flush)")
    except Exception as caught:  # a hook must never crash the agent
        line = f"{event} card {card}: {' '.join(str(caught).split())}"
        log(line)
        if event == "done":
            print(line, file=sys.stderr)
            return 1
        return 0
