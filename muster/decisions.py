"""decisions: the persisted, transport-neutral request store behind questions, approvals and builds.

One JSON file per request in <data dir>/decisions/<id>.json (the fields are in
docs/plans/2026-10-08-actionable-decisions.md). Every status change goes through transition,
which holds a per-request flock and refuses a move from the wrong status: that refusal is the
guard against a double click or a replayed callback. A request that reaches done, failed or
stale moves to decisions/archive/, where load and for_ledger still find it.
"""

import contextlib
import fcntl
import json
import os
import secrets
import time

from . import config, core

OPEN = ("open", "answered", "executing")
TERMINAL = ("done", "failed", "stale")


def root():
    return config.data_dir() / "decisions"


def create(kind, ledger, **fields):
    """Save a new `open` request and return it; the id is drawn again if its file exists."""
    now = time.time()
    while True:
        rid = secrets.token_hex(5)
        path = root() / f"{rid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        except FileExistsError:
            continue
        break
    req = {**fields, "id": rid, "kind": kind, "ledger": ledger, "status": "open", "created_at": now,
           "audit": [{"at": now, "step": "open", "result": "created"}]}
    core.save_json(path, req)
    return req


def load(rid):
    """The request, from decisions/ or else decisions/archive/; FileNotFoundError if neither."""
    try:
        return json.loads((root() / f"{rid}.json").read_text())
    except FileNotFoundError:
        return json.loads((root() / "archive" / f"{rid}.json").read_text())


@contextlib.contextmanager
def locked(rid):
    path = root() / f"{rid}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save(req):
    """Write the request where it belongs: archive/ once its status is terminal."""
    if req["status"] in TERMINAL:
        core.save_json(root() / "archive" / f"{req['id']}.json", req)
        (root() / f"{req['id']}.json").unlink(missing_ok=True)
        (root() / f"{req['id']}.lock").unlink(missing_ok=True)
    else:
        core.save_json(root() / f"{req['id']}.json", req)


def transition(rid, from_statuses, to, **fields):
    """Move the request to `to` if its status is in from_statuses: (request, True), else (request, False)."""
    with locked(rid):
        req = load(rid)
        if req["status"] not in from_statuses:
            return req, False
        req.update(fields, status=to)
        req["audit"].append({"at": time.time(), "step": to, "result": fields.get("outcome") or ""})
        save(req)
        return req, True


def update(rid, **fields):
    """Merge fields with no change of status (an archived request stays archived)."""
    with locked(rid):
        req = {**load(rid), **fields}
        save(req)
        return req


def note(rid, step, result):
    """Append an audit entry."""
    with locked(rid):
        req = load(rid)
        req["audit"].append({"at": time.time(), "step": step, "result": result})
        save(req)


def read_all(archive):
    """Every readable request in decisions/ (or archive/); a file that does not parse is logged and skipped."""
    out = []
    for path in sorted((root() / "archive" if archive else root()).glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError) as error:
            core.log("decisions", f"{path.name}: unreadable, skipped: {error}")
    return out


def open_requests():
    """Requests still in play, oldest first."""
    return sorted((r for r in read_all(False) if r.get("status") in OPEN), key=lambda r: r.get("created_at", 0))


def for_ledger(ledger, kind=None):
    """Every request of a ledger card, archived ones too, newest first."""
    found = [r for r in read_all(False) + read_all(True)
             if r.get("ledger") == ledger and kind in (None, r.get("kind"))]
    return sorted(found, key=lambda r: r.get("created_at", 0), reverse=True)


def stale_others(ledger, kinds, keep, why):
    """Stale the ledger's other `open` requests of these kinds; `keep` is the one that stays."""
    for req in for_ledger(ledger):
        if req["kind"] in kinds and req["id"] != keep and req["status"] == "open":
            transition(req["id"], ("open",), "stale", outcome=why)


def run_of(ledger):
    """The run a ledger card stands for: an ad-hoc run's run.json, else its newest `muster links:` comment."""
    from . import runs  # runs imports events, which imports core

    run_dir = runs.run_dir(ledger)
    if (run_dir / "run.json").is_file():
        run = runs.load(ledger)
        return {"repo": run["repo"], "branch": run["branch"], "base": run["base"], "worktree": run["launch"]["path"],
                "pane": run["launch"]["pane"], "title": run["title"], "evidence_dir": str(run_dir),
                "kind": "adhoc", "card": ledger}
    comments = json.loads(core.kanban("show", ledger, "--json"))["comments"]
    for comment in reversed(comments):
        if comment["body"].startswith(core.LINKS_PREFIX):
            link = json.loads(comment["body"][len(core.LINKS_PREFIX):])
            return {"repo": link["repo"], "issue": link["issue"],
                    "branch": link.get("branch") or config.settings["branch_prefix"] + str(link["issue"]),
                    "base": link.get("base"), "worktree": link.get("worktree"), "pane": link.get("pane"),
                    "title": link.get("title"), "evidence_dir": link.get("launch_dir"),
                    "kind": "issue", "card": ledger}
    raise core.CommandError(f"no run found for card {ledger}")
