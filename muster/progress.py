"""progress: what a run's one status message says (issue #46). Evidence only; nothing here acts on the run.

Writers touch one file per kind in <data dir>/runs/<ledger>/activity/; its mtime is when it was observed:
  launched  core.ensure, once the brief is sent (JSON: the launch dir and the brief's sha256)
  prompt    UserPromptSubmit: the brief, a send-back, a typed reply. Never shown as tool activity
  tool      PostToolUse / PostToolUseFailure, the main agent's or a subagent's
  stop      Stop: the agent ended its turn
  end       SessionEnd (not /clear)
  review    a verified pull request (text: its url): events.move done, runs.complete, decisions.rereview
Only `launched` makes the directory, so a run from before this feature, or one cleanup removed, gets nothing.
Brief receipt is the prompt hook's own evidence (core.seen_at), and "waiting" is the bridge's open question and
permission requests (muster.decisions), which only their own paths end. herdr's screen state is never read.

<data dir>/runs/<ledger>/progress.json is the message record the gateway keeps: {mid, thread, key, text, edited_at,
final, posting, lost, retry_at}. Only hooks and completions write activity, so a message edit never moves a time.
"""

import contextlib
import json
import time

from . import config, conversation, core

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


def name(ledger, ref):
    """The run as its message names it: its topic's name, else the same name built from its launch record."""
    if ref and ref.get("name"):
        return ref["name"]
    for file in (core.intake_dir() / ledger / "launch.json", config.data_dir() / "runs" / ledger / "run.json"):
        try:
            rec = json.loads(file.read_text())
            return conversation.name(rec["repo"], rec.get("title") or "", ledger, issue=rec.get("issue"),
                                     branch=rec.get("branch"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return ledger


def mtimes(ledger):
    out = {}
    for file in activity(ledger).iterdir() if activity(ledger).is_dir() else ():
        out[file.name] = file.stat().st_mtime
    return out


def hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def tool_line(seen, tool, now):
    if tool is None:
        return "no tool activity observed yet" + (f" ({int((now - seen) // 60)} min)" if now - seen >= QUIET else "")
    age = int((now - tool) // 60)
    if now - tool >= QUIET:
        return (f"no new activity observed for {age} min (last tool {hm(tool)}). Long tools can be quiet; "
                f"muster takes no action.")
    return f"last tool activity {age} min ago ({hm(tool)}, as of {hm(now)})"


def state(ledger, now, waits, ref, archived):
    """(key, text, final) for the run's message. `waits`: created_at of the ledger's open question and permission
    requests. `ref`: its conversation reference or None. `archived`: the ledger reads archived (True), or not."""
    ref = ref or {}
    if archived is True or ref.get("state") in ("retiring", "deleted"):
        return "archived", "Archived.", True
    if ref.get("resolved_at"):  # cleanup saw the merge; a human's close of the topic is not this
        return "finished", "✅ Finished: pull request merged.", True
    m = mtimes(ledger)
    try:
        launch = json.loads((activity(ledger) / "launched").read_text())
        seen = core.seen_at(launch["dir"], launch["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        seen = None
    tool, prompt, stop = m.get("tool"), m.get("prompt", 0), m.get("stop", 0)
    latest = max(tool or 0, prompt)
    if m.get("end", 0) > max(latest, stop):
        return "ended", f"⛔ Session ended at {hm(m['end'])}.", False
    line = tool_line(seen or now, tool, now)
    if waits:
        return (f"waiting:{len(waits)}:{min(waits)}",
                f"✋ Waiting for you since {hm(min(waits))} ({len(waits)} open) · {line}", False)
    if m.get("review", 0) > prompt:
        url = (activity(ledger) / "review").read_text().strip()
        return (f"review:{m['review']}",
                f"🔍 Ready for review{': ' + url if url else ''} (since {hm(m['review'])})", False)
    if stop > latest:
        return "turn", f"💬 Turn ended at {hm(stop)}: the agent waits at its prompt (no question open).", False
    if seen is None:  # prompt evidence is best effort: a verified review or a turn end above outranks its absence
        return "launched", f"🚀 Launched {hm(m.get('launched', now))} · brief receipt not confirmed yet.", False
    quiet = now - (tool if tool is not None else seen) >= QUIET
    return ("quiet" if quiet else "working" if tool is not None else "received"), f"Brief received {hm(seen)} · {line}", False


def due(saved, key, text, now):
    """A new state edits at once; the same state's aging times at most every REFRESH."""
    return saved.get("key") != key or (text != saved.get("text") and now - saved.get("edited_at", 0) >= REFRESH)
