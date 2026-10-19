"""progress: what a run's one status message says (issue #46). Evidence only; nothing here acts on the run.

Writers touch one file per kind in <data dir>/runs/<ledger>/activity/; its mtime is when it was observed:
  launched  launcher.ensure, once the brief is sent (JSON: the launch dir and the brief's sha256)
  prompt    UserPromptSubmit: the brief, a send-back, a typed reply. Never shown as tool activity
  tool      PostToolUse / PostToolUseFailure, the main agent's or a subagent's
  stop      Stop: the agent ended its turn
  end       SessionEnd (not /clear)
  review    a verified pull request (text: its url): events.move done, adhoc.complete, decisions.rereview
  proposal  a saved design version (text: "v2"): events.propose
  recommended  the coordinator's recommendation was recorded: decisions.recommend, recommend_design
  revision  the human's feedback or plan revisions reached the builder (delivery evidence): decisions
Only `launched` makes the directory, so a run from before this feature, or one cleanup removed, gets nothing.
Brief receipt is the prompt hook's own evidence (launcher.seen_at), and "needs your decision" is the open requests the
human must answer (muster.decisions), which only their own paths end. herdr's screen state is never read.

<data dir>/runs/<ledger>/progress.json is the message record the gateway keeps: {mid, thread, key, text, edited_at,
final, posting, lost, retry_at}. Only hooks and completions write activity, so a message edit never moves a time.
"""

import collections
import contextlib
import json
import re
import time

from . import config, conversation, core, launcher

QUIET = 15 * 60  # s without a tool before the message says no new activity was observed
REFRESH = 5 * 60  # s between edits that only age the same state's times
RETRY = 60  # s before a send or edit the platform refused is tried again


def activity(ledger):
    return config.data_dir() / "runs" / ledger / "activity"


def path(ledger):
    return config.data_dir() / "runs" / ledger / "progress.json"


def load(ledger):
    try:
        return json.loads(path(ledger).read_text())
    except (OSError, ValueError):
        return {}


def save(ledger, **fields):
    core.save_json(path(ledger), {**load(ledger), **fields})


def touch(ledger, kind, text=None):
    """Record one observation now. Never raises: evidence is best effort and a hook never fails the agent."""
    try:
        directory = activity(ledger)
        if kind == "launched":
            directory.mkdir(parents=True, exist_ok=True)
        elif not directory.is_dir():
            return
        file = directory / kind
        if text is None:
            file.touch()
        else:
            file.write_text(text)
    except OSError as caught:
        with contextlib.suppress(OSError):  # an unwritable log must not fail a launch or a completion either
            core.log("progress", f"{ledger}: {kind}: {caught}")


def launched(ledger, directory, sha, again=False):
    """`again`: a launch already delivered before (a recover); only a run tracked from its first send is kept up."""
    if not (again and not activity(ledger).is_dir()):
        touch(ledger, "launched", json.dumps({"dir": str(directory), "sha256": sha}))


HOOKED = {("prompt", "UserPromptSubmit"): "prompt", ("prompt", "PostToolUse"): "tool",
          ("prompt", "PostToolUseFailure"): "tool"}


def hooked(ledger, event, payload):
    """A pane hook's observation (claude.hook_settings names the events)."""
    try:
        name = payload.get("hook_event_name") if isinstance(payload, dict) else None
        kind = {"stop": "stop", "session-end": "end"}.get(event) or HOOKED.get((event, name))
        if kind and isinstance(ledger, str) and ledger:
            touch(ledger, kind)
    except Exception as caught:  # noqa: BLE001 - a hook never fails the agent
        with contextlib.suppress(Exception):
            core.log("progress", f"{ledger}: {event}: {caught}")


def tracked(skip=()):
    """Ledgers with a launched run whose message is not final yet; `skip`: ledgers already known final."""
    names = (file.parent.parent.name for file in sorted((config.data_dir() / "runs").glob("*/activity/launched")))
    return [n for n in names if n not in skip and not load(n).get("final")]


def record(ledger):
    """The run's launch record (an issue run's launch.json, else an ad-hoc run's run.json), or {}."""
    for file in (core.intake_dir() / ledger / "launch.json", config.data_dir() / "runs" / ledger / "run.json"):
        try:
            rec = json.loads(file.read_text())
            if isinstance(rec, dict) and rec.get("repo"):
                return rec
        except (OSError, ValueError):
            continue
    return {}


def name(ledger, ref):
    """The run as its message names it: its topic's name, else the same name built from its launch record."""
    if ref and ref.get("name"):
        return ref["name"]
    rec = record(ledger)
    try:
        return conversation.name(rec["repo"], rec.get("title") or "", issue=rec.get("issue"), branch=rec.get("branch"))
    except (KeyError, TypeError, AttributeError):
        return ledger


SLUG = re.compile(r"[\w.-]+/[\w.-]+")


def identity(ledger, fallback=None):
    """(short title, issue url or None, link label): what a thread parent shows. The url is built from the launch
    record's repo slug and issue number, never from text an agent wrote. `fallback`: the title without a record."""
    rec = record(ledger)
    repo, issue, branch = str(rec.get("repo") or ""), rec.get("issue"), rec.get("branch") or ""
    title = conversation.short(rec.get("title") or "", repo) or branch or fallback or ledger
    if SLUG.fullmatch(repo) and isinstance(issue, int) and not isinstance(issue, bool):
        return title, f"https://github.com/{repo}/issues/{issue}", f"Issue #{issue}"
    return title, None, branch


def mtimes(ledger):
    out = {}
    for file in activity(ledger).iterdir() if activity(ledger).is_dir() else ():
        out[file.name] = file.stat().st_mtime
    return out


def hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def needs(reqs):
    """ledger -> [(created_at, what)]: each open request, and each unanswered question, the human must answer now.
    A builder's Approval question is not one while its version waits for the coordinator's review (no recommendation
    since the proposal was saved), nor while an open design review for its version stands in for it: that review is
    the one decision shown."""
    from . import decisions  # decisions writes progress evidence: imported here, not at the top

    designs = {(r["ledger"], json.dumps(r.get("proposal"), sort_keys=True)) for r in reqs
               if r["kind"] == "design" and r["status"] == "open"}
    out = {}
    for r in reqs:
        if r.get("status") != "open":
            continue
        kind = r["kind"]
        if kind == "question":
            slot, partial = decisions.approval_slot(r), r.get("partial") or {}
            left = [n for n in range(len(r["questions"])) if str(n) not in partial]
            if slot in left:
                m = mtimes(r["ledger"])
                if ((r["ledger"], json.dumps(r["proposal"], sort_keys=True)) in designs
                        or m.get("proposal", 0) > m.get("recommended", 0)):
                    left.remove(slot)
            what = ["question"] * len(left)
        else:
            what = [{"permission": "permission", "feedback": "feedback to send"}.get(kind) or (
                f"design v{(r.get('proposal') or {}).get('version')}" if kind == "design" else pr_name(r.get("pr")))]
        out.setdefault(r["ledger"], []).extend((r.get("created_at", 0), w) for w in what)
    return out


def opened(url):
    """A completed run's one-line summary, the human's ping: the pull request is open and under review, nothing
    merged or deployed. The coordinator reviews a completed ledger by its kind (skill muster:escalation)."""
    who = config.settings.get("coordinator_name") or "Coordinator"
    return f"{pr_name(url)} opened · {who} is reviewing. No action needed; not merged or deployed. {url}"


def pr_name(url):
    """"PR #47" from a pull request url (the verified one the `review` evidence holds), else "the pull request"."""
    found = re.search(r"/pull/(\d+)", url or "")
    return f"PR #{found.group(1)}" if found else "the pull request"


def ago(t, now):
    return f"{int((now - t) // 60)} min ago ({hm(t)})"


def state(ledger, now, waits, ref, archived):
    """(key, text, final) for the run's message, in human terms: what is happening and whether it needs the human.
    `waits`: (created_at, what) of each open request the human must answer (gateway.needs: an Approval question
    still under the coordinator's review is not one). `ref`: its conversation reference or None. `archived`: the
    ledger reads archived (True), or not. The first match wins: a decision the human owes outranks everything but
    the run's end."""
    ref, who = ref or {}, config.settings.get("coordinator_name") or "Coordinator"
    m = mtimes(ledger)

    def read(kind):
        return (activity(ledger) / kind).read_text().strip() if kind in m else ""

    if archived is True or ref.get("state") in ("retiring", "deleted"):
        return "archived", "Archived", True
    if ref.get("resolved_at"):  # cleanup saw the merge; a human's close of the topic is not this
        return "finished", f"Finished · {pr_name(read('review'))} merged", True
    try:
        launch = json.loads((activity(ledger) / "launched").read_text())
        seen = launcher.seen_at(launch["dir"], launch["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        seen = None
    tool, prompt, stop = m.get("tool"), m.get("prompt", 0), m.get("stop", 0)
    latest = max(tool or 0, prompt)
    if waits:  # before the session's end: a merge or plan decision outlives the builder's session
        first, counts = min(t for t, _ in waits), collections.Counter(w for _, w in waits)
        what = ", ".join(w if n == 1 else f"{n} {w}s" for w, n in counts.items())
        return f"waiting:{len(waits)}:{first}", f"Needs your decision: {what} · since {hm(first)}", False
    if m.get("end", 0) > max(latest, stop):
        return "ended", f"Session ended {hm(m['end'])} · the builder is not running", False
    recommended, review = m.get("recommended", 0), m.get("review", 0)
    if m.get("proposal", 0) > recommended:
        plan = f"design {read('proposal')}" if read("proposal") else "the design"
        return f"reviewing:{m['proposal']}", f"{who} is reviewing {plan} · no action needed", False
    if m.get("revision", 0) > max(review, m.get("proposal", 0)):
        return (f"revision:{m['revision']}",
                f"Feedback delivered {hm(m['revision'])} · revision pending" + (
                    f" · last activity {ago(tool, now)}" if tool and tool > m["revision"] else ""), False)
    if review > prompt:
        pr = pr_name(read("review"))
        if review > recommended:
            return f"review:{review}", f"{pr} opened · {who} is reviewing · no action needed", False
        return f"reviewed:{recommended}", f"{pr} open · {who} reviewed it", False
    if stop > latest:
        return "turn", f"Turn ended {hm(stop)} · the builder is waiting at its prompt, no question open", False
    if seen is None:  # prompt evidence is best effort: a verified review or a turn end above outranks its absence
        return "launched", f"Dispatched {hm(m.get('launched', now))} · waiting for the builder to start", False
    last = tool if tool is not None else seen
    if now - last >= QUIET:
        return "quiet", (f"Quiet {int((now - last) // 60)} min · " + (
            f"last tool {hm(tool)}" if tool is not None else f"started {hm(seen)}, no tool activity yet") +
            " · long tools can be quiet"), False
    if tool is None:
        return "received", f"Started {hm(seen)} · no tool activity yet", False
    return "working", f"Working · last activity {ago(tool, now)}", False


def due(saved, key, text, now):
    """A new state edits at once; the same state's aging times at most every REFRESH."""
    return saved.get("key") != key or (text != saved.get("text") and now - saved.get("edited_at", 0) >= REFRESH)
