"""cards: every decision card and status line a human reads, rendered by the real Slack channel and kept as golden
text in tests/golden/cards/, so a wording change shows up as a readable diff. Block Kit is flattened: a section's
mrkdwn as is, a button as [ label ] (* when primary), an input as its label and hint, a context line under ⤷."""

import os
import re
import textwrap
from pathlib import Path

import pytest

from muster import config, decisions, progress
from muster.channels import shared, slack

from tests.test_decisions import design_args, rec_args, save_versions, world  # noqa: F401
from tests.test_slack import channel  # noqa: F401

GOLDEN = Path(__file__).parent / "golden" / "cards"
FEEDBACK = ("In README.md, remove “Decide from your phone” from the benefits list. Describe chat (Slack or Telegram) "
            "as where you make decisions, not as a distinguishing benefit. Change nothing else.")
SEND_BACK = ("README still lists “Decide from your phone” as a benefit; you asked for chat to be described as the "
             "interface. Everything else meets the brief.\nMerge triggers: nothing (documentation only).\n"
             "After merge: nothing to activate.")
MERGE = ("Docs-only change; README is now a 140-line manual and every link resolves.\n"
         "Merge triggers: nothing (documentation only).\nAfter merge: nothing to activate.")
DESIGN = ("It splits Muster into three parts: intake reads GitHub issues and starts runs, the launcher starts and "
          "recovers coding agents, and delivery carries their events to you. Nothing changes in behavior.\n"
          "- Limit: imports move, so open branches may need a rebase.")
REVISIONS = "Keep the launcher and intake in one module until the split is needed."


def flat(blocks):
    out = []
    for b in blocks:
        if b["type"] == "section":
            out.append(b["text"]["text"])
        elif b["type"] == "context":
            out.append("⤷ " + " ".join(e["text"] for e in b["elements"]))
        elif b["type"] == "actions":
            out.append("  ".join(f"[ {e['text']['text']} ]{'*' if e.get('style') == 'primary' else ''}"
                                 for e in b["elements"] if e["type"] == "button"))
        elif b["type"] == "input":
            out.append(f"{b['label']['text']}: [ … ]" + (f"\n({b['hint']['text']})" if "hint" in b else ""))
    lines = [w for line in "\n\n".join(out).split("\n") for w in textwrap.wrap(line, 100) or [""]]
    return "\n".join(f"│ {line}".rstrip() for line in lines) + "\n"


@pytest.fixture


