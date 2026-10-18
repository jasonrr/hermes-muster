"""Every prompt muster assembles is byte-identical to its output before the prompts moved to prompts/.

The golden files in tests/golden/ were rendered by the code before the move, with data full of characters a
template could misread: braces, `$`, backticks, quotes and newlines.
"""
import re
import subprocess
import types
from pathlib import Path

import pytest

from muster import cleanup, config, core, decisions, hold, runs

GOLDEN = Path(__file__).parent / "golden"
ROOT = Path(__file__).resolve().parent.parent
NASTY = 'a {x} ${y} $z `tick` "dq" \'sq\'\nnext line \\n {{}}'
HERMES = "/opt/he$rmes/{bin}/hermes"


@pytest.fixture
def nasty(sandbox, monkeypatch):
    monkeypatch.setattr(config, "hermes_bin", lambda: HERMES)
    monkeypatch.setitem(config.settings, "gh_config_dir", "/gh/{cfg}/$x")
    notes = sandbox / "notes"
    notes.mkdir()
    (notes / "Radical-Candor-LLC__radicalcandorai.md").write_text(f"\n  Merging deploys. {NASTY}\n\n")
    monkeypatch.setitem(config.settings, "notes_dir", str(notes))


def render(monkeypatch):
    """name → the text muster produces, for each prompt and its variants."""
    out = {}
    repo, other = "Radical-Candor-LLC/radicalcandorai", "Radical-Candor-LLC/RC-Data-Tools"
    auto = {"login": "bot{x}`$z`", "id": 7, "label": 'auto"q"'}
    out["issue-bug-human-bot"] = core.brief(repo, 12, True, base="main")
    out["issue-feature-auto-nonote"] = core.brief(other, 3, False, base="dev{x}", auto=auto)
    monkeypatch.setitem(config.settings, "gh_config_dir", "")
    out["issue-feature-human-nobot"] = core.brief(repo, 4, False)
    out["launch-envelope"] = core.startup_prompt(NASTY + core.reuse_note({"commits": 2, "uncommitted": "3{x}"}))
    run = {"card": "t_{c}$d", "repo": "o/r`x`", "branch": "feat/a{b}", "base": "ma$in"}
    out["ad-hoc-footer"] = runs.footer(run)
    req = {"origin": None, "id": "r{1}$q", "cycle": 2, "pr": "https://github.com/o/r/pull/1?$a={b}"}
    out["revision-issue"] = decisions.wire_text(req, {"kind": "issue"}, NASTY)
    out["revision-ad-hoc"] = decisions.wire_text({**req, "origin": "o`r`"}, {"kind": "adhoc"}, NASTY)
    replies = []
    monkeypatch.setattr(decisions, "transition", lambda rid, frm, to, **kw: replies.append(kw) or ({}, False))
    proposal = {"version": 3, "sha": "abc{d}$e"}
    decisions._design({"id": "d1", "proposal": proposal, "answer": {"action": "approve"}}, "boot")
    decisions._design({"id": "d2", "proposal": proposal, "answer": {"action": "send-back"}, "feedback": NASTY}, "boot")
    decisions._design({"id": "d3", "proposal": proposal, "answer": {"text": "more `x` {y}"}, "feedback": NASTY}, "boot")
    out["design-approve"], out["design-revise"], out["design-revise-typed"] = (r["intent"]["reply"] for r in replies)
    rec = {"repo": repo, "issue": 9, "candidates": [{"issue": 5, "card": "t_5"}], "reason": NASTY,
           "card": "t_{h}", "event": "e$1"}
    out["duplicate-handoff"] = hold.wire(rec)
    calls = []

    def complete_structured(**kw):
        calls.append(kw)
        return types.SimpleNamespace(parsed={"duplicate_of": None, "reason": "no"})

    monkeypatch.setattr(hold, "CTX", types.SimpleNamespace(llm=types.SimpleNamespace(complete_structured=complete_structured)))
    hold.judge(repo, {"number": 9, "title": NASTY, "body": NASTY}, [])
    out["duplicate-classifier"] = calls[0]["instructions"]
    out["duplicate-classifier-input"] = calls[0]["input"][0]["text"]
    out["design-review-cue"] = core.REVIEW_CUE
    out["cleanup-warning-cue"] = cleanup.ASK_LINE
    return out


def test_prompts_match_pre_refactor_output(nasty, monkeypatch):
    out = render(monkeypatch)
    assert sorted(out) == sorted(p.stem for p in GOLDEN.glob("*.txt"))
    for name, text in out.items():
        assert text == (GOLDEN / f"{name}.txt").read_text(), name


def test_every_prompt_the_code_names_ships_in_git_and_none_is_orphaned():
    """Installs are git checkouts: a prompt file that is not tracked is missing from every install."""
    tracked = subprocess.run(["git", "ls-files", "prompts"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
    source = "".join(p.read_text() for p in [ROOT / "__init__.py", *(ROOT / "muster").glob("*.py")])
    named = set(re.findall(r'config\.prompt\("([\w-]+)"', source)) | {"dispatch", "escalation", "builder-workflow"}
    assert {f"prompts/{n}.md" for n in named} | {"prompts/README.md"} == set(tracked)


def test_the_workflow_override_replaces_the_bundled_workflow_in_the_brief(tmp_path, monkeypatch):
    custom = tmp_path / "w.md"
    custom.write_text("\n  My own ${rules} {here}\n")
    monkeypatch.setitem(config.settings, "workflow_prompt_file", str(custom))
    text = core.brief("o/r", 1, False)
    assert "## How to work\n\nMy own ${rules} {here}\n\n## Production" in text
    assert (config.PROMPTS / "builder-workflow.md").read_text().splitlines()[2] not in text


def test_a_placeholder_without_a_value_raises():
    with pytest.raises(KeyError):
        config.prompt("design-reply-approve")
