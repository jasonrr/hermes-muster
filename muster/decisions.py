"""decisions: the persisted, transport-neutral request store behind questions, approvals and builds.

One JSON file per request in <data dir>/decisions/<id>.json (the fields are in
docs/plans/2026-10-08-actionable-decisions.md). Every status change goes through transition,
which holds a per-request flock and refuses a move from the wrong status: that refusal is the
guard against a double click or a replayed callback. A request that reaches done, failed or
stale moves to decisions/archive/, where load and for_ledger still find it.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

from . import config, core, events, launcher, progress

OPEN = ("open", "answered", "executing")
# An outcome's first line is the settled card's headline, the rest what follows (channels.shared.settled). Each claims
# only its evidence: delivered is not done, so nothing says the builder "is revising".
DELIVERED = "↩️ Sent back\nYour feedback reached the builder; revisions are pending."  # the prompt hook saw it (launcher.seen)
SENT = (DELIVERED, "✓ Feedback delivered · revision pending", "Sent ✓")  # its outcome now, before #58, before #48
ANSWER_SENT = "Answer sent to the builder"  # a question the pane's hook answered (bridge.deliver)
TERMINAL = ("done", "failed", "stale")


def root():
    return config.data_dir() / "decisions"


def gateway_up():
    """Hermes's own runtime status says its gateway runs with the notify platform connected
    (gateway_state.json, re-stamped every 60 s by the gateway's housekeeping)."""
    try:
        from gateway import status
    except ImportError:
        return False
    rec = status.read_runtime_status()
    platform = ((rec or {}).get("platforms") or {}).get(config.settings["notify_platform"]) or {}
    return (not status.runtime_status_is_stale(rec) and status.runtime_status_pid_is_live(rec)
            and platform.get("state") == "connected")


def create(kind, ledger, **fields):
    """Save a new request (`open` unless `status` says, e.g. a send-back already authorized on its card) and return it."""
    now, rid, status = time.time(), secrets.token_hex(8), fields.pop("status", "open")
    path = root() / f"{rid}.json"
    req = {**fields, "id": rid, "kind": kind, "ledger": ledger, "status": status, "created_at": now,
           "audit": [{"at": now, "step": status, "result": "created"}]}
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
        (root() / f"{req['id']}.json").unlink(missing_ok=True)  # the lock file stays: a waiter holds its inode
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


def answer_part(rid, n, part, total, compose):
    """Keep question n's answer [answer, chosen] in `partial`, durably: the first answer to a question wins. When
    all `total` are in, `answer = compose(parts)` and the request moves to answered. Returns (request, result),
    result one of "accepted", "completed", "duplicate" (n already has a part) or "closed" (not open)."""
    with locked(rid):
        req = load(rid)
        if req["status"] != "open":
            return req, "closed"
        partial = req.setdefault("partial", {})
        if str(n) in partial:
            return req, "duplicate"
        partial[str(n)] = list(part)
        req["audit"].append({"at": time.time(), "step": "part", "result": str(n)})
        done = len(partial) == total
        if done:
            req.update(answer=compose([partial[str(i)] for i in range(total)]), status="answered")
            req["audit"].append({"at": time.time(), "step": "answered", "result": ""})
        save(req)
        return req, "completed" if done else "accepted"


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


def approval_slot(req):
    """The index of the question a question request's proposal pin binds (headed `Approval`, claude.APPROVAL), or
    None for a request that carries no proposal."""
    from . import claude

    if req.get("kind") != "question" or not req.get("proposal"):
        return None
    return next((n for n, q in enumerate(req["questions"]) if q.get("header") == claude.APPROVAL), None)


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


def rereview(ledger, run=None):
    """(review card, None) once a verified revision follows a delivered send-back, else (None, why or None).

    The ledger stays `done`. The cheap local check comes first: no delivered send-back, no git or gh call.
    The review card's idempotency key holds the head, so a repeated call, retry or replay finds it.
    """
    from . import adhoc  # adhoc imports events, which imports decisions

    sent = next((r for r in for_ledger(ledger, "feedback") if r["status"] == "done" and r.get("outcome") in SENT),
                None)
    if not sent:
        return None, None
    run = run or run_of(ledger)
    if not Path(run.get("worktree") or "/nonexistent").is_dir():
        core.log("decisions", f"rereview {ledger}: worktree gone")
        return None, "worktree gone"
    pr, why = adhoc.verify(run)
    if why:
        return None, why
    head = pr["headRefOid"]
    if head == sent.get("head"):
        return None, f"no new commits since the reviewed head {head[:7]}"
    cycle = sent.get("cycle", 0) + 1
    card = json.loads(core.kanban(
        "create", "--body",
        f"Revised after a send-back. ledger {ledger} | {pr['url']} | head {head} | review cycle {cycle}\n"
        + core.PROVENANCE, "--idempotency-key", f"review:{ledger}:{head}", "--created-by", core.CREATED_BY, "--json",
        "--", f"{short(run)}: {progress.pr_name(pr['url'])} revised"))
    if card["status"] == "ready":
        core.subscribe(card["id"], ledger, mode="wake")  # no ping: the status message says the re-review started
        core.kanban("complete", card["id"], "--summary", f"🔍 {progress.pr_name(pr['url'])} updated · {who()} is "
                    f"re-reviewing it. Nothing to decide yet; nothing is merged or deployed. {pr['url']}")
        events.expect(card["id"], "done", "rereview")
        progress.touch(ledger, "review", pr["url"])  # only when the card is made: a replay keeps "since"
    for old in for_ledger(ledger, "build"):  # a recommendation for this head is the current one: keep it
        if old["status"] == "open" and old.get("head") != head:
            transition(old["id"], ("open",), "stale", outcome="Superseded: the pull request has new commits; "
                                                               f"{who()} reviews them next.")
    return card["id"], None


# -- recommend: the coordinator's review of a finished build becomes a `build` request ------------------

LABELS = {"merge": "Merge {pr}", "send-back": "Send back…", "nothing": "Leave PR open"}  # "…": the human types it
SEND_BACK = "Send back with this feedback"  # the send-back label once the feedback is on the card
LEADS = {"merge": "{who} recommends merging it.", "send-back": "{who} recommends sending it back.",
         "nothing": "{who} recommends leaving it open for now."}
SUMMARY_MAX = 900  # characters: the card shows this summary whole; the full review is attached as a Markdown file
PR_FIELDS = "url,state,headRefOid,headRefName,baseRefName,isDraft,isCrossRepository"  # what refusal checks
MERGEABLE = ("CLEAN", "HAS_HOOKS", "UNSTABLE")  # GitHub's mergeStateStatus; it stays the authority on checks


def view(url, fields, env=None):
    return json.loads(core.run(["gh", "pr", "view", url, "--json", fields], env=env))


def refusal(run, url, pr, head):
    """Why `pr` (read from GitHub) is not the run's own open pull request at `head`, else None."""
    match = events.PR_URL.fullmatch(url or "")
    if not match or match.group(1).lower() != run["repo"].lower():
        return f"{url} is not a pull request of {run['repo']}"
    if pr.get("headRefName") != run["branch"]:
        return f"{url} is from branch {pr.get('headRefName')}, not {run['branch']}"
    if pr.get("baseRefName") != run["base"]:
        return f"{url} targets {pr.get('baseRefName')}, not {run['base']}"
    if pr.get("isCrossRepository"):
        return f"{url} is from a fork"
    if pr.get("isDraft"):
        return f"{url} is still a draft"
    if pr.get("state") != "OPEN":
        return f"{url} is {pr.get('state')}, not OPEN"
    if pr.get("headRefOid") != head:
        return f"stale: PR moved to {str(pr.get('headRefOid'))[:7]}"
    return None


def obsolete(req):
    """Why an open build or feedback request no longer stands, else None: a build is bound to its pull request open at
    the reviewed head, a feedback to its pull request still open. An unreadable pull request is no proof either way:
    None, and a merge reads it again before acting."""
    try:
        pr = view(req["pr"], PR_FIELDS)
    except (OSError, ValueError, core.CommandError) as caught:
        core.log("decisions", f"{req['id']}: pull request unreadable, shown anyway: {caught}")
        return None
    if req["kind"] == "build":
        return refusal(req["run"], req["pr"], pr, req["head"])
    return None if pr.get("state") == "OPEN" else f"{req['pr']} is {pr.get('state')}, not OPEN"


def read_text(path, what):
    raw = Path(path).read_bytes()
    if len(raw) > events.PROPOSAL_MAX:
        raise ValueError(f"{path} ({what}) is {len(raw)} bytes, over {events.PROPOSAL_MAX}: shorten it")
    text = core.SECRET.sub("[redacted]", raw.decode()).strip()
    if not text:
        raise ValueError(f"{path} ({what}) is empty")
    return text


def who():
    return config.settings.get("coordinator_name") or "Coordinator"


def short(run):
    from . import conversation

    return conversation.short(run.get("title") or "", run.get("repo") or "") or run.get("branch") or run.get("card")


def summary_of(args, kind, review):
    """The coordinator's card summary (`--summary`), or None: the card then shows the whole review, so a review longer
    than a summary needs one. Refused, never cut, when it is too long. A build card, summary or review, must say what a
    merge triggers and what is left after it, which no card may hide."""
    text = read_text(args.summary, "summary") if getattr(args, "summary", None) else None
    if text is None and len(review) > SUMMARY_MAX:
        raise ValueError(f"the review is {len(review)} characters, over {SUMMARY_MAX}: give the card a --summary "
                         f"(the full review is attached as a file)")
    if text is not None and len(text) > SUMMARY_MAX:
        raise ValueError(f"the summary is {len(text)} characters, over {SUMMARY_MAX}: the card shows it whole, so "
                         f"shorten it (the full review is attached as a file)")
    shown = (text or review).lower()
    if kind == "build" and not ("merge triggers" in shown and "after merge" in shown):
        raise ValueError("the summary must say what a merge triggers and what is left after it "
                         "(\"Merge triggers: …\" and \"After merge: …\")")
    return text


def quote(text):
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def card(head, lead, feedback, reason, sending, what="feedback"):
    """A decision card's text: the subject, the recommendation, what a send-back sends (quoted, whole), the reason.
    What each button does is the request's `hints`, shown by the channel unless `button_hints` is off."""
    if not feedback:
        return f"**{head}**\n**{lead}**\n\n{reason}"
    if sending:  # "Ella recommends sending it back with this feedback:"
        said = f"**{lead[:-1]} with {'this' if what == 'feedback' else 'these'} {what}:**"
    else:
        said = f"**{lead}**\n\n**The {what} a send-back would send:**"
    return f"**{head}**\n{said}\n{quote(feedback)}\n\n**Why**\n{reason}"


def recommend(args):
    """`hermes muster recommend`: print the id of the open `build` request, or the refusal on stderr and 1."""
    core.prepare_env()
    config.require()
    try:
        if args.choice == "send-back" and not args.feedback:
            raise ValueError("--choice send-back needs --feedback FILE")
        review = read_text(args.review, "review")
        feedback = read_text(args.feedback, "feedback") if args.feedback else None
        if feedback and len(feedback) > FEEDBACK_MAX:
            raise ValueError(f"the feedback is {len(feedback)} characters, over {FEEDBACK_MAX}: the human must see "
                             f"all of it on the message, so shorten it")
        summary = summary_of(args, "build", review)
        activation = read_text(args.activation, "activation")
        if len(activation) > FEEDBACK_MAX:
            raise ValueError(f"the activation is {len(activation)} characters, over {FEEDBACK_MAX}: shorten it; the "
                             f"pull request's own Activation section can hold the rest")
        for req in for_ledger(args.ledger, "build"):
            if req["status"] == "open" and req.get("head") == args.head:
                print("an open build request already exists for this head; the new text was ignored", file=sys.stderr)
                print(req["id"])
                return 0
        run = run_of(args.ledger)
        why = refusal(run, args.pr, view(args.pr, PR_FIELDS),
                      args.head)
        if why:
            raise ValueError(why)
    except (ValueError, OSError, core.CommandError) as caught:
        print(f"recommend: {caught}", file=sys.stderr)
        return 1
    order = [args.choice] + [a for a in LABELS if a != args.choice]
    sent = sum(1 for r in for_ledger(args.ledger, "feedback") if r["status"] == "done" and r.get("outcome") in SENT)
    pr = progress.pr_name(args.pr)
    labels = {**LABELS, "merge": LABELS["merge"].format(pr=pr), **({"send-back": SEND_BACK} if feedback else {})}
    hints = {"merge": f"puts it into {run['base']} now, as it stands. Muster does not deploy.",
             "send-back": f"sends the text above to the builder, asking it to revise {pr}. Nothing is merged."
             if feedback else "type what to change below; your words go to the builder. Nothing is merged.",
             "nothing": "nothing changes."}
    # the feedback a tap sends comes first, whole (FEEDBACK_MAX); then the reason, the one part a long card may cut
    text = card(f"🟡 {pr} · {short(run)}", LEADS[args.choice].format(who=who()), feedback, summary or review,
                sending=args.choice == "send-back")
    req = create("build", args.ledger, run=run, head=args.head, base=run["base"], pr=args.pr, review=review,
                 summary=summary, feedback=feedback, activation=activation, cycle=1 + sent, proposal=None, wait=None, actions=order,
                 choices=[[labels[a] for a in order]], recommended=args.choice,
                 hints=[f"• {labels[a]}: {hints[a]}" for a in order],
                 questions=[{"text": text, "header": "Build", "multi": False,
                             "options": [{"label": labels[a], "description": ""} for a in order]}])
    stale_others(args.ledger, ("build",), req["id"], "superseded by a newer recommendation")
    progress.touch(args.ledger, "recommended")
    print(req["id"])
    return 0


# -- recommend-design: the coordinator's review of one proposal version becomes a `design` request ----------

DESIGN_LABELS = {"approve": "Approve design v{n}", "send-back": "Send back with revisions…", "nothing": "Leave pending"}
DESIGN_SEND_BACK = "Send back with these revisions"  # once the revisions are on the card
DESIGN_LEADS = {"approve": "{who} recommends approving it.", "revise": "{who} recommends sending it back.",
                "judgment": "{who}: this one is your call."}
DESIGN_CHOICE = {"approve": "approve", "revise": "send-back", "judgment": None}
PIN = re.compile(r"v(\d+):([0-9a-f]{12})")
FEEDBACK_MAX = 1500  # characters: Send back's text leads the message, so the human sees all of it before a tap


def recommend_design(args):
    """`hermes muster recommend-design`: print the id of the open `design` request, or the refusal on stderr and 1.

    Bound to the reviewed version: refused unless it is the ledger's newest and its saved text has that hash."""
    core.prepare_env()
    config.require()
    try:
        match = PIN.fullmatch(args.proposal or "")
        if not match:
            raise ValueError("--proposal must be v<n>:<sha> from the card's heading")
        if args.choice == "revise" and not args.feedback:
            raise ValueError("--choice revise needs --feedback FILE")
        pin = {"version": int(match[1]), "sha": match[2]}
        saved = events.versions(args.ledger)
        if pin["version"] not in saved:
            raise ValueError(f"ledger {args.ledger} has no Proposal v{pin['version']}")
        if saved[-1] != pin["version"]:
            raise ValueError(f"Proposal v{saved[-1]} is newer: review that one")
        text = (events.proposals(args.ledger) / f"v{pin['version']}.md").read_text()
        if hashlib.sha256(text.encode()).hexdigest()[:12] != pin["sha"]:
            raise ValueError(f"Proposal v{pin['version']} does not have hash {pin['sha']}")
        review = read_text(args.review, "review")
        feedback = read_text(args.feedback, "feedback") if args.feedback else None
        summary = summary_of(args, "design", review)
        if feedback and len(feedback) > FEEDBACK_MAX:
            raise ValueError(f"the revisions are {len(feedback)} characters, over {FEEDBACK_MAX}: the human must "
                             f"see all of them on the message, so shorten them")
        for req in for_ledger(args.ledger, "design"):
            if req["status"] == "open" and req.get("proposal") == pin:
                print("an open design request already exists for this version; the new text was ignored",
                      file=sys.stderr)
                print(req["id"])
                return 0
        given = next((approval_part(r) or r.get("outcome") or "answered" for r in for_ledger(args.ledger, "question")
                      if r.get("proposal") == pin and (answered(r) or (r["status"] == "open" and approval_part(r)))),
                     None)
        if given:  # the human answered first: their answer stands, and nothing asks them again
            progress.touch(args.ledger, "recommended")
            raise ValueError(f"the builder's approval question for Proposal v{pin['version']} was already answered "
                             f"({given}): nothing is left to decide. Post your review as a short note, no buttons")
        run = run_of(args.ledger)
    except (ValueError, OSError, core.CommandError) as caught:
        print(f"recommend-design: {caught}", file=sys.stderr)
        return 1
    chosen, n = DESIGN_CHOICE[args.choice], pin["version"]
    order = [chosen] + [a for a in DESIGN_LABELS if a != chosen] if chosen else list(DESIGN_LABELS)
    labels = {**{a: label.format(n=n) for a, label in DESIGN_LABELS.items()},
              **({"send-back": DESIGN_SEND_BACK} if feedback else {})}
    hints = {"approve": "sends your approval to the builder, which then implements it. You review the pull request "
                        "before anything merges.",
             "send-back": "sends the revisions above to the builder." if feedback
             else "type them below; they go to the builder.",
             "nothing": "the builder keeps waiting."}
    # the revisions a tap sends come first, whole (FEEDBACK_MAX); a long card cuts only the reason, whole above it
    text = card(f"🟡 Design v{n} · {short(run)}", DESIGN_LEADS[args.choice].format(who=who()), feedback,
                summary or review, sending=args.choice == "revise", what="revisions")
    req = create("design", args.ledger, run=run, proposal=pin, review=review, summary=summary, feedback=feedback,
                 wait=None, actions=order, choices=[[labels[a] for a in order]], recommended=chosen,
                 hints=[f"• {labels[a]}: {hints[a]}" for a in order],
                 questions=[{"text": text, "header": "Design", "multi": False,
                             "options": [{"label": labels[a], "description": ""} for a in order]}])
    stale_others(args.ledger, ("design",), req["id"], "superseded by a newer design review")
    progress.touch(args.ledger, "recommended")
    print(req["id"])
    return 0


# -- execute: the human's tap on a build or feedback request becomes an effect --------------------------

READBACK_WAIT = 2  # s between the read-backs after a merge call
READBACK_TRIES = 3
SEEN_WAIT = 10  # s to wait for the UserPromptSubmit hook to report a sent prompt
SEEN_POLL = 0.5
HUMAN_GH_DROP = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR")
EXTRA = "Additional instructions from the human:"
SEND, DONT = "Send feedback to builder", "Don't send"


def human_gh_env():
    """The environment for a merge: the bot's gh credentials removed, so the human's own gh login acts."""
    return {k: v for k, v in os.environ.items() if k not in HUMAN_GH_DROP}


def redact(text):
    return core.SECRET.sub("[redacted]", str(text))


def fail(rid, why):
    transition(rid, ("answered", "executing"), "failed", outcome=redact(" ".join(str(why).split()))[:500])


def execute(rid, boot=""):
    """Carry out an `answered` build or feedback request. Never raises: anything that goes wrong is `failed`."""
    try:
        _execute(rid, boot)
    except Exception as caught:  # noqa: BLE001 - a worker error must end the request, not vanish
        core.log("decisions", f"execute {rid}: {caught!r}")
        fail(rid, caught)


def recover(req):
    """Settle a request left `executing` by an earlier boot by reading the world back; never repeats an effect."""
    try:
        _recover(req)
    except Exception as caught:  # noqa: BLE001
        core.log("decisions", f"recover {req['id']}: {caught!r}")
        fail(req["id"], f"interrupted; {caught}")


def amend(base, text):
    """The feedback after the human's typed words: appended, or replacing it all after `replace:`."""
    text = redact(text.strip())
    if text.lower().startswith("replace:"):
        if not text[8:].strip():
            raise ValueError("replace: needs the new feedback after it")
        return text[8:].strip()
    return f"{base}\n\n{EXTRA}\n{text}" if base else text


def make_feedback(req, text, **extra):
    """The confirmation of a send-back: the whole text it authorizes, shown once, word for word."""
    shown = (f"**Send this to the builder? · {progress.pr_name(req['pr'])} · {short(req['run'])}**\n\n{text}\n\n"
             "Typed words are added to it; start with replace: to send only yours.")
    return create("feedback", req["ledger"], run=req["run"], head=req["head"], base=req["base"], pr=req["pr"],
                  review=req.get("review"), cycle=req["cycle"], feedback=text, wait=req.get("wait"), proposal=None,
                  actions=["send", "cancel"], choices=[[SEND, DONT]],
                  questions=[{"text": shown, "header": "Feedback", "multi": False,
                              "options": [{"label": SEND, "description": ""}, {"label": DONT, "description": ""}]}],
                  **extra)


def _execute(rid, boot):
    req = load(rid)
    if req["status"] != "answered":
        return  # a second delivery of the same tap: nothing new happens
    answer = req.get("answer") or {}
    action, text = answer.get("action"), answer.get("text")
    if req["kind"] == "design":
        _design(req, boot)
    elif req["kind"] == "build":
        if text is None and action == "nothing":
            transition(rid, ("answered",), "done", outcome="⏸ Left open\nNothing was merged; the pull request stays "
                                                           "open on GitHub.")
        elif text is not None or action == "send-back":
            req, ok = transition(rid, ("answered",), "executing", executing_boot=boot,
                                 intent={"action": "send-back", "head": req["head"]})
            if ok:
                _feedback_from_build(req, boot)
        elif action == "merge":
            _merge(req, boot)
        else:
            fail(rid, f"unknown action {action!r}")
    elif text is None and action == "cancel":
        transition(rid, ("answered",), "done", outcome="Not sent\nYou chose not to send it; Muster sent nothing.")
    else:
        _send(req, boot)


def _design(req, boot):
    """The human's tap on a design review: the reply their approval question gets, or nothing."""
    rid, head, answer = req["id"], events.heading(req["proposal"]), req.get("answer") or {}
    if "text" not in answer and answer.get("action") == "nothing":
        transition(rid, ("answered",), "done", outcome=f"⏸ Left pending\nThe builder's question about design "
                                                       f"v{req['proposal']['version']} stays open.")
        return
    if "text" in answer or answer.get("action") == "send-back":
        revisions = amend(req.get("feedback"), answer["text"]) if "text" in answer else req.get("feedback")
        if not revisions:
            fail(rid, "no revisions were proposed; reply to the message with yours")
            return
        reply, action = config.prompt("design-reply-revise", proposal=head, revisions=revisions), "revise"
    elif answer.get("action") == "approve":
        reply, action = config.prompt("design-reply-approve", proposal=head), "approve"
    else:
        fail(rid, f"unknown action {answer.get('action')!r}")
        return
    req, ok = transition(rid, ("answered",), "executing", executing_boot=boot, intent={"reply": reply, "action": action},
                         **({"resolved": revisions} if action == "revise" else {}))
    if ok:
        _answer_approval(req)


def words(req):
    """(what the human's tap gave, what an undelivered one means): an approval and a revision request never share
    words."""
    n, intent = req["proposal"]["version"], req["intent"]
    if (intent.get("action") or ("approve" if intent["reply"].startswith("Approve") else "revise")) == "approve":
        return f"Approval of design v{n}", "Nothing was approved."
    return f"Revisions for design v{n}", "The revisions were not sent."


def approval_part(ask):
    """The answer already on an approval question's Approval part, or None."""
    slot = approval_slot(ask)
    part = (ask.get("partial") or {}).get(str(slot))
    if part:
        return part[0]
    answer = ask.get("answer") or {}
    return answer.get(ask["questions"][slot]["text"]) if slot is not None and isinstance(answer, dict) else None


def answered(ask):
    """True when an approval question was answered and not abandoned: answered, or done by delivery or the pane.
    A stale one (the builder stopped waiting) delivered nothing, whatever part it held."""
    return ask["status"] in ("answered", "done")


def _answer_approval(req):
    """Give the reviewed version's approval question this request's exact reply (intent.reply) as its Approval
    part, through the question's own first-answer-wins answer_part: the pane's bridge then delivers it as if the
    human had answered the builder's message, with any other question of it answered on its own message. Before
    the builder asks, the request waits (`executing`) and settle_design gives it once the question appears. It
    stays `executing` until the bridge settles the question. Safe to repeat, and from a stale copy: it reads the
    request again first.

    Before any open question is chosen, every ended question for the version is reconciled: one the human
    answered (here or in the pane) ends this request, their answer standing; one the builder asked after the tap
    that ended without this reply ends it undelivered. Either way no later question of the version gets it."""
    req = load(req["id"])
    rid, pin, intent = req["id"], req["proposal"], req["intent"]
    if req["status"] != "executing":
        return
    if intent.get("question"):
        # A recovery: only the question this request chose. The human may have answered it since; never pick another.
        try:
            ask = load(intent["question"])
        except (OSError, ValueError) as caught:
            fail(rid, f"the approval question it chose is unreadable ({caught}); nothing sent")
            return
        _give(req, ask)
        return
    tapped = next((a["at"] for a in req["audit"] if a["step"] == "executing"), req["created_at"])
    asks = [r for r in for_ledger(req["ledger"], "question") if r.get("proposal") == pin]
    for ended in (r for r in asks if r["status"] != "open"):
        given = approval_part(ended)
        if answered(ended) and given != intent["reply"]:  # the human answered first: that answer stands
            fail(rid, f"You already answered the builder's question about design v{pin['version']}: "
                      f"{given or ended.get('outcome') or 'answered'}. Nothing else was sent.")
            return
        if ended.get("created_at", 0) >= tapped and given != intent["reply"]:  # asked after the tap, ended without it
            fail(rid, f"Not delivered: the builder's question ended without it. {words(req)[1]}")
            return
    ask = next((r for r in asks if r["status"] == "open"), None)
    if ask is None:
        return  # not asked yet, or asked and abandoned before the tap: settle_design gives it when the builder asks
    req = update(rid, intent={**intent, "question": ask["id"]})  # recorded first: a recovery acts on this one only
    _give(req, ask)


def _give(req, ask):
    rid, reply, slot = req["id"], req["intent"]["reply"], approval_slot(ask)
    if slot is None:
        fail(rid, "the builder's question carries no Approval question; nothing sent")
        return
    given = approval_part(ask)
    if given is None and ask["status"] == "open":
        compose = lambda parts, q=ask: {x["text"]: parts[i][0] for i, x in enumerate(q["questions"])}  # noqa: E731
        ask, result = answer_part(ask["id"], slot, [reply, None], len(ask["questions"]), compose)
        if result in ("accepted", "completed"):
            return
        given = approval_part(ask)
    if given != reply:  # another answer won, or the question ended without one: never retargeted
        fail(rid, f"You already answered this on the builder's question: {given}. Nothing else was sent." if given
             else f"Not delivered: the builder's question ended without it. {words(req)[1]}")


def settle_design(req):
    """One gateway-scan step of an `executing` design request. Ends it when the human answered its version's
    approval question first, or the builder asked and stopped without it; gives a waiting reply once the builder
    asks; cancels it when a newer version replaced its own
    first; ends it once the bridge settles the question: delivered only on the bridge's ANSWER_SENT. Returns
    the line its card shows meanwhile, or None once it ended."""
    rid, ledger, pin = req["id"], req["ledger"], req["proposal"]
    if req["status"] != "executing":
        return None
    what, nothing = words(req)
    if not req["intent"].get("question"):
        newest = (events.versions(ledger) or [0])[-1]
        if newest > pin["version"]:
            fail(rid, f"Design v{newest} replaced v{pin['version']} before the builder asked. {nothing}")
            return None
        _answer_approval(req)  # ended questions first, even with none open: never a later one after a human answer
        req = load(rid)
        if req["status"] != "executing":
            return None
        if not req["intent"].get("question"):
            return f"{what} recorded · the builder hasn't asked yet; it gets this when it does."
    ask = load(req["intent"]["question"])
    if ask["status"] == "open":
        if approval_part(ask) != req["intent"]["reply"]:
            _give(req, ask)
            return None if load(rid)["status"] != "executing" else f"{what} recorded · delivering to the builder"
        return f"{what} recorded · awaiting your other answer above"
    if ask["status"] == "answered":
        return f"{what} recorded · delivering to the builder"
    if ask["status"] == "done" and ask.get("outcome") in (ANSWER_SENT, "Sent to Claude") and approval_part(ask) == \
            req["intent"]["reply"]:
        revise = not req["intent"]["reply"].startswith("Approve")
        n = pin["version"]
        transition(rid, ("executing",), "done", outcome=(
            f"↩️ Revisions sent\nThe builder received your revisions to design v{n}; a new version is pending."
            if revise else f"✅ Approved\nThe builder received your approval of design v{n}. Next: implementation, "
                           "then a pull request for you to review here. Nothing is merged or deployed."))
        if revise:
            progress.touch(ledger, "revision")
        return None
    fail(rid, f"Not delivered: the builder's question ended without it. {nothing}")
    return None


def _feedback_from_build(req, boot=""):
    """The send-back of a build request: the feedback its card showed (with any typed words), sent at once through a
    feedback request (`_send`: delivery evidence, a retry offered on failure). The card ends with that send's outcome.
    Idempotent per build request: a recovery finds the feedback request, whose own recovery settles it."""
    answer, base = req["answer"], req.get("feedback")
    if "text" in answer:
        text = amend(base, answer["text"])
    elif base:
        text = base
    else:
        fail(req["id"], "no feedback was proposed; reply to the message with your instructions")
        return
    made = [r for r in for_ledger(req["ledger"], "feedback") if r.get("source") == req["id"]]
    if not made and "hints" not in req:  # a card from before #58 never showed its feedback: confirm it first
        make_feedback(req, text, source=req["id"])
        transition(req["id"], ("executing",), "done", outcome="Feedback ready below: confirm it to send it")
        return
    if not made:
        # As a merge, and fail closed: only the pull request reviewed, read now and still open at the reviewed head.
        # (obsolete() shows a card when GitHub is unreadable; that is no proof here.)
        try:
            why = refusal(req["run"], req["pr"], view(req["pr"], PR_FIELDS), req["head"])
        except (OSError, ValueError, core.CommandError) as caught:
            fresh(req)  # unchanged as far as anyone knows: the human may tap again once GitHub answers
            fail(req["id"], f"GitHub could not confirm the pull request ({caught}). Nothing was sent; a fresh card "
                            "follows below to try again")
            return
        if why:
            fail(req["id"], f"{why}. Nothing was sent")
            return
        fb = make_feedback(req, text, source=req["id"], status="answered", answer={"action": "send"})
        execute(fb["id"], boot)  # never raises: anything that goes wrong ends the feedback request failed
        made = [load(fb["id"])]
    fb = made[0]
    if fb["status"] == "done":
        outcome = fb.get("outcome")
    elif fb["status"] in TERMINAL:
        outcome = f"⚠️ Needs attention\n{fb.get('outcome')}"
    else:  # a recovery: the send was interrupted, and its own recovery confirms it or offers a retry
        outcome = ("⏳ Sending\nMuster was interrupted while sending your feedback. The run's status line shows when "
                   "it arrives; if it can't be confirmed, a retry appears below.")
    transition(req["id"], ("executing",), "done", outcome=outcome, resolved=text)


def _merge(req, boot):
    rid, head = req["id"], req["head"]
    req, ok = transition(rid, ("answered",), "executing", executing_boot=boot, intent={"action": "merge", "head": head})
    if not ok:
        return
    env = human_gh_env()
    run = run_of(req["ledger"])
    pr = view(req["pr"], "state,headRefOid,headRefName,baseRefName,isDraft,isCrossRepository,mergeStateStatus,"
                         "autoMergeRequest", env)
    why = refusal(run, req["pr"], pr, head)
    if not why and pr.get("mergeStateStatus") not in MERGEABLE:
        why = f"the merge state is {pr.get('mergeStateStatus')}, not clean"
    if why:
        fail(rid, why)
        return
    try:
        core.run(["gh", "pr", "merge", req["pr"], "--squash", "--match-head-commit", head], env=env)
        error = None
    except core.CommandError as caught:  # it may still have merged (a timeout): GitHub's read-back decides
        error = caught
    state = {}
    for attempt in range(READBACK_TRIES):
        if attempt:
            time.sleep(READBACK_WAIT)
        try:
            state = view(req["pr"], "state,mergeCommit,autoMergeRequest", env)
        except (core.CommandError, ValueError):
            continue
        if state.get("state") == "MERGED":
            transition(rid, ("executing",), "done", outcome=merged(req))
            return
    if error:
        fail(rid, error)
    elif state.get("state") == "OPEN" and state.get("autoMergeRequest"):
        transition(rid, ("executing",), "done", outcome="⏳ Merge queued\nGitHub merges it once its checks pass; not "
                                                        "merged yet.")
    else:
        fail(rid, f"Merge not confirmed: GitHub reads {state.get('state') or 'unknown'}. Check the PR before tapping "
                  "again.")


def fresh(req):
    """The same build card again, open, for a tap that could not act: the human decides again from it."""
    return create("build", req["ledger"], **{k: req[k] for k in (  # only what it has: no `hints` on a pre-#58 card
        "run", "head", "base", "pr", "review", "summary", "feedback", "activation", "cycle", "proposal", "wait",
        "actions", "choices", "questions", "recommended", "hints") if k in req})


def merged(req):
    return f"✅ Merged\nMerged into {req['base']}, as reviewed. Muster does not deploy."


def wire_text(req, run, text):
    tail = "\n\n" + (config.prompt("build-revision-issue", hermes=config.hermes_bin(), pr=req["pr"])
                      if run.get("kind") == "issue" else config.prompt("build-revision-ad-hoc"))
    return config.prompt("build-revision", request=req.get("origin") or req["id"], cycle=req["cycle"],
                         feedback=json.dumps(text + tail, ensure_ascii=True))


def _retry(req, why):
    """Fail a send and offer it again as a new feedback request (the `seen` check stops a double send)."""
    if not [r for r in for_ledger(req["ledger"], "feedback") if r.get("retry_of") == req["id"]]:
        make_feedback(req, req.get("resolved") or req["feedback"], origin=req.get("origin") or req["id"],
                      retry_of=req["id"], prompt_sha=(req.get("intent") or {}).get("prompt_sha"))
    fail(req["id"], f"{why}; a retry is available below.")


def reconcile(req):
    """Retire an `open` retry once the hook reports its original send arrived late: True if it did."""
    sha, evidence = req.get("prompt_sha"), (req.get("run") or {}).get("evidence_dir")
    if req.get("kind") != "feedback" or req.get("status") != "open" or not sha or not evidence:
        return False
    return launcher.seen(evidence, sha) and delivered(transition(req["id"], ("open",), "done", outcome=DELIVERED))


def delivered(moved):
    """A transition's (request, ok): on ok, the run's status message learns the revision is pending. Returns ok."""
    req, ok = moved
    if ok:
        progress.touch(req["ledger"], "revision")
    return ok


def _send(req, boot):
    rid = req["id"]
    run = run_of(req["ledger"])
    answer = req.get("answer") or {}
    final = amend(req["feedback"], answer["text"]) if "text" in answer else req["feedback"]
    wire = wire_text(req, run, final)
    sha = hashlib.sha256(wire.encode()).hexdigest()
    req, ok = transition(rid, ("answered",), "executing", executing_boot=boot, resolved=final,
                         intent={"action": "send-back", "head": req["head"], "prompt_sha": sha})
    if not ok:
        return
    evidence = run.get("evidence_dir")
    if not evidence:
        fail(rid, "this run predates delivery evidence; send it in the pane")
        return
    if not launcher.seen(evidence, sha):
        agent = launcher.agent_at(run["pane"]) if run.get("pane") else None
        if agent is None:
            _retry(req, "Not sent: the builder's session is gone, so Muster did not send it")
            return
        if agent.get("agent_status") in ("working", "blocked"):
            _retry(req, "Not sent: the builder was busy, so Muster did not send it")
            return
        try:
            launcher.herdr_result("agent", "prompt", run["pane"], wire, "--wait", "--until", "working", "--until",
                              "blocked", "--timeout", "30000")
        except core.CommandError as caught:  # not proof the prompt did not arrive: the hook's evidence decides
            core.log("decisions", f"{rid}: herdr prompt: {caught}")
    end = time.monotonic() + SEEN_WAIT
    while not launcher.seen(evidence, sha):
        if time.monotonic() >= end:
            _retry(req, "Delivery could not be confirmed")
            return
        time.sleep(min(SEEN_POLL, max(end - time.monotonic(), 0)))
    delivered(transition(rid, ("executing",), "done", outcome=DELIVERED))


def _recover(req):
    rid, answer = req["id"], req.get("answer") or {}
    if req["kind"] == "design":
        _answer_approval(req)
    elif req["kind"] == "build" and ("text" in answer or answer.get("action") == "send-back"):
        _feedback_from_build(req)
    elif req["kind"] == "build":
        try:
            pr = view(req["pr"], "state,headRefOid,mergeCommit", human_gh_env())
        except (OSError, core.CommandError, ValueError):
            fail(rid, "interrupted; not merged (could not read the pull request)")
            return
        if pr.get("state") == "MERGED" and pr.get("headRefOid") == req["head"]:
            transition(rid, ("executing",), "done", outcome=merged(req))
            return
        if pr.get("state") == "OPEN" and pr.get("headRefOid") == req["head"]:  # unchanged: the human may click again
            fresh(req)
        fail(rid, "interrupted; not merged")
    else:
        sha = (req.get("intent") or {}).get("prompt_sha")
        evidence = run_of(req["ledger"]).get("evidence_dir")
        if not sha or not evidence:
            fail(rid, "interrupted; cannot confirm delivery")
        elif launcher.seen(evidence, sha):
            delivered(transition(rid, ("executing",), "done", outcome=DELIVERED))
        else:
            _retry(req, "Interrupted: delivery could not be confirmed")
