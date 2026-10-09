"""conversation: where a run's messages go, one channel-neutral reference per ledger card (issue #24).

<data dir>/runs/<ledger>/conversation.json holds {ledger, platform, chat_id, user_id, thread_id, name, state, why,
attempts, since, previous, cards, noticed, repaired}. `thread_id` is Telegram's message_thread_id (a Slack
thread_ts later); `user_id` is the human authorized to answer. `state`:
  pending  - the launch asked for a topic; the gateway (which holds the bot) creates it
  creating - an attempt is under way, or its result was lost (muster.gateway reconciles it)
  open     - the run's messages go to thread_id
  closing  - cleanup removed the worktree; the gateway closes the topic, keeping its history
  closed   - closed; the gateway reopens it before presenting anything new
  fallback - no topic (`why`): the run stays in the main chat
Only runs launched with `notify_topics` on have one; without it a run uses the main chat, as before.
"""

import contextlib
import fcntl
import json
import time

from . import config, core

WAIT = 45  # s a launch waits for the gateway's topic (three attempts 10 s apart)
POLL = 0.5
LIMIT = 128  # Telegram's topic name limit
THREADED = ("open", "closing", "closed")


def path(ledger):
    return config.data_dir() / "runs" / ledger / "conversation.json"


def load(ledger):
    try:
        return json.loads(path(ledger).read_text())
    except FileNotFoundError:
        return None


@contextlib.contextmanager
def locked(ledger):
    lock = path(ledger).with_name("conversation.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def update(ledger, **fields):
    """Merge fields, creating the reference when missing. Returns it."""
    with locked(ledger):
        ref = {**(load(ledger) or {"ledger": ledger}), **fields}
        core.save_json(path(ledger), ref)
        return ref


def swap(ledger, from_states, **fields):
    """Merge fields only if the state is in from_states: (ref, True), else (ref, False). The guard between the
    launch's fallback and a late gateway write."""
    with locked(ledger):
        ref = load(ledger)
        if not ref or ref.get("state") not in from_states:
            return ref, False
        ref.update(fields)
        core.save_json(path(ledger), ref)
        return ref, True


def target(ledger):
    """notify_target() plus the run's thread (None: the main chat). chat_type stays the configured one: Hermes's
    inbound topic sessions are "group" too (telegram adapter _build_message_event), so a wake and the human's
    replies in the topic share one session."""
    ref = load(ledger) if ledger else None
    thread = ref.get("thread_id") if ref and ref.get("state") in THREADED else None
    return {**core.notify_target(), "thread_id": thread}


def name(repo, title, ledger, issue=None, branch=None):
    """The topic's name: the run's readable title, ended by its ledger id. The id makes the name unique, so
    Telegram's topic-created notice (which carries only the name) points at exactly one run."""
    short, tag = repo.split("/")[-1], f" · {ledger}"
    head = f"{short}#{issue}" if issue is not None else f"{short} {branch}:"
    return f"{head} {title}"[:LIMIT - len(tag)] + tag


def request(ledger, topic_name):
    """The launch side: ask the gateway for the run's topic and wait for it. None when topics are off. Never
    raises: anything that goes wrong is a fallback to the main chat, so the subscription after it still runs."""
    if not config.settings["notify_topics"]:
        return None
    try:
        ref = load(ledger)
        if ref:  # a relaunch of the same card: its topic (and its cards) stay
            return ref
        from . import decisions  # decisions imports events, which imports core

        target = core.notify_target()
        base = {"platform": config.settings["notify_platform"], "chat_id": target["chat_id"],
                "user_id": target["user_id"], "thread_id": None, "name": topic_name, "attempts": 0, "since": 0,
                "previous": [], "cards": [], "noticed": False, "why": ""}
        if not decisions.gateway_up():
            return update(ledger, **{**base, "state": "fallback", "why": "the Hermes gateway is not running"})
        update(ledger, **base, state="pending")
        end = time.monotonic() + WAIT
        while time.monotonic() < end:
            ref = load(ledger)
            if (ref or {}).get("state") in ("open", "fallback"):
                return ref
            time.sleep(POLL)
        ref, _ = swap(ledger, ("pending", "creating"), state="fallback", why=f"no topic within {WAIT} s")
        return ref
    except Exception as caught:  # noqa: BLE001 - a topic must never stop a launch
        core.log("conversation", f"{ledger}: topic request failed: {caught!r}")
        with contextlib.suppress(Exception):  # a late topic must not take a run whose cards are in the main chat
            swap(ledger, ("pending", "creating"), state="fallback", noticed=False, why=f"topic request failed: {caught}")
        return None


def add_card(ledger, card):
    """Remember a card subscribed for the run, so a recreated topic can move its subscription."""
    if load(ledger) is None:  # no topic for this run (topics off): touch nothing
        return
    with locked(ledger):
        ref = load(ledger)
        if ref and card not in ref.setdefault("cards", []):
            ref["cards"].append(card)
            core.save_json(path(ledger), ref)


def finish(ledger):
    """The run is finished (cleanup removed its worktree): the gateway closes the topic."""
    if load(ledger):
        swap(ledger, ("open",), state="closing")


def every():
    """Every readable reference; a file that does not parse is logged and skipped."""
    out = []
    for file in sorted((config.data_dir() / "runs").glob("*/conversation.json")):
        try:
            ref = json.loads(file.read_text())
        except (OSError, ValueError) as error:
            core.log("conversation", f"{file}: unreadable, skipped: {error}")
            continue
        out.append({**ref, "ledger": ref.get("ledger") or file.parent.name})
    return out


def active():
    """References the gateway still has work on: not closed, and not a fallback already told and settled."""
    return [ref for ref in every() if ref.get("state") != "closed" and not (
        ref.get("state") == "fallback" and ref.get("noticed") and (not ref.get("previous") or ref.get("repaired")))]
