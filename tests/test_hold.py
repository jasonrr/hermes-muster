"""hold (#34): a cheap model judges an automatic approval against the repository's live runs; a duplicate is held
and the judgment handed to the live run's agent, which answers `hook duplicate <n> same|different`."""

import argparse
import json
import time
from types import SimpleNamespace

import pytest

import muster.core as core
from muster import config, events, hold
from tests.fake_herdr import World

REPO = "Radical-Candor-LLC/radicalcandorai"
SENTRY = {"login": "sentry[bot]", "id": 39604003}
JASON = {"login": "jasonrr", "id": 108170}
CREATED = {493: "2026-10-15T23:34:11Z", 494: "2026-10-15T23:34:13Z", 495: "2026-10-15T23:34:16Z"}


def tick(dry_run=False):
    return core.tick(argparse.Namespace(dry_run=dry_run))


class Model:
    """ctx.llm.complete_structured: answers[issue number] is duplicate_of, an Exception to raise, or "garbage"."""

    def __init__(self):
        self.answers, self.calls = {}, []
        self.llm = self

    def complete_structured(self, **kw):
        data = json.loads(kw["input"][0]["text"])
        self.calls.append((data["new_issue"]["number"], [r["number"] for r in data["live_runs"]], kw))
        answer = self.answers.get(data["new_issue"]["number"])
        if isinstance(answer, Exception):
            raise answer
        if answer == "garbage":
            return SimpleNamespace(parsed=None, text="not json")
        return SimpleNamespace(parsed={"duplicate_of": answer, "reason": "Same Redis timeout in signup."})


