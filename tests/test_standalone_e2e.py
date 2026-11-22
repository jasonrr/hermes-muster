"""One whole standalone run with Hermes made unimportable and every `hermes` argv a failure: launch, a hook, a
decision answered from the command line."""

import argparse
import io
import json
import sys
import time

import muster.adhoc as adhoc
import muster.cli as cli
import muster.config as config
import muster.core as core
import muster.decisions as decisions
import muster.ledger as ledger
import muster.runs as runs
from tests.fake_herdr import World


def test_a_standalone_run_needs_no_hermes(standalone_home, tmp_path, monkeypatch, capsys):
    for name in ("hermes_cli", "hermes_constants", "gateway", "agent", "plugins", "tools"):
        monkeypatch.setitem(sys.modules, name, None)  # the top-level packages; muster.gateway is muster's own
    w = World(tmp_path)

    def run(argv):
        assert argv[0] != "hermes", argv
        if argv[:1] == ["git"] and argv[3:] == ["remote", "get-url", "origin"]:
            return "git@github.com:Radical-Candor-LLC/radicalcandorai.git\n"
        out = w.handle(argv)
        assert out is not None, argv
        return out
    monkeypatch.setattr(core, "run", run)
    clone = tmp_path / "clones" / "radicalcandorai"
    brief = tmp_path / "brief.md"
    brief.write_text("Add a --json flag. Keep text output.")

    assert cli.standalone(["launch", "--cwd", str(clone), "--branch", "feat/x", "--title", "t",
                           "--brief", str(brief)]) == 0
    launched = json.loads(capsys.readouterr().out)
    card = launched["card"]
    [rec] = ledger.cards(config.settings["board"])
    assert rec["id"] == card and rec["status"] == "ready"
    directory = runs.run_dir(card)
    texts = [directory / "settings.json", directory / "brief.md"]
    for path in texts:
        text = path.read_text()
        assert "/bin/muster hook" in text and "hermes muster" not in text, path

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"message": "Claude needs your permission"})))
    assert adhoc.hook(argparse.Namespace(card=card, event="notification", url=None)) == 0
    assert any(c["status"] == "blocked" for c in ledger.cards(config.settings["board"]) if c["id"] != card)
    assert runs.flush(argparse.Namespace()) in (0, None)

    req = decisions.create("question", card, tool={"name": "AskUserQuestion"}, proposal=None, wait=None,
                           run={"repo": "radicalcandorai", "pane": "p1", "kind": "adhoc", "branch": "feat/x"},
                           alive=time.time(), questions=[{"text": "Which?", "header": "Pick", "multi": False,
                           "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}],
                           choices=[["A", "B"]])
    assert cli.standalone(["decisions", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in listed["requests"]] == [req["id"]]
    assert cli.standalone(["decisions", "answer", req["id"], "--choice", "A"]) == 0
    done = decisions.load(req["id"])
    assert done["status"] == "answered" and done["answer"] == {"Which?": "A"}
