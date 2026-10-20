"""The install gate's verdicts from canned pytest reports and configs. Ordinary suite: no Hermes needed (configs are
JSON, which is YAML, loaded with json.loads here and with Hermes's loader in the gate)."""

import json

import pytest

from tests.acceptance import gate

CASE = '<testcase classname="tests.acceptance.test_lifecycle" name="{name}">{body}</testcase>'
GOOD = {"display": {"tool_progress": "all", "platforms": {"slack": {"cleanup_progress": True}}},
        "plugins": {"entries": {"muster": {"settings": {"notify_platform": "slack", "notify_topics": True,
                                                         "slack_bot_token": "xoxb-SECRET"}}}}}


def junit(tmp_path, *bodies):
    cases = "".join(CASE.format(name=f"t{i}", body=b) for i, b in enumerate(bodies))
    path = tmp_path / "junit.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>')
    return path


@pytest.mark.parametrize("bodies, report, want", [
    (["", ""], "", "PASS"),
    (["", '<failure message="B: stale ping"/>'], "", "FAIL"),
    (['<error message="setup"/>'], "", "FAIL"),
    (['<skipped type="pytest.skip" message="HERMES_AGENT_DIR unset"/>'], "", "BLOCKED"),
    (['<skipped type="pytest.xfail" message="cleanup_progress skips wake turns"/>'], "", "BLOCKED"),
    ([""], "XPASS tests/acceptance/test_lifecycle.py::t0 - a named Hermes gap", "BLOCKED"),
    (['<failure message="x"/>', '<skipped type="pytest.skip" message="y"/>'], "", "FAIL"),
])
def test_the_verdict_from_the_report(tmp_path, bodies, report, want):
    failed, blocked = gate.tests(junit(tmp_path, *bodies), report)
    assert gate.verdict(failed, blocked, gate.mismatches(json.dumps(GOOD), json.loads))[0] == want


def test_a_skip_names_its_reason():
    assert gate.verdict([], ["t0: skipped: HERMES_AGENT_DIR unset"], [])[1] == ["t0: skipped: HERMES_AGENT_DIR unset"]


def test_production_config_is_compared_by_key_name_only():
    assert gate.mismatches(json.dumps(GOOD), json.loads) == []
    off = json.loads(json.dumps(GOOD))
    off["display"]["platforms"]["slack"]["cleanup_progress"] = False
    off["plugins"]["entries"]["muster"]["settings"]["notify_topics"] = False
    assert gate.mismatches(json.dumps(off), json.loads) == [
        "display.platforms.slack", "plugins.entries.muster.settings.notify_topics"]
    assert gate.verdict([], [], ["display.platforms.slack"]) == ("CONFIG MISMATCH", ["display.platforms.slack"])


def test_an_unparseable_config_says_so_without_its_text():
    assert gate.mismatches("slack_bot_token: xoxb-SECRET\n{{{", json.loads) == ["config: unreadable"]


def test_the_gate_never_prints_a_secret(tmp_path, monkeypatch, capsys):
    """The whole main(): the suite's run is canned, the production config holds a token."""
    home = tmp_path / "prod"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps(GOOD))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AGENT_DIR", str(tmp_path))
    monkeypatch.setitem(__import__("sys").modules, "hermes_yaml", type("Y", (), {"safe_load": staticmethod(json.loads)}))

    def run(argv, **kwargs):
        junit(tmp_path / "out", '<failure message="xoxb-SECRET in a log"/>').replace(
            next(a for a in argv if a.startswith("--junitxml=")).split("=", 1)[1])
        return type("Done", (), {"stdout": "", "returncode": 1})()

    (tmp_path / "out").mkdir()
    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.main([]) == 1
    out = capsys.readouterr()
    assert out.out.startswith("FAIL") and "SECRET" not in out.out + out.err


def test_without_the_hermes_checkout_the_gate_is_blocked(monkeypatch, capsys):
    monkeypatch.delenv("HERMES_AGENT_DIR", raising=False)
    assert gate.main([]) == 2
    assert capsys.readouterr().out == "BLOCKED: HERMES_AGENT_DIR unset\n"