class Fake:
    """gh and hermes kanban for three Sentry issues; herdr and git are fake_herdr's World."""

    model = None

    def __init__(self, tmp_path, numbers=(493, 494, 495)):
        self.world = World(tmp_path)
        self.issues = {n: {"number": n, "title": "RedisStorageError: Redis call timed out after 2.0s",
                           "body": f"stack {n}", "state": "open", "state_reason": None, "user": SENTRY,
                           "created_at": CREATED.get(n, "2026-10-15T23:34:20Z"),
                           "labels": [{"name": "agent-ready"}, {"name": "automatic-approval"}, {"name": "bug"}]}
                       for n in numbers}
        self.events = {n: [{"id": 1000 + n, "event": "labeled", "actor": SENTRY, "label": {"name": "agent-ready"}},
                           {"id": 2000 + n, "event": "labeled", "actor": SENTRY,
                            "label": {"name": "automatic-approval"}}] for n in numbers}
        self.cards, self.keys, self.subs, self.calls = {}, {}, {}, []
        self.world.on_submit = self.evidence
        self.order = list(numbers)[::-1]  # the API lists newest first

    def evidence(self, text):
        """Claude's UserPromptSubmit hook in every run's pane: the prompt's sha beside each launch record."""
        for record in core.intake_dir().glob("*/launch.json"):
            core.prompt_seen(record.parent, {"prompt": text})

    def kanban(self, argv):
        verb, rest = argv[4], argv[5:]
        if verb == "create":
            key = rest[rest.index("--idempotency-key") + 1]
            live = [c for c in self.keys.get(key, []) if self.cards[c]["status"] != "archived"]
            if live:
                card = self.cards[live[0]]
                return json.dumps({"id": live[0], "status": card["status"], "assignee": None,
                                   "created_at": card["created_at"]})
            cid = f"t_{len(self.cards) + 1}"
            self.cards[cid] = {"status": "ready", "comments": [], "blocks": [], "title": argv[-1],
                               "body": rest[rest.index("--body") + 1], "created_at": int(time.time()) - 60}
            self.keys.setdefault(key, []).append(cid)
            return json.dumps({"id": cid, "status": "ready", "assignee": None, "created_at": int(time.time())})
        card = rest[rest.index("--") + 1] if verb == "block" else rest[0]
        if card not in self.cards:
            raise core.CommandError(f"hermes kanban: exit 1\nno such task: {card}")
        c = self.cards[card]
        if verb == "show":
            return json.dumps({"task": {"id": card, "status": c["status"], "assignee": None},
                               "comments": [{"body": b} for b in c["comments"]]})
        if verb == "comment":
            c["comments"].append(rest[1])
        elif verb == "block":
            assert c["status"] == "ready", "hermes refuses a second block"
            kind = rest[rest.index("--kind") + 1] if "--kind" in rest[:rest.index("--")] else None
            c["blocks"].append(kind)
            c["status"] = "blocked"
        elif verb == "unblock":
            c["status"] = "ready"
        elif verb == "archive":
            c["status"] = "archived"
        elif verb == "notify-subscribe":
            flag = dict(zip(rest[1::2], rest[2::2]))
            rows = self.subs.setdefault(card, [])
            row = {"chat_id": flag["--chat-id"], "thread_id": flag.get("--thread-id", ""), "user_id": flag["--user-id"],
                   "chat_type": flag["--chat-type"], "notifier_profile": flag["--notifier-profile"],
                   "delivery_mode": flag["--delivery-mode"]}
            if row not in rows:
                rows.append(row)
        elif verb == "notify-unsubscribe":
            flag = dict(zip(rest[1::2], rest[2::2]))
            rows = self.subs.get(card, [])
            keep = [r for r in rows if (r["chat_id"], r["thread_id"]) != (flag["--chat-id"], flag.get("--thread-id", ""))]
            if len(keep) == len(rows):
                raise core.CommandError("(no such subscription)")
            self.subs[card] = keep
        elif verb == "notify-list":
            return json.dumps(self.subs.get(card, []))
        else:
            raise AssertionError(argv)
        return ""

    def run(self, argv):
        self.calls.append(argv)
        out = self.world.handle(argv)
        if out is not None:
            return out
        if argv[:4] == ["hermes", "kanban", "boards", "list"]:
            return json.dumps([{"slug": "muster"}])
        if argv[:2] == ["hermes", "kanban"]:
            return self.kanban(argv)
        if argv[:2] == ["gh", "api"]:
            url = argv[-1]
            if "/issues?" in url:
                if not url.startswith(f"repos/{REPO}/"):
                    return ""
                return "".join(json.dumps(self.issues[n]) + "\n" for n in self.order if self.issues[n]["state"] == "open")
            number = int(url.split("/issues/")[1].split("/")[0])
            if url.endswith("/timeline?per_page=100"):
                return "".join(json.dumps(e) + "\n" for e in self.events[number])
            return json.dumps(self.issues[number])
        raise AssertionError(argv)

    # views
    def card_of(self, number):
        """The newest card of the issue's newest approval."""
        return [c for k, cards in self.keys.items() if k.startswith(f"{REPO}#{number}@") for c in cards][-1]

    def launched(self):
        return [c[c.index("--branch") + 1] for c in self.calls if c[:3] == ["herdr", "worktree", "create"]]

    def prompts(self, pane):
        return [c[4] for c in self.calls if c[:3] == ["herdr", "agent", "prompt"] and c[3] == pane]

    def pane(self, number):
        return json.loads((core.intake_dir() / self.card_of(number) / "launch.json").read_text())["launch"]["pane"]

    def idle(self, number):
        self.world.agents[self.pane(number)]["agent_status"] = "idle"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setitem(config.settings, "auto_approvers", [{**SENTRY, "repos": [REPO.lower()]}])
    f = Fake(tmp_path)
    monkeypatch.setattr(core, "run", f.run)
    f.model = Model()
    monkeypatch.setattr(hold, "CTX", f.model)
    f.model.answers = {494: 493, 495: 493}
    return f


def held(fake, number):
    return hold.load(fake.card_of(number))


# the incident ------------------------------------------------------------------------------------------------

