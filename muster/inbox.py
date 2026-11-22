"""`muster decisions` and `muster ledger`: the human's side of muster's requests, from a command line.

Standalone has no chat channel, so the person (or the coordinator for them) lists the open requests and answers them
here, through the same request files and the same gateway.shape/compose the channels use."""

import json
import os
import re
import sys
import time

from . import config, core, decisions, events, gateway, ledger

ID = re.compile(r"[0-9a-f]{16}")
STALE = "Ended: the builder stopped waiting. Nothing was sent."


def fail(message, code=2):
    print(message, file=sys.stderr)
    return code


def covered_slot(req):
    """The builder's Approval question a design request of the same version stands in for (the channels hide it: the
    design request decides it), else None."""
    slot = decisions.approval_slot(req)
    if slot is None or str(slot) in (req.get("partial") or {}):
        return None
    return slot if any(r["status"] == "open" and r.get("proposal") == req["proposal"]
                       for r in decisions.for_ledger(req["ledger"], "design")) else None


def shown(req):
    slot = covered_slot(req)
    return not (slot is not None and all(n == slot or str(n) in (req.get("partial") or {})
                                         for n in range(len(req["questions"]))))


def requests():
    return [{"id": r["id"], "kind": r["kind"], "ledger": r["ledger"], "run": r.get("run"),
             "questions": r.get("questions") or [], "choices": r.get("choices") or [], "actions": r.get("actions"),
             "recommended": r.get("recommended"), "card": r.get("card"), "tool": r.get("tool")}
            for r in decisions.open_requests() if r["status"] == "open" and shown(r)]


def reviewed_run(card):
    """Whether this done card is a run still awaiting its first review: its record exists and is not closed."""
    for path in (config.data_dir() / "runs" / card / "run.json", config.data_dir() / "intake" / card / "launch.json"):
        if path.is_file():
            try:
                return not json.loads(path.read_text()).get("closed")
            except (OSError, ValueError):
                return False  # a corrupt record is skipped, not a reason to hide every other card
    return False


def with_run(entry):
    try:
        return {**entry, "run": decisions.run_of(entry["ledger"])}
    except (OSError, ValueError, KeyError, core.CommandError):
        return entry


def reviews(cards):
    """Work the coordinator must review: design versions and finished builds that no request covers yet."""
    every = decisions.read_all(False) + decisions.read_all(True)
    builds = [r for r in every if r.get("kind") == "build"]
    found = []
    for card in cards:
        key, status = card.get("key") or "", card["status"]
        if key.startswith("design:"):
            _, led, v, sha = (key.split(":", 3) + [""] * 4)[:4]
            if not v[1:].isdigit():
                continue  # a malformed key is skipped, not a reason to hide every other card
            pin = {"version": int(v[1:]), "sha": sha}
            if (pin["version"] == (events.versions(led) or [0])[-1]
                    and not any(r.get("kind") == "design" and r.get("ledger") == led and r.get("proposal") == pin
                                for r in every)
                    and not any(r.get("proposal") == pin and r["status"] != "open"
                                for r in decisions.for_ledger(led, "question"))):
                found.append({"kind": "design", "ledger": led, "proposal": f"{v}:{sha}", "card": card["id"]})
        elif key.startswith("review:"):
            _, led, head = key.split(":", 2)
            if not any(r.get("ledger") == led and r.get("head") == head for r in builds):
                found.append(with_run({"kind": "build", "ledger": led, "head": head, "card": card["id"]}))
        elif status == "done" and reviewed_run(card["id"]) and not any(r.get("ledger") == card["id"] for r in builds):
            found.append(with_run({"kind": "build", "ledger": card["id"], "card": card["id"]}))
    return found


def blocked(cards):
    return [{"card": c["id"], "title": c["title"], "block_kind": c["block_kind"],
             "reason": c["comments"][-1]["body"] if c["comments"] else ""} for c in cards if c["status"] == "blocked"]


