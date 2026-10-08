"""decisions: a persisted request store; transition is the one guard against double clicks and replays."""

import json
import threading

import pytest

from muster import config, core, decisions, runs


def test_create_and_load():
    req = decisions.create("question", "t_1", questions=[{"text": "Q"}])
    assert len(req["id"]) == 10 and req["status"] == "open" and req["ledger"] == "t_1"
    assert req["audit"][0]["step"] == "open" and req["audit"][0]["result"] == "created"
    assert decisions.load(req["id"]) == req


def test_create_retries_a_colliding_id(monkeypatch):
    first = decisions.create("question", "t_1")
    ids = iter([first["id"], "ffffffffff"])
    monkeypatch.setattr(decisions.secrets, "token_hex", lambda n: next(ids))
    assert decisions.create("question", "t_1")["id"] == "ffffffffff"


def test_transition_success_and_refusal():
    rid = decisions.create("question", "t_1")["id"]
    req, ok = decisions.transition(rid, ("open",), "answered", answer={"Q": "A"})
    assert ok and req["status"] == "answered" and req["answer"] == {"Q": "A"}
    assert req["audit"][-1]["step"] == "answered"
    again, ok = decisions.transition(rid, ("open",), "answered", answer={"Q": "B"})
    assert not ok and again["answer"] == {"Q": "A"} and len(again["audit"]) == 2


def test_two_threads_racing_get_one_winner():
    rid = decisions.create("question", "t_1")["id"]
    wins, start = [], threading.Barrier(8)

    def go():
        start.wait()
        wins.append(decisions.transition(rid, ("open",), "answered")[1])

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_terminal_status_archives_and_load_still_finds_it():
    rid = decisions.create("question", "t_1")["id"]
    decisions.transition(rid, ("open",), "done", outcome="finished")
    root = config.data_dir() / "decisions"
    assert not (root / f"{rid}.json").exists() and not (root / f"{rid}.lock").exists()
    assert (root / "archive" / f"{rid}.json").is_file()
    req = decisions.load(rid)
    assert req["status"] == "done" and req["audit"][-1]["result"] == "finished"
    assert decisions.transition(rid, ("open",), "stale")[1] is False


def test_update_merges_without_changing_status_even_when_archived():
    rid = decisions.create("question", "t_1")["id"]
    assert decisions.update(rid, alive=5)["alive"] == 5
    assert decisions.load(rid)["status"] == "open"
    decisions.transition(rid, ("open",), "failed")
    assert decisions.update(rid, outcome="late")["outcome"] == "late"
    assert decisions.load(rid)["status"] == "failed"


def test_note_appends_an_audit_entry():
    rid = decisions.create("question", "t_1")["id"]
    decisions.note(rid, "send", "ok")
    assert decisions.load(rid)["audit"][-1]["step"] == "send"
    assert decisions.load(rid)["audit"][-1]["result"] == "ok"


def test_open_requests_skips_a_corrupt_file_oldest_first():
    a = decisions.create("question", "t_1")["id"]
    b = decisions.create("build", "t_2")["id"]
    done = decisions.create("question", "t_3")["id"]
    decisions.update(a, created_at=2)
    decisions.update(b, created_at=1)
    decisions.transition(done, ("open",), "done")
    decisions.transition(b, ("open",), "answered")
    (config.data_dir() / "decisions" / "bad.json").write_text("{not json")
    assert [r["id"] for r in decisions.open_requests()] == [b, a]
    assert "bad.json" in core.log_path("decisions").read_text()


def test_for_ledger_newest_first_including_archive():
    old = decisions.create("question", "t_1")["id"]
    new = decisions.create("build", "t_1")["id"]
    decisions.create("question", "t_2")
    decisions.update(old, created_at=1)
    decisions.update(new, created_at=2)
    decisions.transition(old, ("open",), "done")
    assert [r["id"] for r in decisions.for_ledger("t_1")] == [new, old]
    assert [r["id"] for r in decisions.for_ledger("t_1", kind="question")] == [old]


def test_stale_others_keeps_one_and_leaves_other_kinds():
    keep = decisions.create("build", "t_1")["id"]
    other = decisions.create("build", "t_1")["id"]
    feedback = decisions.create("feedback", "t_1")["id"]
    elsewhere = decisions.create("build", "t_2")["id"]
    decisions.stale_others("t_1", ("build",), keep, "superseded")
    assert decisions.load(keep)["status"] == "open"
    assert decisions.load(other)["status"] == "stale"
    assert decisions.load(other)["outcome"] == "superseded"
    assert decisions.load(feedback)["status"] == "open"
    assert decisions.load(elsewhere)["status"] == "open"


def test_run_of_adhoc_run():
    run = {"repo": "acme/app", "branch": "muster/x", "base": "main", "worktree": "/wt", "pane": "p_1",
           "title": "T", "card": "t_9", "other": 1}
    runs.run_dir("t_9").mkdir(parents=True)
    (runs.run_dir("t_9") / "run.json").write_text(json.dumps(run))
    assert decisions.run_of("t_9") == {
        "repo": "acme/app", "branch": "muster/x", "base": "main", "worktree": "/wt", "pane": "p_1",
        "title": "T", "evidence_dir": str(runs.run_dir("t_9")), "kind": "adhoc", "card": "t_9"}


def show(monkeypatch, *bodies):
    comments = [{"body": body} for body in bodies]
    monkeypatch.setattr(core, "kanban", lambda *a: json.dumps({"comments": comments}))


def test_run_of_issue_run_takes_the_newest_links_comment(monkeypatch):
    old = {"repo": "acme/app", "issue": 3, "branch": "old", "pane": "p_old"}
    new = {"repo": "acme/app", "issue": 3, "branch": "muster/3", "base": "main", "worktree": "/wt",
           "pane": "p_new", "title": "T", "launch_dir": "/launch"}
    show(monkeypatch, f"{core.LINKS_PREFIX} {json.dumps(old)}", "chatter",
         f"{core.LINKS_PREFIX} {json.dumps(new)}", "later chatter")
    assert decisions.run_of("t_5") == {
        "repo": "acme/app", "issue": 3, "branch": "muster/3", "base": "main", "worktree": "/wt",
        "pane": "p_new", "title": "T", "evidence_dir": "/launch", "kind": "issue", "card": "t_5"}


def test_run_of_issue_run_without_branch_falls_back_to_the_prefix(monkeypatch):
    show(monkeypatch, f"{core.LINKS_PREFIX} {json.dumps({'repo': 'acme/app', 'issue': 7, 'pane': 'p'})}")
    run = decisions.run_of("t_5")
    assert run["branch"] == config.settings["branch_prefix"] + "7" and run["evidence_dir"] is None


def test_run_of_without_any_run_raises(monkeypatch):
    show(monkeypatch, "chatter")
    with pytest.raises(core.CommandError):
        decisions.run_of("t_5")
