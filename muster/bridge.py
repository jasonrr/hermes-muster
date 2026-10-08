"""bridge: the pane side of a channel decision.

Claude Code runs a PermissionRequest hook in parallel with the pane's own dialog, and takes whichever
answers first. `wait` records the dialog as a request (muster.decisions), polls until the gateway has
answered it, prints that answer as the hook's decision and closes the request. Printing nothing leaves the
dialog to decide, so every failure path prints nothing: muster never allows on error. A permission prompt
nobody answers in PERMISSION_DEADLINE gets an explicit deny (Hermes's own approvals.timeout usually denies first).

How a request ends without a channel answer, all from Claude itself:
- the pane answers first (or the session stops): Claude sends the hook SIGTERM (seen live, 2.1.295);
- the hook dies outright: it stops writing `alive`, and the gateway stales the request after ALIVE_MAX.

A subagent's prompt (the payload names `agent_id`) differs: Claude shows its dialog only after this hook
returns (seen live), so the pane cannot answer it while we wait. It is sent only when the gateway is up;
otherwise the hook returns at once and the dialog shows.

`muster-pin` under the run's directory holds the proposal an AskUserQuestion approval carried (written by
the PreToolUse hook; consumed here).
"""

import json
import os
import signal
import sys
import time

from . import claude, config, core, decisions

POLL = 1  # s between looks at the request
ALIVE_EVERY = 5  # s between `alive` writes
DEADLINE = 86340  # s a question waits: just inside the hook's 86400 s timeout
PERMISSION_DEADLINE = 600  # s a permission prompt waits for the channel; then muster denies it
ASKED = "AskUserQuestion"


def log(line):
    core.log("bridge", line)


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


def approval_card(name, tool_input, sub=None):
    """What Hermes's approval card shows: the command (a Bash command as is, any other tool's input as JSON,
    secrets redacted), why, and whether "Allow session" is offered (Bash only: a session rule for that exact
    command; any other tool's session rule would cover every call of the tool)."""
    bash = name == "Bash" and isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str)
    command = tool_input["command"] if bash else json.dumps(tool_input, indent=2, sort_keys=True)
    why = (tool_input.get("description") if bash else None) or f"Claude wants to use {name}"
    who = f" (the {sub} subagent)" if sub else ""
    return {"command": core.SECRET.sub("[redacted]", command), "why": f"{why}{who}", "session": bash}


TERMINATED = []  # set by SIGTERM: Claude closed the dialog (answered in the pane) or is stopping


def wait(directory, link, payload):
    """Hold the PermissionRequest until the channel answers; print the decision. Always returns 0."""
    rid = None
    signal.signal(signal.SIGTERM, lambda *_: TERMINATED.append(True))  # checked by poll within POLL s
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
            fields = {"questions": questions, "choices": [labels(q["options"]) for q in questions]}
            kind = "question"
        else:
            fields, kind = {"card": approval_card(name, tool_input, sub)}, "permission"
        try:
            wait_card = (directory / core.WAIT_KIND).read_text().strip() or None
        except OSError:
            wait_card = None
        run = {"repo": link["repo"], "pane": pane_of(link), "kind": "issue" if "issue" in link else "adhoc",
               "branch": link.get("branch") or config.settings["branch_prefix"] + str(link.get("issue", "")),
               **({"issue": link["issue"]} if "issue" in link else {})}
        rid = decisions.create(kind, link["card"], tool={"name": name}, proposal=pin, wait=wait_card, run=run,
                               alive=time.time(), **fields, **({"subagent": sub} if sub else {}))["id"]
        poll(rid, tool_input)
    except Exception as caught:  # noqa: BLE001 - a hook never fails the agent, and never allows on error
        log(f"permission {link.get('card')}: {' '.join(str(caught).split())}")
        if rid:
            try:
                decisions.transition(rid, ("open",), "stale", outcome="muster error; answer in the pane")
            except Exception:  # noqa: BLE001
                pass
    return 0


def poll(rid, tool_input):
    parent, start, last_alive = os.getppid(), time.monotonic(), None
    while True:
        if os.getppid() != parent:
            return  # Claude is gone
        if TERMINATED:
            decisions.transition(rid, ("open",), "stale", outcome="Answered in the pane")
            return
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
        command = (tool_input or {}).get("command") if req["tool"]["name"] == "Bash" else None
        if answer.get("scope") == "session" and isinstance(command, str):
            return {"behavior": "allow", "updatedPermissions": [{
                "type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": command}],
                "behavior": "allow", "destination": "session"}]}
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
    # Claude applies the first answer; had the pane answered first, SIGTERM would have ended the wait.
    answer = req["answer"]
    if req["kind"] == "question":
        outcome = "Delivered ✓"
    elif answer.get("timeout"):
        outcome = "No answer in time: denied"
    elif chosen["behavior"] == "deny":
        outcome = "Denied ✓" + (f": {answer['message']}" if answer.get("message") else "")
    else:
        outcome = "Allowed for this session ✓" if answer.get("scope") == "session" else "Allowed ✓"
    decisions.transition(req["id"], ("answered",), "done", outcome=outcome)


def emit(chosen):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": chosen}}), flush=True)
