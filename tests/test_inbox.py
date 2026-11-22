import argparse
import json
import sys
import time

import pytest

from muster import cli, config, core, decisions, events, inbox, ledger

PROPOSAL = {"version": 1, "sha": "a" * 12}


@pytest.fixture
def home(standalone_home):
    config.load_file()  # flag on: requests and cards live under MUSTER_HOME
    return standalone_home


def run(capsys, *argv):
    code = cli.standalone(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def question(**extra):
    q = {"text": "Which db?", "header": "DB", "multi": False,
         "options": [{"label": "pg", "description": ""}, {"label": "sqlite", "description": ""}]}
    return decisions.create("question", "t_1", questions=[q], choices=[["pg", "sqlite"]], alive=time.time(), **extra)


def build():
    labels = ["Merge pr", "Send back…", "Leave PR open"]
    return decisions.create("build", "t_1", run={}, pr="u", head="h", actions=["merge", "send-back", "nothing"],
                            choices=[labels], questions=[{"text": "Merge?", "header": "Build", "multi": False,
                                                          "options": [{"label": x} for x in labels]}])


def card(key, status="ready"):
    made = json.loads(ledger.call(config.settings["board"], ["create", "--idempotency-key", key, "--json", "--", key]))
    if status != "ready":
        ledger.call(config.settings["board"], ["complete", made["id"], "--summary", "s"])
    return made


def test_a_question_is_listed_and_answered_by_choice(home, capsys):
    rid = question()["id"]
    code, out, _ = run(capsys, "decisions", "list", "--json")
    listed = json.loads(out)["requests"]
    assert code == 0 and [r["id"] for r in listed] == [rid] and listed[0]["choices"] == [["pg", "sqlite"]]
    assert run(capsys, "decisions", "answer", rid, "--choice", "sqlite")[0] == 0
    req = decisions.load(rid)
    assert req["status"] == "answered" and req["answer"] == {"Which db?": "sqlite"}


def test_free_text_answers_the_only_question(home, capsys):
    rid = question()["id"]
    assert run(capsys, "decisions", "answer", rid, "--text=-- use duckdb")[0] == 0
    assert decisions.load(rid)["answer"] == {"Which db?": "-- use duckdb"}


def test_a_wrong_label_records_nothing(home, capsys):
    rid = question()["id"]
    code, _, err = run(capsys, "decisions", "answer", rid, "--choice", "mysql")
    assert code == 2 and err
    req = decisions.load(rid)
    assert req["status"] == "open" and "partial" not in req


def test_a_multi_select_takes_a_json_array_of_labels(home, capsys):
    rid = question()["id"]
    decisions.update(rid, questions=[{**decisions.load(rid)["questions"][0], "multi": True}])
    assert run(capsys, "decisions", "answer", rid, "--choice", '["pg", "mysql"]')[0] == 2
    assert run(capsys, "decisions", "answer", rid, "--choice", '["pg", "sqlite"]')[0] == 0
    assert decisions.load(rid)["answer"] == {"Which db?": "pg, sqlite"}


def test_a_permission_deny_with_a_message_reads_back(home, capsys):
    rid = decisions.create("permission", "t_1", tool={"name": "Bash"}, alive=time.time())["id"]
    assert run(capsys, "decisions", "answer", rid, "--deny", "--message", "no")[0] == 0
    assert decisions.load(rid)["answer"] == {"decision": "deny", "message": "no"}
    other = decisions.create("permission", "t_1", tool={"name": "Bash"}, alive=time.time())["id"]
    assert run(capsys, "decisions", "answer", other, "--choice", "x")[0] == 2
    assert run(capsys, "decisions", "answer", other, "--allow")[0] == 0
    assert decisions.load(other)["answer"] == {"decision": "allow"}


def test_allow_on_a_question_is_refused(home, capsys):
    rid = question()["id"]
    assert run(capsys, "decisions", "answer", rid, "--allow")[0] == 2
    assert decisions.load(rid)["status"] == "open"


def test_a_builder_pane_cannot_answer(home, capsys, monkeypatch):
    rid = question()["id"]
    monkeypatch.setenv("MUSTER_PANE", "1")
    code, _, err = run(capsys, "decisions", "answer", rid, "--choice", "pg")
    assert code == 2 and "pane" in err and decisions.load(rid)["status"] == "open"
    assert run(capsys, "ledger", "show", "t_x")[0] == 2


def test_answer_under_hermes_exits_2_and_records_nothing(home, capsys):
    rid = question()["id"]
    config.standalone = False
    args = argparse.ArgumentParser()
    cli.setup(args)
    code = inbox.main(args.parse_args(["decisions", "answer", rid, "--choice", "pg"]))
    config.standalone = True
    assert code == 2 and "chat channel" in capsys.readouterr().err
    assert decisions.load(rid)["status"] == "open"


def test_a_bad_id_exits_2(home, capsys):
    for argv in (["show", "../x"], ["answer", "XYZ", "--allow"]):
        assert run(capsys, "decisions", *argv)[0] == 2
    assert run(capsys, "decisions", "show", "0" * 16)[0] == 2


def test_show_prints_the_request(home, capsys):
    rid = question()["id"]
    assert json.loads(run(capsys, "decisions", "show", rid)[1])["id"] == rid


def test_a_stale_question_is_ended_not_answered(home, capsys):
    rid = question()["id"]
    decisions.update(rid, alive=time.time() - 3600)
    code, out, _ = run(capsys, "decisions", "answer", rid, "--choice", "pg")
    assert code == 1 and "stopped waiting" in out
    assert decisions.load(rid)["status"] == "stale"


def test_a_build_left_open_ends_done(home, capsys, monkeypatch):
    monkeypatch.setattr(decisions, "obsolete", lambda req: None)
    rid = build()["id"]
    code, out, _ = run(capsys, "decisions", "answer", rid, "--choice", "Leave PR open")
    assert code == 0 and "done" in out
    assert decisions.load(rid)["status"] == "done"


def test_an_obsolete_build_is_staled_not_answered(home, capsys, monkeypatch):
    monkeypatch.setattr(decisions, "obsolete", lambda req: "u is MERGED")
    rid = build()["id"]
    code, out, _ = run(capsys, "decisions", "answer", rid, "--choice", "Leave PR open")
    assert code == 1 and "u is MERGED" in out
    assert decisions.load(rid)["outcome"] == "Not shown: u is MERGED"


def test_answering_twice_says_so(home, capsys):
    rid = question()["id"]
    run(capsys, "decisions", "answer", rid, "--choice", "pg")
    code, out, _ = run(capsys, "decisions", "answer", rid, "--choice", "sqlite")
    assert code == 1 and "already answered" in out
    assert decisions.load(rid)["answer"] == {"Which db?": "pg"}


def test_a_design_card_without_a_request_is_a_review_until_one_exists(home, capsys, monkeypatch):
    monkeypatch.setattr(events, "versions", lambda ledger: [1])
    made = card(f"design:t_1:v1:{PROPOSAL['sha']}", "done")
    reviews = json.loads(run(capsys, "decisions", "list", "--json")[1])["reviews"]
    assert reviews == [{"kind": "design", "ledger": "t_1", "proposal": f"v1:{PROPOSAL['sha']}", "card": made["id"]}]
    decisions.create("design", "t_1", proposal=PROPOSAL)
    assert json.loads(run(capsys, "decisions", "list", "--json")[1])["reviews"] == []


def test_a_superseded_design_card_is_not_a_review(home, capsys, monkeypatch):
    monkeypatch.setattr(events, "versions", lambda ledger: [1, 2])
    card(f"design:t_1:v1:{PROPOSAL['sha']}", "done")
    assert json.loads(run(capsys, "decisions", "list", "--json")[1])["reviews"] == []


def test_a_design_the_builder_was_asked_directly_is_not_a_review(home, capsys, monkeypatch):
    monkeypatch.setattr(events, "versions", lambda ledger: [1])
    card(f"design:t_1:v1:{PROPOSAL['sha']}", "done")
    rid = question(proposal=PROPOSAL)["id"]
    decisions.transition(rid, ("open",), "answered", answer={})
    assert json.loads(run(capsys, "decisions", "list", "--json")[1])["reviews"] == []


def test_a_finished_run_without_a_build_request_is_a_review(home, capsys):
    made = card("run", "done")
    (config.data_dir() / "runs" / made["id"]).mkdir(parents=True)
    (config.data_dir() / "runs" / made["id"] / "run.json").write_text("{}")
    reviews = json.loads(run(capsys, "decisions", "list", "--json")[1])["reviews"]
    assert [(r["kind"], r["ledger"]) for r in reviews] == [("build", made["id"])]
    decisions.create("build", made["id"], head="h")
    assert json.loads(run(capsys, "decisions", "list", "--json")[1])["reviews"] == []


def test_a_closed_run_is_not_a_review(home, capsys):
    made = card("run", "done")
    (config.data_dir() / "runs" / made["id"]).mkdir(parents=True)
    (config.data_dir() / "runs" / made["id"] / "run.json").write_text('{"closed": true}')
    assert json.loads(run(capsys, "decisions", "list", "--json")[1])["reviews"] == []


def test_a_revised_review_card_is_a_review_until_its_head_has_a_request(home, capsys):
    made = card("review:t_1:abc123")
    reviews = json.loads(run(capsys, "decisions", "list", "--json")[1])["reviews"]
    assert reviews == [{"kind": "build", "ledger": "t_1", "head": "abc123", "card": made["id"]}]
    decisions.create("build", "t_1", head="abc123")
    assert json.loads(run(capsys, "decisions", "list", "--json")[1])["reviews"] == []


def test_a_blocked_card_is_listed_with_its_reason(home, capsys):
    made = card("k")
    ledger.call(config.settings["board"], ["block", made["id"], "launch failed", "--kind", "capability"])
    blocked = json.loads(run(capsys, "decisions", "list", "--json")[1])["blocked"]
    assert blocked == [{"card": made["id"], "title": "k", "block_kind": "capability", "reason": "launch failed"}]
    assert "launch failed" in run(capsys, "decisions", "list")[1]


def test_ledger_shows_a_card(home, capsys):
    made = card("k")
    code, out, _ = run(capsys, "ledger", "show", made["id"], "--json")
    assert code == 0 and json.loads(out)["task"]["id"] == made["id"]


def test_ledger_under_hermes_is_refused(home, capsys):
    config.standalone = False
    args = argparse.ArgumentParser()
    cli.setup(args)
    assert inbox.ledger_cmd(args.parse_args(["ledger", "show", "t_1"])) == 2
    assert "hermes kanban" in capsys.readouterr().err


def test_display_redacts_a_token_without_hermes(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.redact", None)
    assert core.display("token ghp_abc123 here") == "token [redacted] here"