def list_(args):
    out = {"requests": requests()}
    if config.standalone:
        cards = [c for c in ledger.cards(config.settings["board"]) if c["status"] != "archived"]
        out.update(reviews=reviews(cards), blocked=blocked(cards))
    if args.json:
        print(json.dumps(out))
        return
    for r in out["requests"]:
        asked = " | ".join(q["text"] for q in r["questions"]) or (r["tool"] or {}).get("name", "")
        print(f"{r['id']} {r['kind']} {r['ledger']} {json.dumps(asked)}")
    for r in out.get("reviews", []):
        print(f"review {r['kind']} {r['ledger']} {r.get('proposal') or r.get('head') or '-'} card {r['card']}")
    for b in out.get("blocked", []):
        print(f"blocked {b['card']} {json.dumps(b['title'])} {b['block_kind']} {json.dumps(b['reason'])}")
    if any(r["kind"] == "design" for r in out.get("reviews", [])):
        print("Next: muster recommend-design <ledger> --proposal v<n>:<sha> --choice approve|revise|judgment --review FILE")
    if any(r["kind"] == "build" for r in out.get("reviews", [])):
        print("Next: muster recommend <ledger> --pr URL --head SHA --choice merge|send-back|nothing --review FILE "
              "--activation FILE")


def show(args):
    try:
        print(json.dumps(decisions.load(args.id), indent=2))
    except FileNotFoundError:
        return fail(f"no request {args.id}")
    except ValueError as error:
        return fail(f"request {args.id} is unreadable: {error}")


def stale(req):
    """Why an open request must not be shown, else None; the request is ended first."""
    if req["kind"] in ("question", "permission") and time.time() - req.get("alive", req.get("created_at", 0)) > gateway.ALIVE_MAX:
        decisions.transition(req["id"], ("open", "answered"), "stale", outcome=STALE)
        return STALE
    if req["kind"] in ("build", "feedback") and (why := decisions.obsolete(req)):
        decisions.transition(req["id"], ("open",), "stale", outcome=f"Not shown: {why}")
        return f"Not shown: {why}"


def values(req, args):
    """The answer for each question, or an error string."""
    count = len(req["questions"])
    if args.text is not None:
        return [args.text] if count == 1 else "--text answers one question; this request has several"
    if len(args.choice or []) != count:
        return f"give --choice once per question ({count})"
    for n, value in enumerate(args.choice):
        if gateway.shape(req, n, value)[1] in (None, []):  # free text, or an empty multi-select
            return f"{value!r} is not a non-empty selection from {req['choices'][n]}"
    return args.choice


def answer(args):
    if not config.standalone:
        return fail("answer in your chat channel")
    if os.environ.get("MUSTER_PANE"):
        return fail("a builder pane cannot answer its own request")
    try:
        req = decisions.load(args.id)
    except FileNotFoundError:
        return fail(f"no request {args.id}")
    rid, kind = args.id, req["kind"]
    if args.message and not args.deny:
        return fail("--message goes with --deny")
    if kind == "permission":
        if not (args.allow or args.deny):
            return fail("a permission request takes --allow or --deny")
        got = {"decision": "allow"} if args.allow else {"decision": "deny", **({"message": args.message} if args.message else {})}
    elif args.allow or args.deny:
        return fail("--allow and --deny answer a permission request only")
    else:
        got = values(req, args)
        if isinstance(got, str):
            return fail(got)
    if kind == "question" and covered_slot(req) is not None:
        return fail("the design request decides this design: answer that one")
    if req["status"] == "open" and (why := stale(req)):
        print(why)
        return 1
    if kind == "permission":
        result = "completed" if decisions.transition(rid, ("open",), "answered", answer=got)[1] else "closed"
    else:
        total, results = len(req["questions"]), []
        for n, value in enumerate(got):
            _, result = decisions.answer_part(rid, n, gateway.shape(req, n, value), total,
                                              lambda parts: gateway.compose(req, parts))
            if result == "closed":
                break
            results.append(result)  # a "duplicate" slot was answered before: the rest still count
        result = "completed" if "completed" in results else "accepted" if "accepted" in results else "closed"
    if result == "closed":
        print("already answered or ended")
        return 1
    if result == "completed" and kind in ("build", "feedback", "design"):
        decisions.execute(rid, f"cli:{os.getpid()}")
    final = decisions.load(rid)
    print(f"{final['status']}: {final.get('outcome') or ''}".rstrip(": "))
    return 1 if final["status"] == "failed" else 0


def main(args):
    if getattr(args, "id", None) is not None and not ID.fullmatch(args.id):
        return fail("a request id is 16 lowercase hex characters")
    return {"list": list_, "show": show, "answer": answer}[args.decisions_command](args)


def ledger_cmd(args):
    """`muster ledger <hermes kanban args>`: the coordinator's `show`, `comment` and `complete`."""
    if not config.standalone:
        return fail("use hermes kanban")
    if os.environ.get("MUSTER_PANE"):
        return fail("a builder pane cannot use the ledger")
    try:
        print(core.kanban(*args.args))
    except core.CommandError as error:
        return fail(error, 1)