def test_the_incident_replay_launches_one_builder_and_hands_two_reports_to_it(fake):
    assert tick() == 0
    assert fake.launched() == ["muster/493"]
    assert [(n, live) for n, live, _ in fake.model.calls] == [(494, [493]), (495, [493])]
    kw = fake.model.calls[0][2]
    assert kw["task"] == "muster_dedupe" and "untrusted data" in kw["instructions"] and "model" not in kw
    for n in (494, 495):
        card = fake.cards[fake.card_of(n)]
        assert card["status"] == "blocked" and card["blocks"] == [None]  # generic: never needs_input
        assert card["body"].startswith(f"{REPO}#{n}")
        rec = held(fake, n)
        assert rec["candidates"] == [{"card": fake.card_of(493), "issue": 493}] and rec["resolution"] is None
    # Tick 2: 493's agent is busy with its brief: nothing is sent.
    pane = fake.pane(493)
    assert tick() == 0 and len(fake.prompts(pane)) == 1  # only its brief
    fake.idle(493)
    assert tick() == 0
    [sent] = fake.prompts(pane)[1:]
    assert sent.startswith("muster: a model judged") and "\n" not in sent and "#494" in sent
    assert "hook duplicate 494 same" in sent and "stack 494" not in sent  # issue text never in the instruction
    assert held(fake, 494)["delivery"]["delivered"] is False  # the evidence is read on the next tick
    fake.idle(493)
    assert tick() == 0
    assert held(fake, 494)["delivery"]["delivered"] is True
    assert len(fake.prompts(pane)) == 3 and "#495" in fake.prompts(pane)[2]
    fake.idle(493)
    for _ in range(3):
        assert tick() == 0
    assert len(fake.prompts(pane)) == 3 and fake.launched() == ["muster/493"]
    assert all(fake.cards[fake.card_of(n)]["blocks"] == [None] for n in (494, 495))


@pytest.mark.parametrize("answer", [None, "garbage", 999, True, RuntimeError("no provider configured")])
def test_anything_but_a_live_run_launches_as_before(fake, answer):
    fake.model.answers = {494: answer, 495: answer}
    assert tick() == 0
    assert fake.launched() == ["muster/493", "muster/494", "muster/495"]
    comments = fake.cards[fake.card_of(494)]["comments"]
    assert any(c.startswith("dedupe check failed") for c in comments) == (answer is not None)


def test_no_plugin_llm_access_launches_and_says_why(fake, monkeypatch):
    monkeypatch.setattr(hold, "CTX", None)
    assert tick() == 0
    assert len(fake.launched()) == 3
    assert any("no Hermes plugin LLM access" in c for c in fake.cards[fake.card_of(495)]["comments"])


def test_without_a_live_run_there_is_no_model_call(fake):
    fake.issues = {493: fake.issues[493]}
    fake.order = [493]
    assert tick() == 0 and fake.model.calls == [] and fake.launched() == ["muster/493"]


def test_a_human_approval_is_never_judged(fake):
    for n in (494, 495):
        fake.issues[n]["user"] = JASON
        fake.events[n] = [{"id": 1000 + n, "event": "labeled", "actor": JASON, "label": {"name": "agent-ready"}}]
    assert tick() == 0
    assert [n for n, _, _ in fake.model.calls] == [] and len(fake.launched()) == 3


@pytest.mark.parametrize("gone", ["archived", "agent-gone", "other-agent"])
def test_a_run_that_is_not_live_is_no_candidate(fake, gone):
    fake.issues = {493: fake.issues[493]}
    fake.order = [493]
    assert tick() == 0
    if gone == "archived":
        fake.cards[fake.card_of(493)]["status"] = "archived"
        fake.issues[493]["state"] = "closed"  # else the open labeled issue gets a new card, as today
    elif gone == "agent-gone":
        del fake.world.agents[fake.pane(493)]
    else:
        fake.world.agents[fake.pane(493)]["name"] = "someone-else"
    fake.issues[494] = Fake(core.intake_dir(), (494,)).issues[494]
    fake.events[494] = Fake(core.intake_dir(), (494,)).events[494]
    fake.order = [494, 493]
    assert tick() == 0
    assert fake.model.calls == [] and fake.launched() == ["muster/493", "muster/494"]


