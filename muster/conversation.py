"""conversation: where a run's messages go, one channel-neutral reference per ledger card (issue #24).

<data dir>/runs/<ledger>/conversation.json holds {ledger, platform, chat_id, user_id, thread_id, name, state, why,
attempts, since, previous, cards, noticed, repaired, resolved_at, closed_at, closed_by, unsubscribed, parent_text}. `thread_id` is Telegram's
message_thread_id, or Slack's thread parent ts; `user_id` is the human authorized to answer. `resolved_at`: when cleanup
found the run's work resolved (its pull request merged); the topic closes then, whatever is left to clean up. `state`:
  pending  - the launch asked for a topic; the gateway (which holds the bot) creates it
  creating - an attempt is under way, or its result was lost (muster.gateway reconciles it)
  open     - the run's messages go to thread_id
  closing  - the work is resolved; the gateway closes the topic, keeping its history (retried until Telegram
             confirms). Closing never touches the worktree
  closed   - closed in Telegram (`closed_by` muster or the human, at `closed_at`). Only the human's reopen, or a
             request made after `closed_at`, reopens it; nothing older (a replay, a restart, a saved approval) does
  retiring - the ledger card reads archived on the board: the gateway stales the run's prompts and deletes
             the topic (retried until Telegram confirms)
  deleted  - terminal: the topic is gone and is never probed, reopened or recreated (issue #28). This file
             stays as the run's compact record; the archived card keeps its pull request and comments
  fallback - no topic (`why`): the run stays in the main chat
Only runs launched with `notify_topics` on have one; without it a run uses the main chat, as before.
"""

import contextlib
import fcntl
import json
import re
import time

from . import config, core

WAIT = 45  # s a launch waits for the gateway's topic (three attempts 10 s apart)
POLL = 0.5
LIMIT = 128  # Telegram's topic name limit
SHORT = 48  # characters of a title in a run's name
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
    mine = ref and ref.get("platform", "telegram") == config.settings["notify_platform"]  # never another platform's
    thread = ref.get("thread_id") if mine and ref.get("state") in THREADED else None
    return {**core.notify_target(), "thread_id": thread}


def short(title, repo=""):
    """A run's title as people name it: a leading repo or product tag ("muster: …", "[app] …") dropped, cut at a word
    near SHORT. The full title stays in the linked issue."""
    text = " ".join(str(title or "").split())
    tag = re.match(r"\[([^\]]+)\]\s*|([\w.-]+):\s+", text)
    if tag and (tag.group(1) or tag.group(2)).lower() in {repo.split("/")[-1].lower(), "muster"}:
        text = text[tag.end():]
    if len(text) <= SHORT:
        return text
    return (text[:SHORT].rsplit(" ", 1)[0] or text[:SHORT]).rstrip(" ,;:-") + "…"


def tag(repo, issue=None, branch=None):
    """What tells a run apart: its issue number (with the repo's name when several repos are configured), or branch."""
    if issue is None:
        return branch or ""
    return f"{repo.split('/')[-1]}#{issue}" if len(config.settings.get("repos") or []) > 1 else f"#{issue}"


def name(repo, title, issue=None, branch=None):
    """The topic's name: the run's short title, then its issue number or branch. No ledger id: `unique` keeps two
    live runs' names apart, so Telegram's topic-created notice (which carries only the name) points at one run."""
    head, end = short(title, repo), tag(repo, issue, branch)
    return (f"{head} · {end}" if head and head != end else end or head)[:LIMIT]


def unique(ledger, wanted):
    """`wanted`, or `wanted (2)`, (3)…: no other run's live conversation has that name."""
    taken = {r.get("name") for r in every() if r["ledger"] != ledger and r.get("state") != "deleted"}
    n, out = 1, wanted
    while out in taken:
        n += 1
        out = f"{wanted[:LIMIT - 6]} ({n})"
    return out


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
                "user_id": target["user_id"], "thread_id": None, "name": unique(ledger, topic_name), "attempts": 0,
                "since": 0, "previous": [], "cards": [], "noticed": False, "why": ""}
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
    """The run's work is resolved: the gateway closes the topic."""
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
    """References the gateway still has work on: not deleted (unless its cards' subscriptions are still to be
    removed), and not a fallback already told and settled. A closed one stays: its ledger may be archived later."""
    return [ref for ref in every() if (ref.get("state") != "deleted" or ref.get("unsubscribed") is False) and not (
        ref.get("state") == "fallback" and ref.get("noticed") and (not ref.get("previous") or ref.get("repaired")))]
