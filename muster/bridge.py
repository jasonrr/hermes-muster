"""bridge: the pane side of a channel decision.

Claude Code runs a PermissionRequest hook in parallel with the pane's own dialog, and takes whichever
answers first. `wait` records the dialog as a request (muster.decisions), polls until the gateway has
answered it, and prints that answer as the hook's decision. Printing nothing leaves the dialog to decide,
so every failure path prints nothing: muster never allows on error. A permission prompt nobody answers in
PERMISSION_DEADLINE gets an explicit deny.

A subagent's prompt (the payload names `agent_id`) differs: Claude shows its dialog only after this hook
returns (seen live, 2.1.295), so herdr never shows the pane blocked and the pane cannot answer while we wait.
It is sent only when the gateway is up; otherwise the hook returns at once and the dialog shows. `settle` (a PostToolUse hook) finishes
the request from what Claude actually did, and `session_end` stales whatever is still waiting.

Files under the run's directory (<git dir> for an issue run, runs/<card> for an ad-hoc run):
  muster-decisions/<request id>   one marker per request in flight
  muster-pin                      the proposal an AskUserQuestion approval carried (written by the
                                  PreToolUse hook; consumed here)
"""

import hashlib
import json
import os
import sys
import time

from . import claude, config, core, decisions

POLL = 1  # s between looks at the request
ALIVE_EVERY = 5  # s between `alive` writes
DEADLINE = 86340  # s a question waits: just inside the hook's 86400 s timeout
PERMISSION_DEADLINE = 600  # s a permission prompt waits for the channel; then muster denies it
BLOCKED_WAIT = 5  # s to wait for herdr to show the pane blocked
MARKERS = "muster-decisions"
ASKED = "AskUserQuestion"
SHOWN_MAX = 3000


def log(line):
    core.log("bridge", line)


def fingerprint(name, tool_input):
    """sha256 of the tool input. For an AskUserQuestion only its `questions`: Claude adds `answers` and other
    fields to the input PostToolUse reports (seen live), and the questions are what was asked."""
    if name == ASKED and isinstance(tool_input, dict):
        tool_input = tool_input.get("questions")
    return hashlib.sha256(json.dumps(tool_input, sort_keys=True).encode()).hexdigest()


def pane_of(link):
    """An ad-hoc run.json keeps its pane under launch; an issue run's links file has it at the top."""
    return (link.get("launch") or {}).get("pane") or link.get("pane")


def write_pin(directory, pin):
    """Leave the proposal an AskUserQuestion approval was allowed with for `wait` (or remove a stale one)."""
    path = directory / core.PIN_FILE
    if pin:
        core.save_json(path, pin)
    else:
        path.unlink(missing_ok=True)


def labels(options):
    """Option labels as sent to a chooser: a repeat gets " (2)", so an answer maps back by index."""
    seen, out = {}, []
    for option in options:
        seen[option["label"]] = n = seen.get(option["label"], 0) + 1
        out.append(option["label"] if n == 1 else f"{option['label']} ({n})")
    return out


def normalize(question):
    options = []
    for option in question.get("options") or []:
        if isinstance(option, dict):
            options.append({"label": str(option.get("label", "")), "description": str(option.get("description", "")),
                            **({"preview": option["preview"]} if option.get("preview") else {})})
    return {"text": str(question.get("question", "")), "header": str(question.get("header", "")),
            "options": options, "multi": bool(question.get("multiSelect"))}


def permission_question(name, tool_input, sub=None):
    shown = core.SECRET.sub("[redacted]", json.dumps(tool_input, indent=2, sort_keys=True))
    options = [{"label": "Allow once", "description": ""}, {"label": "Deny", "description": ""}]
    if len(shown) > SHOWN_MAX:
        shown, options = "(the input is too long to show here; allow in the pane)", options[1:]
    who = f" (from the {sub} subagent)" if sub else ""
    return {"text": f"Allow {name}{who}?\n{shown}", "header": "Permission", "options": options, "multi": False}


def wait(directory, link, payload):
    """Hold the PermissionRequest until the channel answers; print the decision. Always returns 0."""
    rid = None
    try:
        core.prepare_env()
        name, tool_input = payload.get("tool_name"), payload.get("tool_input")
        sub = (payload.get("agent_type") or "worker") if payload.get("agent_id") else None
        if sub and not decisions.gateway_up():
            log(f"permission {link.get('card')}: gateway down; the {sub} subagent's dialog is left to the pane")
            return 0  # its dialog shows only once this hook returns: holding it would hide the prompt
        pin = None
        if name == ASKED:
            questions = [normalize(q) for q in claude.ask(payload) or []]
            if not questions:
                return 0
            with_pin = directory / core.PIN_FILE
            try:
                pin = json.loads(with_pin.read_text())
            except (OSError, ValueError):
                pin = None
            with_pin.unlink(missing_ok=True)
            kind = "question"
        else:
            questions, kind = [permission_question(name, tool_input, sub)], "permission"
        try:
            wait_card = (directory / core.WAIT_KIND).read_text().strip() or None
        except OSError:
            wait_card = None
        run = {"repo": link["repo"], "pane": pane_of(link), "kind": "issue" if "issue" in link else "adhoc",
               "branch": link.get("branch") or config.settings["branch_prefix"] + str(link.get("issue", "")),
               **({"issue": link["issue"]} if "issue" in link else {})}
        rid = decisions.create(
            kind, link["card"], questions=questions, choices=[labels(q["options"]) for q in questions],
            tool={"name": name, "input_sha": fingerprint(name, tool_input)}, proposal=pin, wait=wait_card,
            run=run, alive=time.time(), **({"subagent": sub} if sub else {}))["id"]
        markers = directory / MARKERS
        markers.mkdir(parents=True, exist_ok=True)
        (markers / rid).write_text("")
        if not sub:
            if not blocked(run["pane"]):
                decisions.transition(rid, ("open",), "stale", outcome="answered before muster could ask")
                return 0
            decisions.update(rid, blocked_seen=True)  # from here on, the pane leaving `blocked` means it was answered there
        poll(rid, tool_input)
    except Exception as caught:  # noqa: BLE001 - a hook never fails the agent, and never allows on error
        log(f"permission {link.get('card')}: {' '.join(str(caught).split())}")
        if rid:
            try:
                decisions.transition(rid, ("open",), "stale", outcome="muster error; answer in the pane")
            except Exception:  # noqa: BLE001
                pass
    return 0