# the agent's verdict ------------------------------------------------------------------------------------------

def delivered(fake):
    tick()
    fake.idle(493)
    tick()  # 494 sent
    fake.idle(493)
    tick()  # 494 delivered, 495 sent


def hook(fake, number, verdict, card_issue=493):
    link = {"repo": REPO, "card": fake.card_of(card_issue), "issue": card_issue}
    return events.duplicate(link, str(number), verdict)


def test_different_launches_the_held_issue_once(fake, capsys):
    tick()
    assert hook(fake, 494, "different") == 1  # nothing was sent yet
    assert "not sent to this run yet" in capsys.readouterr().err
    delivered(fake)
    assert hook(fake, 494, "different") == 0
    assert hook(fake, 494, "same") == 0 and "already recorded: different" in capsys.readouterr().out
    assert tick() == 0
    assert fake.launched() == ["muster/493", "muster/494"]
    rec = held(fake, 494)
    assert rec["resolution"]["state"] == "released" and rec["finished"] is True
    assert fake.cards[fake.card_of(494)]["blocks"] == [None]  # launched: unblocked, no new block
    assert tick() == 0 and fake.launched() == ["muster/493", "muster/494"]


def test_same_comments_once_and_stays_held_until_the_issue_closes(fake):
    delivered(fake)
    assert hook(fake, 494, "same") == 0
    tick()
    tick()
    card = fake.cards[fake.card_of(494)]
    assert [c for c in card["comments"] if c.startswith("confirmed")] and card["status"] == "blocked"
    assert len([c for c in card["comments"] if c.startswith("confirmed")]) == 1
    # The canonical PR merges with `Closes #494`: GitHub closes it, and the hold ends.
    fake.issues[494].update(state="closed", state_reason="completed")
    assert tick() == 0
    assert card["status"] == "archived" and held(fake, 494)["resolution"]["state"] == "closed"
    assert fake.launched() == ["muster/493"]


def test_a_hook_from_another_run_or_for_an_unknown_issue_is_refused(fake, capsys):
    delivered(fake)
    assert hook(fake, 777, "same") == 1
    assert events.duplicate({"repo": REPO, "card": "t_other", "issue": 5}, "494", "same") == 1
    assert events.duplicate({"repo": REPO, "card": fake.card_of(493), "issue": 493}, "x", "same") == 2
    assert held(fake, 494)["verdict"] is None


# lifecycle ----------------------------------------------------------------------------------------------------

def test_a_closed_held_issue_is_archived_and_a_reopen_needs_a_new_label_event(fake):
    tick()
    fake.issues[495].update(state="closed", state_reason="duplicate")
    assert tick() == 0
    card = fake.card_of(495)
    assert fake.cards[card]["status"] == "archived"
    assert sum(c.startswith(f"not dispatched {card}:") for c in fake.cards[card]["comments"]) == 1
    fake.issues[495].update(state="open", state_reason="reopened")
    cards = len(fake.cards)
    assert tick() == 0
    assert len(fake.cards) == cards and fake.card_of(495) == card  # no new card for the same approval
    fake.events[495].append({"id": 3495, "event": "labeled", "actor": SENTRY, "label": {"name": "agent-ready"}})
    fake.events[495].append({"id": 3496, "event": "labeled", "actor": SENTRY, "label": {"name": "automatic-approval"}})
    assert tick() == 0
    assert fake.card_of(495) != card  # a new approval: a new card, judged again


def test_a_held_card_archived_by_hand_is_closed_not_recreated(fake):
    tick()
    card = fake.card_of(495)
    fake.cards[card]["status"] = "archived"
    tick()
    tick()
    assert held(fake, 495)["resolution"]["reason"] == "archived" and fake.card_of(495) == card


