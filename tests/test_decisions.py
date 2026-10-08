"""decisions: a persisted request store; transition is the one guard against double clicks and replays."""

import json
import threading

import pytest

from muster import config, core, decisions, events, runs


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
    assert not (root / f"{rid}.json").exists()
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
    run = {"repo": "acme/app", "branch": "muster/x", "base": "main", "title": "T", "card": "t_9",
           "launch": {"path": "/wt", "pane": "p_1"}}
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


# -- recommend (task 5) ---------------------------------------------------------------------------

import argparse  # noqa: E402

URL = "https://github.com/acme/app/pull/9"
HEAD = "a" * 40
RUN = {"repo": "acme/app", "issue": 3, "branch": "muster/3", "base": "main", "worktree": "/wt", "pane": "p_1",
       "title": "T", "evidence_dir": None, "kind": "issue", "card": "t_1"}


class World:
    """gh pr view/merge with a mutable pull request, and herdr agent get/prompt, as the real tools answer."""

    def __init__(self, tmp):
        self.pr = {"url": URL, "state": "OPEN", "headRefOid": HEAD, "headRefName": "muster/3", "baseRefName": "main",
                   "isDraft": False, "isCrossRepository": False, "mergeStateStatus": "CLEAN",
                   "autoMergeRequest": None, "mergeCommit": None}
        self.calls, self.prompts, self.evidence = [], [], tmp / "evidence"
        self.agent = {"agent_status": "idle"}  # None: no agent
        self.merge_error = self.gh_missing = self.prompt_error = None
        self.merges_on_call = True  # the merge call really merges
        self.delivers = True  # the pane's UserPromptSubmit hook fires

    def run(self, argv, env=None):
        self.calls.append((argv, env))
        if argv[0] == "gh" and self.gh_missing:
            raise FileNotFoundError(2, "No such file or directory: 'gh'")
        if argv[:3] == ["gh", "pr", "view"]:
            return json.dumps({k: self.pr[k] for k in argv[argv.index("--json") + 1].split(",")})
        if argv[:3] == ["gh", "pr", "merge"]:
            if self.merge_error:
                raise core.CommandError(self.merge_error)
            if self.merges_on_call:
                self.pr.update(state="MERGED", mergeCommit={"oid": "c" * 40})
            return ""
        if argv[:3] == ["herdr", "agent", "get"]:
            if self.agent is None:
                raise core.CommandError("herdr: exit 1\nagent_not_found")
            return json.dumps({"result": {"agent": self.agent}})
        if argv[:3] == ["herdr", "agent", "prompt"]:
            self.prompts.append(argv[4])
            if self.delivers:
                core.prompt_seen(self.evidence, {"prompt": argv[4]})
            if self.prompt_error:
                raise core.CommandError(self.prompt_error)
            return json.dumps({"result": {}})
        raise AssertionError(argv)

    def merge_calls(self):
        return [a for a, _ in self.calls if a[:3] == ["gh", "pr", "merge"]]


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    run = {**RUN, "evidence_dir": str(w.evidence)}
    monkeypatch.setattr(core, "run", w.run)
    monkeypatch.setattr(decisions, "run_of", lambda ledger: dict(run))
    monkeypatch.setattr(decisions, "READBACK_WAIT", 0, raising=False)
    monkeypatch.setattr(decisions, "SEEN_WAIT", 0, raising=False)
    w.run_dict = run
    return w


def files(tmp_path, review="Looks good.", feedback=None):
    (tmp_path / "review.md").write_text(review)
    if feedback is not None:
        (tmp_path / "fb.md").write_text(feedback)
    return str(tmp_path / "review.md"), (str(tmp_path / "fb.md") if feedback is not None else None)


def rec_args(tmp_path, choice="merge", head=HEAD, pr=URL, review="Looks good.", feedback=None):
    r, f = files(tmp_path, review, feedback)
    return argparse.Namespace(ledger="t_1", pr=pr, head=head, choice=choice, review=r, feedback=f)


def test_recommend_creates_the_build_request(world, tmp_path, capsys):
    assert decisions.recommend(rec_args(tmp_path, "send-back", feedback="Fix the null case.")) == 0
    rid = capsys.readouterr().out.strip()
    req = decisions.load(rid)
    assert req["kind"] == "build" and req["status"] == "open" and req["ledger"] == "t_1"
    assert req["choices"] == [["Send back (recommended)", "Merge (squash)", "Do nothing"]]
    assert req["actions"] == ["send-back", "merge", "nothing"]
    assert req["questions"] == [{"text": "Looks good.", "header": "Build", "multi": False, "options": [
        {"label": label, "description": ""} for label in req["choices"][0]]}]
    assert (req["head"], req["base"], req["pr"], req["cycle"]) == (HEAD, "main", URL, 1)
    assert req["review"] == "Looks good." and req["feedback"] == "Fix the null case."
    assert req["run"]["pane"] == "p_1" and req["run"]["repo"] == "acme/app"


