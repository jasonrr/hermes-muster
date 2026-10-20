"""The run lifecycle as the human sees it in Slack (design §4), against a real gateway, the real Slack adapter, the
Slack stand-in and a scripted coordinator. Hermes's own messages (the notifier's ping, the coordinator's tool
bubble) are real here, which no in-process test can show."""

import hashlib
import json
import re
import subprocess
import time

from muster import launcher
from tests.acceptance import hermes_kit as kit
from tests.acceptance.conftest import APPROVER, CHANNEL, SCAN, shown


def wait(what, pred, timeout):
    kit.wait_until(pred, what, timeout=timeout, interval=0.25)
    return pred()


def launch(rig, token):
    """`hermes muster launch` as a caller runs it; the world's pane "receives" the brief as Claude's hook would."""
    runs = rig.hermes / "plugin-data" / "muster" / "runs"

    def received(text):
        newest = max(runs.glob("*/run.json"), key=lambda p: p.stat().st_mtime)
        launcher.prompt_seen(newest.parent, {"prompt": text})

    rig.world.on_submit = received
    brief = rig.root / "brief.md"
    brief.write_text("Build the demo page.\n")
    done = rig.hermes_cli("muster", "launch", "--cwd", rig.settings["repos"][0].split("=", 1)[1], "--branch",
                          "feat/x", "--title", f"Demo [in:{token}]", "--brief", str(brief))
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)["card"], brief.read_text()


def hook(rig, event, card, payload=None, *args):
    done = rig.hermes_cli("muster", "hook", event, *args, "--card", card, payload=payload)
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def works(rig, card, brief):
    hook(rig, "prompt", card, {"hook_event_name": "UserPromptSubmit", "prompt": brief})
    hook(rig, "prompt", card, {"hook_event_name": "PostToolUse", "tool_name": "Bash"})


def parent(rig):
    """The run's thread parent: the one top-level message naming the run."""
    tops = [v for v in rig.standin.visible(CHANNEL) if not v.extra.get("thread_ts") and "Demo" in v.text]
    return tops[0] if len(tops) == 1 else None


def replies(rig):
    top = parent(rig)
    return [v for v in rig.standin.visible(CHANNEL)
            if top and v.extra.get("thread_ts") == top.message_id and v.message_id != top.message_id and not v.deleted]


def audit(rig):
    """The thread's replies as the human reads them. The stand-in shows each file of one upload as its own message
    (tests/fakes/platforms/slack_standin.py:418-430; Slack shows one), so one upload's files are one entry here."""
    got, upload = [], None
    for v in replies(rig):
        text = shown(v.text, v.extra.get("blocks") or [])
        if v.extra.get("file_id") and upload == text:
            continue
        upload = text if v.extra.get("file_id") else None
        got.append(text)
    return got


def expect_exactly(rig, *patterns):
    """The thread's replies are these, in order, and nothing else: a leftover ping or tool bubble fails (design §5)."""
    got = audit(rig)
    ok = len(got) == len(patterns) and all(re.search(p, g, re.S) for p, g in zip(patterns, got))
    assert ok, "thread replies:\n" + "\n---\n".join(got) + "\n\nexpected, in order:\n" + "\n".join(patterns)


def card(rig):
    """The open decision card: the reply carrying buttons."""
    return next((v for v in replies(rig) if any(b.get("type") == "actions" for b in v.extra.get("blocks") or [])),
                None)


def button(rig, message, label):
    return next(b for b in rig.standin.buttons(CHANNEL)
                if str(b["message_id"]) == message.message_id and b["text"]["text"] == label)


def propose(rig, card_id, token, version=1, choice="approve", feedback=None):
    """The builder proposes a design; the coordinator's woken turn is scripted to recommend on it."""
    plan = rig.root / f"plan-v{version}.md"
    plan.write_text(f"# Plan v{version}\nBuild the page in two steps.\n")
    sha12 = hashlib.sha256(plan.read_text().encode()).hexdigest()[:12]
    review, summary = rig.root / f"review-v{version}.md", rig.root / f"summary-v{version}.md"
    review.write_text(f"Design v{version} is sound: two small steps, each tested.\n")
    summary.write_text(f"Two small steps, each tested. Approve v{version}.\n")  # the coordinator always passes one
    command = (f"hermes muster recommend-design {card_id} --proposal v{version}:{sha12} --choice {choice} "
               f"--review {review} --summary {summary}")
    if feedback:
        (rig.root / "feedback.md").write_text(feedback)
        command += f" --feedback {rig.root / 'feedback.md'}"
    rig.director.script(token, kit.ToolCall("terminal", {"command": command}), kit.Text("[SILENT]"))
    hook(rig, "propose", card_id, None, str(plan))
    return asks(rig, card_id, version)


