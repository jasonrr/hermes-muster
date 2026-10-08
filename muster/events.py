"""events: agent hooks in a muster pane turn waits and endings into kanban events.

Registered only by the pane's own --settings file (core.agent_settings); a no-op in any checkout
without <git dir>/muster-card.json. The gateway notifier turns each block or completion into a
Telegram ping for the human and a queued agent turn.

  notification   open a WAIT card (subscribed notify+wake, blocked "<message>\\nReply in Herdr pane P.")
                 for a permission prompt or an AskUserQuestion (hook matchers in claude.hook_settings)
                 unless one is open or the ledger is done or archived; its id is kept in <git dir>/muster-wait
  prompt         archive the open wait card (silent): the human answered, or (via PostToolUse,
                 registered on the same event) the agent resumed on its own
  session-end    block the ledger card, if it is still ready (not on /clear)
  done <PR url>  complete the ledger card (from ready or blocked), archive any open wait card

Every block reason and completion summary is written for the human to read whole: the board's
"human_notices" setting makes the gateway's Telegram ping lead with the card's title and show the
full text. Provenance (pane, branch, worktree, issue) stays on the card: its body and its links comment.

A card per wait because hermes routes a second same-kind block of one card to triage, where
unblock, block and complete all fail (tests/test_kanban_contract.py). The ledger card is blocked
at most once and completed once. Each event is saved to <data dir>/runs/<card>/outbox/ before any
kanban call and delivered from there in order (runs.drain, then replay here); one that does not land
in two tries stays queued, and the flush (runs.flush, every minute) delivers it. A hook never fails
the agent: the error goes to <data dir>/logs/events.log and it exits 0. Only `done` prints, and only
`done` can exit non-zero, because a UserPromptSubmit hook's stdout reaches the agent's context.
"""

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import claude, config, core

STALE_CLAIM = 120  # s: an empty wait marker this old is from a hook killed at its 30 s timeout
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/\d+")


def log_path():
    return config.data_dir() / "logs" / "events.log"


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


def where(link):
    """(title prefix, body reference) of a wait card: a muster pane's issue, an ad-hoc run's branch."""
    if "issue" in link:
        return (f"{link['repo']}#{link['issue']}",
                f"issue https://github.com/{link['repo']}/issues/{link['issue']}")
    return f"{link['repo']} {link['branch']}", f"branch {link['branch']}"


def open_wait(git_dir, link, detail, key):
    ledger = status(link["card"])
    if ledger not in ("ready", "blocked"):
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
            return open_wait(git_dir, link, detail, key)
        if stale:
            # The hook that claimed it was killed (hook timeout) before recording a card.
            # ponytail: two hooks reclaiming the same stale marker within milliseconds can open two wait cards;
            # rename-to-unique and re-check if that is ever seen.
            path.unlink(missing_ok=True)
            return open_wait(git_dir, link, detail, key)
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
                f"worktree {link['worktree']}\n{link.get('provenance', core.PROVENANCE)}",
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
        core.subscribe(card)
        # "--": the agent's question may start with "--" (e.g. "--kind=..."); argparse would read it as a flag.
        core.kanban("block", "--kind", "needs_input", "--", card,
                    f"{detail or 'The agent is waiting for you.'}\nReply in Herdr pane {link['pane']}.")
    expect(card, "blocked", "notification")
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


def move(event, git_dir, link, detail, key):
    """This event's moves, each read back. Returns a report line; raises when a move did not land."""
    if event == "notification":
        return open_wait(git_dir, link, detail, key)
    if event == "prompt":
        return close_wait(git_dir, event)
    card = link["card"]
    now = status(card)
    if event == "session-end" and now == "ready":
        core.kanban("block", "--kind", "needs_input", card,
                    "The agent's session ended before it opened a pull request.\n"
                    f"Check Herdr pane {link['pane']}.")
        expect(card, "blocked", event)
        return f"{event}: card {card} ready -> blocked"
    if event == "done":
        if now in ("ready", "blocked"):
            # One line: the board keeps a completion's first summary line only. The agent reports done
            # right after opening its pull request; nothing here has merged or deployed it.
            core.kanban("complete", card, "--summary",
                        f"Ready for review: {detail} (open; not merged or deployed)")
            expect(card, "done", event)
        elif now != "done":
            raise core.CommandError(f"done: card {card} is {now}; complete it by hand")
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
            move(event, git_dir, link, entry["detail"], entry["key"])
            return
        except (core.CommandError, core.LaunchError, OSError, ValueError, KeyError):
            if attempt:
                raise


def log(line):
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as out:
        out.write(line + "\n")


def hook(args):
    if args.card:
        from . import runs  # lazy: runs imports events at module level
        return runs.hook(args)
    event = args.event
    if event == "stop":
        return 0  # fires at every turn end; an issue run has nothing to do with it
    payload = {}
    if event != "done":
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
        if event == "done":
            print(line, file=sys.stderr)
            return 1
        return 0
    if found is None:
        if event == "done":
            cwd = payload.get("cwd") or os.getcwd()
            line = f"done: not inside a muster worktree (cwd {cwd})"
            log(line)
            print(line, file=sys.stderr)
            return 1
        return 0
    git_dir, link = found
    if event == "prompt" and link.get("launch_dir"):
        with contextlib.suppress(OSError, ValueError):  # the launch's evidence that its brief arrived
            core.prompt_seen(link["launch_dir"], payload)
    if event == "done":
        match = PR_URL.fullmatch(args.url or "")
        if not match or match.group(1).lower() != link["repo"].lower():
            print(f"usage: hermes muster hook done https://github.com/{link['repo']}/pull/<n>", file=sys.stderr)
            return 2
        detail = args.url
    else:
        detail = claude.detail(payload)
    from . import runs  # lazy: runs imports events at module level
    card = link["card"]
    queued = runs.pending(card)
    if event == "prompt" and (queued[-1].name.endswith("-prompt.json") if queued
                              else not (git_dir / core.WAIT_KIND).exists()):
        return 0  # every PostToolUse lands here: nothing open, or one queued prompt is enough
    try:
        # Saved before any move, so a hook killed mid-move leaves its event for the flush.
        path = runs.enqueue(card, event, detail, git_dir=str(git_dir), link=link)
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
        error = json.loads(runs.pending(card)[0].read_text()).get("error") or "not delivered"
        raise core.CommandError(f"{error} (queued for the flush)")
    except Exception as caught:  # a hook must never crash the agent
        line = f"{event} card {card}: {' '.join(str(caught).split())}"
        log(line)
        if event == "done":
            print(line, file=sys.stderr)
            return 1
        return 0