def cards(world, channel, tmp_path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setitem(config.settings, "coordinator_name", "Ella")
    monkeypatch.setattr(decisions, "short", lambda run: "Make Muster docs a manual")
    monkeypatch.setattr(shared, "title", lambda req: "Make Muster docs a manual")
    shown, texts = {}, []

    def card(name, req):
        req = decisions.load(req["id"])
        shown[name] = flat(slack.blocks(req, 0))
        decisions.transition(req["id"], ("open",), "stale")  # so the next card for this head is a new request
        return req

    def settled(name, req):
        shown[name] = flat([*slack.sections(shared.settled(decisions.load(req["id"]))), *slack.link(req)])

    def build(choice, feedback=None, summary=SEND_BACK):
        assert decisions.recommend(rec_args(tmp_path, choice, review="Full review.", feedback=feedback,
                                            summary=summary)) == 0
        return decisions.load(capsys.readouterr().out.strip())

    def design(choice, feedback=None):
        texts.append(f"# Plan {len(texts) + 1}\nSplit intake, launcher, delivery.")  # each review gets its own version
        n, sha = len(texts), save_versions(*texts)[-1]
        args = design_args(tmp_path, f"v{n}:{sha}", choice, feedback)
        (tmp_path / "dsum.md").write_text(DESIGN)
        args.summary = str(tmp_path / "dsum.md")
        assert decisions.recommend_design(args) == 0
        return decisions.load(capsys.readouterr().out.strip())

    return type("Cards", (), {"shown": shown, "card": staticmethod(card), "settled": staticmethod(settled),
                              "build": staticmethod(build), "design": staticmethod(design), "world": world,
                              "monkeypatch": monkeypatch})


def compare(shown):
    """Changed a message on purpose? Regenerate with MUSTER_REGOLD=1, then read the golden diff."""
    if os.environ.get("MUSTER_REGOLD"):
        GOLDEN.mkdir(exist_ok=True)
        for name, text in shown.items():
            (GOLDEN / f"{name}.txt").write_text(text)
    for name, text in shown.items():
        assert text == (GOLDEN / f"{name}.txt").read_text(), name


def tap(req, action, text=None):
    decisions.transition(req["id"], ("open",), "answered",
                         answer={"action": action} if text is None else {"action": action, "text": text})
    decisions.execute(req["id"])
    return decisions.load(req["id"])


def test_decision_cards(cards):
    req = cards.card("pr-send-back", cards.build("send-back", FEEDBACK))
    # the feedback the tap sends is on the card, whole, before any tap; the recommended button is the primary one
    assert decisions.quote(FEEDBACK) in slack.mrkdwn(req["questions"][0]["text"]).replace("&gt;", ">")
    cards.card("pr-merge", cards.build("merge", summary=MERGE))
    cards.card("design-approve", cards.design("approve"))
    cards.card("design-revise", cards.design("revise", REVISIONS))
    cards.monkeypatch.setitem(config.settings, "button_hints", False)
    cards.card("pr-send-back-hints-off", cards.build("send-back", FEEDBACK))
    compare(cards.shown)
    off = cards.shown["pr-send-back-hints-off"]
    assert "What each button does" not in off and "Enter sends" not in off
    for kept in ("recommends sending it back", "> In README.md", "Merge triggers", "[ Send back with this feedback ]*",
                 "[ Merge PR #9 ]", "[ Leave PR open ]"):
        assert kept in off, kept
    for text in cards.shown.values():  # no hash, ledger id or "(recommended)" in what the human decides from
        assert not re.search(r"\b[0-9a-f]{12,40}\b|\bt_\w+|\(recommended\)", text), text
        assert text.count("]*") <= 1  # only the recommended button is primary


def test_settled_cards(cards):
    pane = cards.world
    cards.settled("pr-sent-back", tap(cards.build("send-back", FEEDBACK), "send-back"))
    pane.delivers = False
    cards.settled("pr-not-confirmed", tap(cards.build("send-back", FEEDBACK), "send-back"))
    pane.delivers, pane.agent = True, {"agent_status": "working"}
    cards.settled("pr-not-sent-busy", tap(cards.build("send-back", FEEDBACK), "send-back"))
    pane.agent = {"agent_status": "idle"}
    cards.settled("pr-left-open", tap(cards.build("nothing", summary=MERGE), "nothing"))
    cards.settled("pr-merged", tap(cards.build("merge", summary=MERGE), "merge"))  # last: the PR is merged after it
    for name, outcome in [("design-approved", "approve"), ("design-revisions-sent", "revise")]:
        req = cards.design(outcome, REVISIONS if outcome == "revise" else None)
        reply = "Approve" if outcome == "approve" else "Revise"
        decisions.transition(req["id"], ("open",), "answered", answer={"action": "approve" if outcome == "approve"
                                                                          else "send-back"})
        decisions._design(decisions.load(req["id"]), "")
        ask = decisions.create("question", "t_1", proposal=req["proposal"], status="done", outcome=decisions.ANSWER_SENT,
                               partial={"0": [decisions.load(req["id"])["intent"]["reply"], None]},
                               questions=[{"text": "Approve?", "header": "Approval", "options": [], "multi": False}])
        decisions.update(req["id"], intent={**decisions.load(req["id"])["intent"], "question": ask["id"]})
        assert decisions.settle_design(decisions.load(req["id"])) is None, reply
        cards.settled(name, req)
    compare(cards.shown)
    for name, text in cards.shown.items():  # delivery is not work done
        assert "is coding" not in text and "is revising" not in text, name
    assert "could not be confirmed" in cards.shown["pr-not-confirmed"]
    assert "did not send" not in cards.shown["pr-not-confirmed"]
    assert "Muster did not send it" in cards.shown["pr-not-sent-busy"]


def test_status_lines(cards, monkeypatch):
    ledger = "t_1"
    progress.touch(ledger, "launched", '{"dir": "/nowhere", "sha256": "x"}')
    lines = [progress.opened("https://github.com/acme/app/pull/57")]
    progress.touch(ledger, "proposal", "v1")
    lines.append(progress.state(ledger, 0, [], None, False)[1])
    lines.append(progress.state(ledger, 0, [(0, "PR #57")], None, False)[1])
    progress.touch(ledger, "recommended")
    progress.touch(ledger, "review", "https://github.com/acme/app/pull/57")
    lines.append(progress.state(ledger, 0, [], None, False)[1])
    progress.touch(ledger, "revision")
    lines.append(progress.state(ledger, 0, [], None, False)[1])
    lines.append(progress.state(ledger, 0, [], {"resolved_at": 1}, False)[1])
    cards.shown["status-lines"] = "\n".join(re.sub(r"\d\d:\d\d", "HH:MM", line) for line in lines) + "\n"
    compare(cards.shown)
    assert not any("done" in line.lower() or "no action needed" in line.lower() for line in lines)


def test_the_builders_approval_question_while_the_design_is_reviewed(cards):
    progress.touch("t_1", "launched", '{"dir": "/nowhere", "sha256": "x"}')
    progress.touch("t_1", "proposal", "v1")
    ask = decisions.create("question", "t_1", proposal={"version": 1, "sha": "a" * 12}, choices=[["Approve", "Revise"]],
                           questions=[{"text": "Implement design v1?", "header": "Approval", "multi": False,
                                       "options": [{"label": x, "description": ""} for x in ("Approve", "Revise")]}])
    cards.shown["design-reviewing-question"] = flat(slack.blocks(ask, 0))
    compare(cards.shown)
    assert "Nothing to decide yet" in cards.shown["design-reviewing-question"]


def test_decisions_with_their_markdown_files_on_both_platforms(cards):
    """The sample messages a design decision posts: its files (names and the line above them), then its card, as each
    channel renders it; and the card when the files could not be attached."""
    from muster.channels import telegram

    req = cards.design("revise", REVISIONS)
    docs = shared.documents(req)
    names = [fname for fname, _ in docs]
    assert docs[1][1] == "# Plan 1\nSplit intake, launcher, delivery." and docs[0][1] == "Small and right."
    files = "\n".join(f"│ 📎 {fname}" for fname in names)
    slack_req = {**req, "detail": {"platform": "slack", "files": names, "url": "https://example.slack.com/files/F1",
                                   "links": [["Full review", "https://example.slack.com/files/F1"],
                                             ["Design v1", "https://example.slack.com/files/F2"]]}}
    cards.shown["design-files-slack"] = f"│ {shared.caption(req)}\n{files}\n│\n" + flat(slack.blocks(slack_req, 0))
    tg_req = {**req, "detail": {"platform": "telegram", "files": names}}
    buttons = "\n".join(f"│ [ {label} ]" for label in [*telegram.buttons(tg_req, 0), "✏️ Other (type answer)"])
    cards.shown["design-files-telegram"] = (
        "\n".join(f"│ 📎 {fname} · caption: {shared.caption(req)}" for fname in names) + "\n│\n"
        + "\n".join(f"│ {line}".rstrip() for line in telegram.render(tg_req, 0).split("\n")) + "\n" + buttons + "\n")
    failed = {**req, "detail": {"platform": "slack", "files": [], "failed": "missing_scope"}}
    cards.shown["design-files-failed-slack"] = flat(slack.blocks(failed, 0))
    compare(cards.shown)
    assert names == ["make-muster-docs-a-manual-design-v1-review.md", "make-muster-docs-a-manual-design-v1.md"]
    for name in ("design-files-slack", "design-files-telegram"):
        assert "> " + REVISIONS in cards.shown[name] and "Small and right." not in cards.shown[name]