def asks(rig, card_id, version):
    """The builder asks for approval, as its brief says: an AskUserQuestion headed Approval fires PreToolUse, then
    the PermissionRequest hook, which holds the dialog until the channel answers (claude.hook_settings,
    bridge.wait). Returns that held hook; its stdout is what Claude receives."""
    tool = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [
        {"question": f"Approve design v{version}?", "header": "Approval", "multiSelect": False,
         "options": [{"label": "Approve", "description": "Build it as proposed."},
                     {"label": "Revise", "description": "Say what to change."}]}]}}
    hook(rig, "notification", card_id, {"hook_event_name": "PreToolUse", **tool})
    held = subprocess.Popen([str(rig.user / ".local/bin/hermes"), "muster", "hook", "permission", "--card", card_id],
                            env=rig.child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                            cwd=rig.root)
    held.stdin.write(json.dumps({"hook_event_name": "PermissionRequest", **tool}))
    held.stdin.close()
    rig.held.append(held)
    return held


def answered(held, timeout=60):
    """What Claude got back from the held dialog."""
    out, _ = held.communicate(timeout=timeout)
    return json.loads(out)


def recommended(rig):
    return [m for m in rig.marks() if "recommend-design" in m["argv"] or "recommend" in m["argv"]]


def message(rig, mid):
    v = next(v for v in rig.standin.visible(CHANNEL) if v.message_id == mid)
    return shown(v.text, v.extra.get("blocks") or [])


def settle(rig, decision, pattern, timeout=30):
    """C: the card ends in its settled state once the builder received the answer (design §4)."""
    try:
        wait(f"C: the card reads {pattern}", lambda: re.search(pattern, message(rig, decision.message_id), re.S), timeout)
    except AssertionError:
        raise AssertionError(f"C: the card never matched {pattern!r}; it reads:\n{message(rig, decision.message_id)}")


def prompts(rig):
    return [e for e in rig.entries() if e.get("argv", [])[:3] == ["herdr", "agent", "prompt"]]


def hold(seconds):
    time.sleep(seconds)  # B must hold across scans, not just once: a late ping or bubble lands after the card


def checkpoint_b(rig, subtests, decision, files_pattern, card_pattern):
    """B: the human's decision is the parent's one live status, and the thread holds only the audit history: the
    full review and the files (#63), then the card. No "is reviewing" ping, no tool bubble (design §4)."""
    with subtests.test("B: the parent asks for the decision"):
        assert f"🟡 Your decision: {decision}" in parent(rig).text
    with subtests.test("B: no stale ping or tool bubble in the thread"):
        expect_exactly(rig, files_pattern, card_pattern)
    with subtests.test("B: the coordinator's recommend ran shimmed"):
        assert recommended(rig) and all(m["patched"] for m in recommended(rig))


def to_design_card(rig, token, subtests=None, **kwargs):
    """Launch, work (A: the diamond), propose v1 and ask; returns (card id, the parent, the held dialog) once the
    decision card is up and three scans have passed."""
    card_id, brief = launch(rig, token)
    works(rig, card_id, brief)
    top = wait("the parent says the builder is working", lambda: (p := parent(rig)) and "Working ·" in p.text and p, 30)
    if subtests:
        with subtests.test("A: the parent spins the diamond while the builder works"):
            assert ":diamond_spin: Working ·" in top.text, top.text
    held = propose(rig, card_id, token, **kwargs)
    wait("a decision card with buttons", lambda: card(rig), 60)
    hold(3 * SCAN)
    return card_id, top, held


DESIGN_V1 = (r"^Full review and design · Design v1", r"^🟡 Design v1 ·.*Approve design v1")


def test_design_approve(rig, subtests):
    card_id, top, held = to_design_card(rig, "d1", subtests)
    checkpoint_b(rig, subtests, "design v1", *DESIGN_V1)
    decision = card(rig)
    rig.standin.block_action(APPROVER, CHANNEL, decision.message_id, button(rig, decision, "Approve design v1"))
    got = answered(held)
    answer = got["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]["Approve design v1?"]
    assert answer.startswith("Approve: the human approved Proposal v1 "), got
    settle(rig, decision, r"^✅ Approved\b.*The builder received your approval of design v1")
    assert len(prompts(rig)) == 1  # the brief only: the approval went back through the held dialog, once
    assert ":diamond_spin: Working" in parent(rig).text and parent(rig).message_id == top.message_id


def test_bubble_rule_detects(make_rig):
    """Positive control: with Hermes's cleanup off for Slack, the coordinator's tool bubble stays, and the rule sees it."""
    from tests.acceptance.conftest import DISPLAY, start

    rig = start(make_rig(display={**DISPLAY, "platforms": {"slack": {"cleanup_progress": False}}}))
    to_design_card(rig, "d1")
    assert any("terminal" in entry and "recommend-design" in entry for entry in audit(rig)), audit(rig)
    try:
        expect_exactly(rig, *DESIGN_V1)
    except AssertionError:
        return
    raise AssertionError("the bubble rule passed a thread with a tool bubble in it")