def blocked(pane):
    """True once herdr shows the pane's agent blocked (a dialog is up), looking for up to BLOCKED_WAIT s."""
    end = time.monotonic() + BLOCKED_WAIT
    while pane:
        agent = core.agent_at(pane)
        if agent and agent.get("agent_status") == "blocked":
            return True
        if time.monotonic() >= end:
            break
        time.sleep(POLL)
    return False


def poll(rid, tool_input):
    parent, start, last_alive = os.getppid(), time.monotonic(), None
    while True:
        if os.getppid() != parent:
            return  # Claude is gone
        try:
            req = decisions.load(rid)
        except FileNotFoundError:
            return
        permission = req["kind"] == "permission"
        if req["status"] == "open" and time.monotonic() - start >= (PERMISSION_DEADLINE if permission else DEADLINE):
            outcome = "No answer in 10 min: denied" if permission else "No answer in 24 h; answer in the pane"
            req, ok = decisions.transition(rid, ("open",), "stale", outcome=outcome)
            if ok:
                if permission:
                    emit({"behavior": "deny", "message": "No answer from the human within 10 minutes, so muster denied "
                          "this. Ask again, or wait for the human, if you still need it."})
                return
        if req["status"] == "answered":
            return deliver(req, tool_input)
        if req["status"] != "open":
            return
        if last_alive is None or time.monotonic() - last_alive >= ALIVE_EVERY:
            decisions.update(rid, alive=time.time())
            last_alive = time.monotonic()
        time.sleep(POLL)


def decision(req, tool_input):
    """The PermissionRequest decision for an answered request, or None when the answer is not usable."""
    answer = req.get("answer")
    if not isinstance(answer, dict):
        return None
    if req["kind"] == "question":
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in answer.items()):
            return None
        return {"behavior": "allow", "updatedInput": {**tool_input, "answers": answer}}
    if answer.get("decision") == "allow":
        return {"behavior": "allow"}
    if answer.get("decision") == "deny":
        message = answer.get("message")
        return {"behavior": "deny", **({"message": message} if message else {})}
    return None


def deliver(req, tool_input):
    chosen = decision(req, tool_input)
    if chosen is None:
        log(f"request {req['id']}: answer not usable, left to the pane")
        return
    emit(chosen)
    decisions.update(req["id"], delivered_by_hook=True)


def emit(chosen):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": chosen}}), flush=True)


# -- what Claude did ------------------------------------------------------------------------------

def squash(text):
    return " ".join(str(text).split())


def readable(response):
    """The answers an AskUserQuestion's tool_response carries, whitespace-normalized, or None."""
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict) or not answers:
        return None
    return {squash(k): squash(v) for k, v in answers.items()}


def outcome(req, event, payload):
    if req["status"] == "open":
        return "Answered in the pane"
    if req["kind"] == "question":
        seen = readable(payload.get("tool_response")) if event == "PostToolUse" else None
        if seen is None:
            return "Answered; muster could not confirm where"
        ours = {squash(k): squash(v) for k, v in (req.get("answer") or {}).items()}
        if req.get("delivered_by_hook") and seen == ours:
            return "Delivered ✓"
        return f"Answered in the pane: {'; '.join(seen.values())}"
    if event == "PostToolUseFailure":
        return "Finished; muster could not confirm the decision"
    return "Allowed in the pane" if (req.get("answer") or {}).get("decision") == "deny" else "Allowed ✓"


def settle(directory, payload):
    """Finish the request whose tool just ran (or failed). Never raises."""
    event = payload.get("hook_event_name")
    if event not in ("PostToolUse", "PostToolUseFailure"):
        return
    try:
        name, now = payload.get("tool_name"), fingerprint(payload.get("tool_name"), payload.get("tool_input"))
        for marker in sorted((directory / MARKERS).glob("*")):
            try:
                req = decisions.load(marker.name)
            except FileNotFoundError:
                marker.unlink(missing_ok=True)
                continue
            if req.get("tool") != {"name": name, "input_sha": now}:
                log(f"settle: {event} of {name} does not match request {req['id']} ({req.get('tool', {}).get('name')})")
                continue
            marker.unlink(missing_ok=True)
            for _ in range(3):  # the outcome reads the state it then moves from; a change in between asks again
                if req["status"] not in ("open", "answered"):
                    break
                req, ok = decisions.transition(req["id"], (req["status"],), "done", outcome=outcome(req, event, payload))
                if ok:
                    break
            return
    except Exception as caught:  # noqa: BLE001
        log(f"settle: {' '.join(str(caught).split())}")


def session_end(directory):
    """The agent's session ended: nothing is waiting any more. Never raises."""
    try:
        for marker in sorted((directory / MARKERS).glob("*")):
            try:
                decisions.transition(marker.name, ("open", "answered"), "stale", outcome="The agent session ended")
            except FileNotFoundError:
                pass
            marker.unlink(missing_ok=True)
    except Exception as caught:  # noqa: BLE001
        log(f"session-end: {' '.join(str(caught).split())}")
