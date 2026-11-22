"""The standalone ledger: `hermes kanban --board <b> ...` over one JSON file per card.

`call(board, args)` takes the argv after `--board <b>` and returns the stdout the real CLI would print; a refusal
is a core.CommandError. tests/test_kanban_contract.py runs the same cases against both backends.
"""

import argparse
import contextlib
import fcntl
import json
import secrets
import time

from . import config, core


KINDS = ("capability", "dependency", "needs_input", "transient")  # what `hermes kanban block --kind` accepts


class Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would print usage and SystemExit
        raise core.CommandError(message)


def root(board):
    return config.data_dir() / "ledger" / board


@contextlib.contextmanager
def locked(board):
    path = root(board) / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load(board, card):
    try:
        return json.loads((root(board) / f"{card}.json").read_text())
    except FileNotFoundError:
        raise core.CommandError(f"no such task: {card}") from None


def save(board, rec):
    core.save_json(root(board) / f"{rec['id']}.json", rec)


def cards(board):
    """Every card record, oldest first."""
    files = sorted(root(board).glob("*.json"), key=lambda p: p.stat().st_mtime)
    return sorted((json.loads(p.read_text()) for p in files), key=lambda rec: rec["created_at"])


def last_event(board, card):
    try:
        return load(board, card)["events"]
    except core.CommandError:
        return 0


def block_kind(board, card):
    try:
        return load(board, card)["block_kind"]
    except core.CommandError:
        return None


def parser():
    top = Parser(prog="kanban", exit_on_error=False)
    verbs = top.add_subparsers(dest="verb", required=True, parser_class=Parser)

    def verb(name, positionals=(), options=(), required=(), flags=()):
        p = verbs.add_parser(name, exit_on_error=False)
        for positional in positionals:
            p.add_argument(positional)
        for option in options:
            p.add_argument("--" + option.replace("_", "-"), dest=option, choices=KINDS if option == "kind" else None)
        for option in required:
            p.add_argument("--" + option.replace("_", "-"), dest=option, required=True)
        for flag in flags:
            p.add_argument("--" + flag, action="store_true")

    verb("create", ["title"], ["body", "idempotency_key", "created_by"], flags=["json"])
    verb("show", ["card"], flags=["json"])
    verb("comment", ["card", "text"])
    verb("block", ["card", "reason"], ["kind"])
    verb("unblock", ["card"])
    verb("complete", ["card"], required=["summary"])
    verb("archive", ["card"])
    subscription = ["thread_id", "user_id", "chat_type", "notifier_profile", "delivery_mode"]
    verb("notify-subscribe", ["card"], subscription, required=["platform", "chat_id"])
    verb("notify-unsubscribe", ["card"], ["thread_id"], required=["platform", "chat_id"])
    verb("notify-list", ["card"], flags=["json"])
    return top


def move(board, card, allowed, status, **fields):
    rec = load(board, card)
    if rec["status"] not in allowed:
        raise core.CommandError(f"cannot move {card} from {rec['status']} to {status}")
    rec.update(fields, status=status)
    save(board, rec)
    return rec


def create(board, a):
    if a.idempotency_key:
        # ponytail: key lookup scans every card file; add an index file when a board passes ~10k cards
        for rec in cards(board):
            if rec["key"] == a.idempotency_key and rec["status"] != "archived":
                return rec
    rec = {"id": "t_" + secrets.token_hex(4), "title": a.title, "body": a.body or "", "status": "ready",
           "assignee": None, "created_at": int(time.time()), "created_by": a.created_by or "", "key": a.idempotency_key,
           "block_kind": None, "kinds_blocked": [], "events": 0, "comments": [], "subs": []}
    save(board, rec)
    return rec


def block(board, a):
    rec = load(board, a.card)
    if rec["status"] != "ready":
        raise core.CommandError(f"cannot block {a.card}: it is {rec['status']}")
    rec["comments"].append({"body": a.reason, "created_at": int(time.time())})
    if a.kind and a.kind in rec["kinds_blocked"]:
        rec["status"] = "triage"  # a second block of the same kind: a human triages it
    else:
        rec.update(status="blocked", block_kind=a.kind, events=rec["events"] + 1)
        if a.kind:
            rec["kinds_blocked"].append(a.kind)
    save(board, rec)
    return rec


def subscribe(board, a):
    rec = load(board, a.card)
    key = {"platform": a.platform, "chat_id": a.chat_id, "thread_id": a.thread_id}
    row = next((s for s in rec["subs"] if all(s[k] == v for k, v in key.items())), None)
    if row is None:
        row = {**key, "user_id": None, "chat_type": None, "notifier_profile": "default",
               "delivery_mode": "notify+wake", "last_event_id": 0, "last_ping_event_id": 0}
        rec["subs"].append(row)
    row.update({k: v for k, v in vars(a).items() if k in row and v is not None})
    save(board, rec)


def unsubscribe(board, a):
    rec = load(board, a.card)
    kept = [s for s in rec["subs"] if (s["platform"], s["chat_id"], s["thread_id"]) != (a.platform, a.chat_id, a.thread_id)]
    if len(kept) == len(rec["subs"]):
        raise core.CommandError(f"no such subscription on {a.card}")
    save(board, {**rec, "subs": kept})


def call(board, args):
    try:
        a = parser().parse_args(args)
    except argparse.ArgumentError as error:  # exit_on_error=False raises this for a bad choice instead of calling error()
        raise core.CommandError(str(error)) from None
    with locked(board):
        match a.verb:
            case "create":
                return json.dumps(create(board, a))
            case "show":
                rec = load(board, a.card)
                task = {k: v for k, v in rec.items() if k not in ("comments", "subs")}
                return json.dumps({"task": task, "comments": rec["comments"]})
            case "comment":
                rec = load(board, a.card)
                rec["comments"].append({"body": a.text, "created_at": int(time.time())})
                save(board, rec)
            case "block":
                block(board, a)
            case "unblock":
                move(board, a.card, ("blocked",), "ready", block_kind=None)
            case "complete":
                rec = load(board, a.card)
                move(board, a.card, ("ready", "blocked"), "done", events=rec["events"] + 1)
            case "archive":
                move(board, a.card, ("ready", "blocked", "triage", "done"), "archived")
            case "notify-subscribe":
                subscribe(board, a)
            case "notify-unsubscribe":
                unsubscribe(board, a)
            case "notify-list":
                return json.dumps(load(board, a.card)["subs"])
    return ""
