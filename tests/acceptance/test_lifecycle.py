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
from tests.acceptance.conftest import APPROVER, CHANNEL, OTHER, SCAN, shown


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
    (tests/fakes/platforms/slack_standin.py:418-430; Slack shows one), so one upload's files are one entry here,
    its file names listed: a duplicate or extra file still shows."""
    got, upload = [], None
    for v in replies(rig):
        text, title = shown(v.text, v.extra.get("blocks") or []), v.extra.get("title")
        if v.extra.get("file_id") and upload == text:
            got[-1] += f", {title}"
            continue
        upload = text if v.extra.get("file_id") else None
        got.append(f"{text} [files: {title}" if upload else text)
    return [g + "]" if "[files: " in g else g for g in got]


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


def propose(rig, card_id, token, version=1, choice="approve", feedback=None, ask=True, final=None):
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
    rig.director.script(token, kit.ToolCall("terminal", {"command": command}), final or kit.Text("[SILENT]"))
    hook(rig, "propose", card_id, None, str(plan))
    return asks(rig, card_id, version) if ask else None


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
    rig.held.append(held)
    held.stdin.write(json.dumps({"hook_event_name": "PermissionRequest", **tool}))
    held.stdin.close()
    return held


def answered(held, timeout=60):
    """What Claude got back from the held dialog."""
    out, _ = held.communicate(timeout=timeout)
    return json.loads(out)


def recommended(rig):
    return [m for m in rig.marks() if "recommend-design" in m["argv"] or "recommend" in m["argv"]]


def message(rig, mid):
    v = next((v for v in rig.standin.visible(CHANNEL) if v.message_id == mid and not v.deleted), None)
    return shown(v.text, v.extra.get("blocks") or []) if v else ""


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


def checkpoint_b(rig, subtests, decision, *patterns, recommends=1):
    """B: the human's decision is the parent's one live status, and the thread holds only the audit history: the
    full review and the files (#63), then the card. No "is reviewing" ping, no tool bubble (design §4)."""
    with subtests.test("B: the parent asks for the decision"):
        assert f"🟡 Your decision: {decision}" in parent(rig).text
    with subtests.test("B: no stale ping or tool bubble in the thread"):
        expect_exactly(rig, *patterns)
    with subtests.test("B: the coordinator's recommend ran shimmed"):
        assert len(recommended(rig)) == recommends and all(m["patched"] for m in recommended(rig))


def text_input(message):
    """The card's typed-answer box, as a block_action for it: Enter in the box sends its value (slack.py:194-197)."""
    block = next(b for b in message.extra["blocks"] if b["type"] == "input")
    return {"action_id": block["element"]["action_id"], "block_id": block["block_id"], "type": "plain_text_input"}


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


def files(version):
    """The upload above a design card: the design and its review, once each (#63)."""
    return rf"^Full review and design · Design v{version}\b.*\[files: [\w-]+-design-v{version}-review\.md, [\w-]+-design-v{version}\.md\]$"


DESIGN_V1 = (files(1), r"^🟡 Design v1 ·.*Approve design v1")


def test_design_approve(rig, subtests):
    card_id, top, held = to_design_card(rig, "d1", subtests)
    checkpoint_b(rig, subtests, "design v1", *DESIGN_V1)
    with subtests.test("the coordinator's tool bubble was shown, then removed"):  # cleanup ran, not just no bubble
        shown_ = [(c.response or {}).get("ts") for c in rig.standin.calls_of("chat.postMessage")
                  if "recommend-design" in json.dumps(c.params) and "terminal" in json.dumps(c.params)]
        deleted = [c.params.get("ts") for c in rig.standin.calls_of("chat.delete")]
        assert shown_ and all(ts in deleted for ts in shown_), (shown_, deleted)
    decision = card(rig)
    rig.standin.block_action(APPROVER, CHANNEL, decision.message_id, button(rig, decision, "Approve design v1"))
    got = answered(held)
    answer = got["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]["Approve design v1?"]
    assert answer.startswith("Approve: the human approved Proposal v1 "), got
    settle(rig, decision, r"^✅ Approved\b.*The builder received your approval of design v1")
    assert len(prompts(rig)) == 1  # the brief only: the approval went back through the held dialog, once
    wait("the parent is back to working", lambda: ":diamond_spin: Working" in parent(rig).text, 3 * SCAN)
    assert parent(rig).message_id == top.message_id


def test_bubble_rule_detects(make_rig):
    """Positive control: with Hermes's cleanup off for Slack, the coordinator's tool bubble stays, and the rule sees it."""
    from tests.acceptance.conftest import DISPLAY, start

    rig = start(make_rig(display={**DISPLAY, "platforms": {"slack": {"cleanup_progress": False}}}))
    to_design_card(rig, "d1")
    assert any("terminal" in entry and "recommend-design" in entry for entry in audit(rig)), audit(rig)
    try:
        expect_exactly(rig, *DESIGN_V1)
    except AssertionError as caught:
        assert "recommend-design" in str(caught), caught  # the rule saw the bubble, not just something else
        return
    raise AssertionError("the bubble rule passed a thread with a tool bubble in it")


def test_design_send_back(rig, subtests):
    _, top, held = to_design_card(rig, "s1", subtests, choice="revise", feedback="Split step 2 in two.\n")
    checkpoint_b(rig, subtests, "design v1", files(1), r"^🟡 Design v1 ·.*Split step 2 in two.*Send back")
    decision = card(rig)
    rig.standin.block_action(APPROVER, CHANNEL, decision.message_id, {**text_input(decision), "value": "Tighten section 2"})
    answer = answered(held)["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]["Approve design v1?"]
    assert "Tighten section 2" in answer, answer
    settle(rig, decision, r"^↩️ Revisions sent")
    wait("the parent says the feedback reached the builder", lambda: "↩️ Feedback delivered" in parent(rig).text, 30)
    assert len(prompts(rig)) == 1 and parent(rig).message_id == top.message_id


def test_design_late_recommendation(rig, subtests):
    """The coordinator's turn ends about 5 s after its card is up: nothing it leaves behind may land after B."""
    slow = kit.Text("[SILENT]" + " " * 160, chunk_chars=8, delay_per_chunk=0.25)
    to_design_card(rig, "l1", subtests, final=slow)
    wait("the coordinator's turn has ended", lambda: not rig.director.scripts.get("l1"), 30)
    hold(5 + 3 * SCAN)
    checkpoint_b(rig, subtests, "design v1", *DESIGN_V1)


def test_design_v2_while_v1_is_open(rig, subtests):
    card_id, _, _ = to_design_card(rig, "v1", subtests, ask=False)
    first = card(rig)
    held = propose(rig, card_id, "v1", version=2)
    wait("v2's card", lambda: (c := card(rig)) and c.message_id != first.message_id, 60)
    hold(3 * SCAN)
    with subtests.test("only v2's card has buttons"):
        assert {str(b["message_id"]) for b in rig.standin.buttons(CHANNEL)} == {card(rig).message_id}
        assert "Design v1" in message(rig, first.message_id) and card(rig).message_id != first.message_id
    checkpoint_b(rig, subtests, "design v2", files(1), r"⏹ Ended · Design v1\b.*superseded by a newer design review",
                 files(2), r"^🟡 Design v2 ·.*Approve design v2", recommends=2)
    held.kill()


PR = "https://github.com/demo/demo/pull/7"


def to_pr_card(rig, token, subtests, choice, feedback=None):
    """Launch, work, open PR #7 and end the turn (adhoc.verify completes the card); the coordinator's woken turn is
    scripted to recommend on that head. Returns the card id once the decision card is up and three scans passed."""
    card_id, brief = launch(rig, token)
    works(rig, card_id, brief)
    rig.world.pr = PR
    head = rig.world.pr_record()["headRefOid"]
    review, summary, activation = rig.root / "review.md", rig.root / "summary.md", rig.root / "activation.md"
    review.write_text("The change is small and tested.\n")
    summary.write_text("Small and tested.\nMerge triggers: nothing.\nAfter merge: nothing to activate.\n")
    activation.write_text("none: tests only.\n")
    command = f"hermes muster recommend {card_id} --pr {PR} --head {head} --choice {choice} --review {review} " \
              f"--summary {summary} --activation {activation}"
    if feedback:
        (rig.root / "feedback.md").write_text(feedback)
        command += f" --feedback {rig.root / 'feedback.md'}"
    rig.director.script(token, kit.ToolCall("terminal", {"command": command}), kit.Text("[SILENT]"))
    hook(rig, "stop", card_id, {"hook_event_name": "Stop"})
    for agent in rig.world.agents.values():  # what herdr then reports for the pane: Claude's turn is over
        agent["agent_status"] = "idle"
    wait("the PR's decision card", lambda: card(rig), 60)
    hold(3 * SCAN)
    return card_id, head


def build_files():
    return r"^Full review · PR #7\b.*\[files: [\w-]+-pr-7-review-\w{7}\.md\]$"


def merges(rig):
    return [e for e in rig.entries() if e.get("argv", [])[:3] == ["gh", "pr", "merge"] or e.get("stub") == "gh"]


def test_pr_leave_open(rig, subtests):
    to_pr_card(rig, "p1", subtests, "nothing")
    checkpoint_b(rig, subtests, "PR #7", build_files(), r"^🟡 PR #7 ·.*Leave PR open")
    decision = card(rig)
    rig.standin.block_action(APPROVER, CHANNEL, decision.message_id, button(rig, decision, "Leave PR open"))
    settle(rig, decision, r"^⏸")
    assert merges(rig) == []


def test_pr_send_back(rig, subtests):
    to_pr_card(rig, "p2", subtests, "send-back", feedback="Handle the empty title.\n")
    checkpoint_b(rig, subtests, "PR #7", build_files(), r"^🟡 PR #7 ·.*Handle the empty title.*Send back")
    decision = card(rig)
    rig.standin.block_action(APPROVER, CHANNEL, decision.message_id, button(rig, decision, "Send back with this feedback"))
    wait("the feedback reached the builder", lambda: len(prompts(rig)) == 2, 30)
    assert "Handle the empty title" in prompts(rig)[1]["argv"][4] and merges(rig) == []


def test_pr_merge(rig, subtests):
    _, head = to_pr_card(rig, "p3", subtests, "merge")
    checkpoint_b(rig, subtests, "PR #7", build_files(), r"^🟡 PR #7 ·.*Merge PR #7")
    decision = card(rig)
    rig.standin.block_action(APPROVER, CHANNEL, decision.message_id, button(rig, decision, "Merge PR #7"))
    wait("the merge", lambda: merges(rig), 30)
    hold(SCAN)  # a second merge, or a hit on the gh stub, would land here
    assert [e["argv"] for e in merges(rig)] == [["gh", "pr", "merge", PR, "--squash", "--match-head-commit", head]]
    assert "error" not in merges(rig)[0]  # answered by the world; the gh stub saw nothing


def ephemeral(rig, user, pattern):
    return [e for e in rig.standin.ephemerals(CHANNEL) if e.get("user") == user and re.search(pattern, str(e.get("text")))]


def test_duplicate_tap(rig, subtests):
    _, _, held = to_design_card(rig, "t1", subtests)
    decision = card(rig)
    approve = button(rig, decision, "Approve design v1")
    rig.standin.block_action(APPROVER, CHANNEL, decision.message_id, approve)
    rig.standin.block_action(APPROVER, CHANNEL, decision.message_id, approve)
    assert answered(held)["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    settle(rig, decision, r"^✅ Approved\b")
    # slack.handle: "Already answered" while the request is open, ended(req) once it settled; both change nothing
    nothing = r"Already answered|already ended\. Nothing was changed"
    wait("the second tap is told nothing changed", lambda: ephemeral(rig, APPROVER, nothing), 30)
    hold(SCAN)
    assert len(ephemeral(rig, APPROVER, nothing)) == 1 and len(rig.standin.ephemerals(CHANNEL)) == 1
    assert re.search(r"^✅ Approved\b", message(rig, decision.message_id))  # the second tap did not re-settle it


def test_non_approver_tap(rig, subtests):
    _, _, held = to_design_card(rig, "n1", subtests)
    decision = card(rig)
    before, edits = len(rig.entries()), decision.edits
    rig.standin.block_action(OTHER, CHANNEL, decision.message_id, button(rig, decision, "Approve design v1"))
    wait("the non-approver is told", lambda: ephemeral(rig, OTHER, "^Not authorized$"), 30)
    hold(SCAN)
    assert len(rig.entries()) == before and held.poll() is None  # nothing journaled; the builder still waits
    assert card(rig).message_id == decision.message_id and card(rig).edits == edits


def channel_posts(rig):
    return len([c for c in rig.standin.calls_of("chat.postMessage") if c.params.get("channel") == CHANNEL])


def test_restart_mid_review(rig, subtests):
    _, top, held = to_design_card(rig, "r1", subtests)
    decision = card(rig)
    posts = channel_posts(rig)
    rig.gw.stop()
    rig.gw.start()
    hold(5 + 5 + 3 * SCAN)  # a notifier tick after its 5 s start delay (kanban_watchers.py), then three scans
    with subtests.test("nothing is reposted"):
        assert channel_posts(rig) == posts  # Hermes's own shutdown notice goes elsewhere, not to the run's channel
        cards = [c for c in rig.standin.calls_of("chat.postMessage") if "Approve design v1" in json.dumps(c.params)]
        assert [(c.response or {}).get("ts") for c in cards] == [decision.message_id]  # one card across both lives
    assert parent(rig).message_id == top.message_id and card(rig).message_id == decision.message_id
    checkpoint_b(rig, subtests, "design v1", *DESIGN_V1)
    # The restarted gateway is live, not just quiet: the card still answers the builder's held dialog.
    rig.standin.block_action(APPROVER, CHANNEL, decision.message_id, button(rig, decision, "Approve design v1"))
    assert answered(held)["hookSpecificOutput"]["decision"]["behavior"] == "allow"