def test_recover_releases_a_held_card_and_refuses_a_closed_one(fake, capsys):
    tick()
    assert core.recover(argparse.Namespace(card=fake.card_of(494), resend=False, adopt=False)) == 0
    assert fake.launched() == ["muster/493", "muster/494"]
    assert "released (recover" in capsys.readouterr().out
    fake.issues[495].update(state="closed", state_reason="not_planned")
    tick()
    assert core.recover(argparse.Namespace(card=fake.card_of(495), resend=False, adopt=False)) == 1
    assert fake.launched() == ["muster/493", "muster/494"]


def test_a_revoked_approval_is_not_released_and_says_so_once(fake):
    delivered(fake)
    # Someone else relabels it: no approval stands (a relabel by the approver would be a new, valid one).
    fake.events[494].append({"id": 9, "event": "labeled", "actor": {"login": "someone", "id": 1},
                             "label": {"name": "agent-ready"}})
    hook(fake, 494, "different")
    tick()
    tick()
    card = fake.cards[fake.card_of(494)]
    assert fake.launched() == ["muster/493"]
    assert sum(c.startswith(f"not released {fake.card_of(494)}:") for c in card["comments"]) == 1


def test_a_gone_agent_gets_one_notice_and_no_delivery(fake):
    tick()
    del fake.world.agents[fake.pane(493)]
    tick()
    tick()
    notices = [c for c in fake.cards.values() if c["title"].startswith("Still held: radicalcandorai#494")]
    assert len(notices) == 1 and "is gone" in notices[0]["body"] and notices[0]["blocks"] == [None]
    assert len(fake.prompts(fake.pane(493))) == 1


def test_no_answer_in_24_hours_is_one_reminder(fake, monkeypatch):
    delivered(fake)
    now = time.time()
    monkeypatch.setattr(hold, "clock", lambda: now + 86400)
    tick()
    tick()
    notices = [c for c in fake.cards.values() if c["title"].startswith("Still held: radicalcandorai#494")]
    assert len(notices) == 1 and "has not answered" in notices[0]["body"]


def test_a_pending_wait_in_the_live_run_holds_the_delivery(fake):
    tick()
    fake.idle(493)
    launch = json.loads((core.intake_dir() / fake.card_of(493) / "launch.json").read_text())["launch"]
    git_dir = fake.world.worktrees[launch["path"]]["git_dir"]
    (core.Path(git_dir) / core.WAIT_KIND).write_text("t_wait")
    tick()
    assert len(fake.prompts(fake.pane(493))) == 1


@pytest.mark.parametrize("step", ["notify-subscribe", "block"])
def test_a_crash_after_any_hold_step_repeats_nothing(fake, step):
    fake.world.crash_after = lambda argv: argv[:5] == ["hermes", "kanban", "--board", "muster", step] and "t_2" in argv
    with pytest.raises(KeyboardInterrupt):
        tick()
    tick()
    tick()
    assert fake.launched() == ["muster/493"]
    for n in (494, 495):
        assert fake.cards[fake.card_of(n)]["blocks"] == [None]


def test_dry_run_judges_and_writes_nothing(fake, capsys):
    tick()
    before = {k: dict(v) for k, v in fake.cards.items()}
    fake.issues[496] = Fake(core.intake_dir(), (496,)).issues[496]
    fake.events[496] = Fake(core.intake_dir(), (496,)).events[496]
    fake.order = [496] + fake.order
    assert tick(dry_run=True) == 0
    assert "#496 would judge against #493" in capsys.readouterr().out
    assert {k: dict(v) for k, v in fake.cards.items()} == before


def test_a_released_run_with_a_topic_pings_only_in_its_topic(fake):
    from muster import conversation
    tick()
    card = fake.card_of(494)
    assert [r["thread_id"] for r in fake.subs[card]] == [""]  # held: the main chat
    conversation.update(card, state="open", thread_id="41")  # the topic the launch asks the gateway for
    assert hold.release(card) == 0
    assert [r["thread_id"] for r in fake.subs[card]] == ["41"]
