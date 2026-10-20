"""runs: every run's directory and its durable event delivery, for issue runs and ad-hoc runs alike. A pane's
hook saves each event to the run's outbox before any kanban call (enqueue), drain() delivers the queue in
order under the run's lock, and the flush cron retries what a hook could not deliver and checks the gateway's
acks. What an ad-hoc run's event means is muster.adhoc (deliver, reconcile); an issue run's entries replay
through events.replay.

One run is the directory <data dir>/runs/<card>/: run.json, lock, the wait marker (core.WAIT_KIND),
brief.md, settings.json, outbox/<ns>-<event>.json and sent/<ns>-<event>.json. An issue run's card
(events.hook) has only lock and outbox/: its entries carry their git_dir and link, and the flush
replays them through events.replay.
"""

import contextlib
import fcntl
import json
import sqlite3
import time

from . import config, core, events, launcher

UNACKED_AFTER = 15 * 60
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


def owned(card):
    """Saved events that still own the run: all but a design wake, which retries on its own and holds nothing up."""
    return [p for p in pending(card) if not p.name.endswith("-design.json")]


def sent(card):
    """Delivered events still waiting for the gateway's ack."""
    return sorted((run_dir(card) / "sent").glob("*.json"))


def wait_card(directory):
    path = directory / core.WAIT_KIND
    return (path.read_text().strip() or None) if path.is_file() else None


def last_event(card):
    """Id of the card's newest blocked or completed event, read-only from the board db; 0 if none."""
    with contextlib.closing(sqlite3.connect(f"file:{core.board_db()}?mode=ro", uri=True)) as db:
        row = db.execute("SELECT max(id) FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'completed')",
                         (card,)).fetchone()
    return row[0] or 0


def acked(card):
    """True once every notify+wake subscription pinged the human and claimed the wake past the card's newest event,
    and every `wake` subscription claimed its wake (it sends no ping).

    No event found is not acked: the move happened, so a missing event means we cannot tell. The
    notifier delivers an archived card's pending events, then drops its subscriptions, so an archived
    card with none left was delivered (or its chat was dropped as dead after 12 failed sends).
    """
    want = last_event(card)
    subs = [s for s in json.loads(core.kanban("notify-list", card, "--json"))
            if s.get("delivery_mode") in ("notify+wake", "wake")]
    if not subs:
        return events.status(card) == "archived"
    return bool(want) and all(s.get("last_event_id", 0) >= want and (
        s["delivery_mode"] == "wake" or s.get("last_ping_event_id", 0) >= want) for s in subs)


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
    from . import adhoc  # lazy: adhoc imports runs at module level

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
            if entry.get("unfinished"):  # the agent was told why once; it reruns done after fixing it
                path.unlink()
                log(f"{card} {entry['event']}: dropped after reporting: {entry.get('error')}")
                continue
            try:
                ack = adhoc.deliver(run, entry) if run else events.replay(entry)
                if ack:
                    core.save_json(directory / "sent" / path.name,
                                   {**entry, "ack": ack, "moved_at": int(time.time()), "error": None})
                path.unlink()
            except events.Unfinished as error:
                note(card, path, entry, error)
                core.save_json(path, {**entry, "error": " ".join(str(error).split()), "unfinished": True})
                return True
            except ERRORS as error:
                note(card, path, entry, error)
                if entry["event"] in ("stop", "design"):
                    continue  # a PR check, or the coordinator's wake: a later ask must not wait behind it
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


def closed(card):
    """Nothing left to do: the ledger is done or archived, nothing queued, every ack seen or given up."""
    return (not pending(card) and all(json.loads(p.read_text()).get("warned") for p in sent(card))
            and events.status(card) in ("done", "archived"))


def flush(args=None):
    from . import adhoc  # lazy: adhoc imports runs at module level

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
                adhoc.reconcile(load(card))
                drain(card)
                if closed(card):
                    with launcher.launch_lock(directory):  # relaunch saves run.json under it; busy: the next flush
                        core.save_json(directory / "run.json", {**load(card), "closed": True})
            except ERRORS as error:
                log(f"{card} flush: {error}")
    return 0