@pytest.mark.parametrize("choice, first", [("merge", "Merge (squash) (recommended)"), ("nothing", "Do nothing (recommended)")])
def test_recommended_choice_goes_first(world, tmp_path, capsys, choice, first):
    decisions.recommend(rec_args(tmp_path, choice))
    req = decisions.load(capsys.readouterr().out.strip())
    assert req["choices"][0][0] == first and req["actions"][0] == choice and len(req["actions"]) == 3


def test_texts_are_redacted(world, tmp_path, capsys):
    token = "ghp_" + "x" * 20
    decisions.recommend(rec_args(tmp_path, "send-back", review=f"saw {token}", feedback=f"use {token}"))
    req = decisions.load(capsys.readouterr().out.strip())
    assert token not in json.dumps(req) and "[redacted]" in req["review"] and "[redacted]" in req["feedback"]


@pytest.mark.parametrize("change, reason", [
    ({"headRefName": "other"}, "branch"), ({"baseRefName": "dev"}, "targets dev"), ({"isCrossRepository": True}, "fork"),
    ({"isDraft": True}, "draft"), ({"state": "CLOSED"}, "OPEN"), ({"headRefOid": "b" * 40}, "moved to bbbbbbb")])
def test_refuses_a_pull_request_that_is_not_this_runs(world, tmp_path, capsys, change, reason):
    world.pr.update(change)
    assert decisions.recommend(rec_args(tmp_path)) == 1
    captured = capsys.readouterr()
    assert reason in captured.err and captured.out == "" and decisions.for_ledger("t_1") == []


def test_refuses_a_url_of_another_repo(world, tmp_path, capsys):
    assert decisions.recommend(rec_args(tmp_path, pr="https://github.com/acme/other/pull/9")) == 1
    assert "acme/app" in capsys.readouterr().err


def test_refuses_a_head_argument_that_is_not_the_prs(world, tmp_path, capsys):
    assert decisions.recommend(rec_args(tmp_path, head="b" * 40)) == 1


def test_send_back_needs_feedback(world, tmp_path, capsys):
    assert decisions.recommend(rec_args(tmp_path, "send-back")) == 1
    assert "--feedback" in capsys.readouterr().err


def test_refuses_empty_or_oversize_files(world, tmp_path, capsys):
    assert decisions.recommend(rec_args(tmp_path, review="  \n")) == 1
    assert decisions.recommend(rec_args(tmp_path, "send-back", feedback="")) == 1
    assert decisions.recommend(rec_args(tmp_path, review="x" * (events.PROPOSAL_MAX + 1))) == 1
    assert decisions.for_ledger("t_1") == []


def test_a_run_that_cannot_be_found_is_a_refusal(world, tmp_path, monkeypatch, capsys):
    def none(ledger):
        raise core.CommandError("no run found for card t_1")

    monkeypatch.setattr(decisions, "run_of", none)
    assert decisions.recommend(rec_args(tmp_path)) == 1
    assert "no run found" in capsys.readouterr().err


def test_cycle_counts_sent_feedback_requests(world, tmp_path, capsys):
    for outcome in ("Sent ✓", "Sent ✓", "Not sent"):
        rid = decisions.create("feedback", "t_1")["id"]
        decisions.transition(rid, ("open",), "done", outcome=outcome)
    decisions.recommend(rec_args(tmp_path))
    assert decisions.load(capsys.readouterr().out.strip())["cycle"] == 3


def test_older_open_build_requests_are_staled(world, tmp_path, capsys):
    old = decisions.create("build", "t_1", head="b" * 40)["id"]
    other = decisions.create("build", "t_2", head="b" * 40)["id"]
    decisions.recommend(rec_args(tmp_path))
    new = capsys.readouterr().out.strip()
    assert decisions.load(old)["status"] == "stale" and decisions.load(other)["status"] == "open"
    assert decisions.load(new)["status"] == "open"


def test_the_same_head_again_returns_the_open_request_and_ignores_the_new_text(world, tmp_path, capsys):
    decisions.recommend(rec_args(tmp_path, review="first"))
    first = capsys.readouterr().out.strip()
    assert decisions.recommend(rec_args(tmp_path, "nothing", review="second")) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == first and "ignored" in captured.err
    assert decisions.load(first)["review"] == "first" and len(decisions.for_ledger("t_1")) == 1
