"""The install gate's verdicts from canned pytest reports and configs. Ordinary suite: no Hermes needed (configs are
JSON, which is YAML, loaded with json.loads here and with Hermes's loader in the gate)."""

import json
import subprocess
import sys

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


def test_a_run_without_lifecycle_tests_is_not_a_pass(tmp_path):
    empty = tmp_path / "empty.xml"
    empty.write_text('<testsuites><testsuite name="pytest" tests="1"><testcase classname="tests.acceptance.test_gate" '
                     'name="t"/></testsuite></testsuites>')
    assert gate.verdict(*gate.tests(empty, ""), []) == ("FAIL", ["failed: no lifecycle test ran"])


CLEAN = ["", ""]  # passing lifecycle cases, no failure, error or skip element


@pytest.mark.parametrize("code", [2, 3])  # interrupted; pytest internal error
def test_a_nonzero_pytest_exit_is_never_a_pass(tmp_path, code):
    failed, blocked = gate.tests(junit(tmp_path, *CLEAN), "")
    got = gate.verdict(failed, blocked, gate.mismatches(json.dumps(GOOD), json.loads), code)
    assert got == ("FAIL", [f"failed: pytest exited {code} with no failed test in its report"])


def test_a_nonzero_exit_keeps_the_report_diagnostics(tmp_path):
    report = junit(tmp_path, "", '<failure message="B: stale ping"/>')
    assert gate.verdict(*gate.tests(report, ""), [], 1) == (
        "FAIL", ["failed: tests.acceptance.test_lifecycle::t1: B: stale ping"])
    skipped = junit(tmp_path, "", '<skipped type="pytest.skip" message="HERMES_AGENT_DIR unset"/>')
    assert gate.verdict(*gate.tests(skipped, ""), [], 2) == (
        "FAIL", ["failed: pytest exited 2 with no failed test in its report",
                 "tests.acceptance.test_lifecycle::t1: skipped: HERMES_AGENT_DIR unset"])


def test_a_real_interrupted_run_is_not_a_pass(tmp_path):
    """The reproduction: one passing lifecycle test, then a KeyboardInterrupt. pytest exits 2 and its JUnit report
    has a passing case and no failure, error or skip element."""
    (tmp_path / "test_lifecycle.py").write_text("def test_ok():\n    pass\n\n\n"
                                                "def test_interrupted():\n    raise KeyboardInterrupt\n")
    report = tmp_path / "j.xml"
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", f"--junitxml={report}",
                           "test_lifecycle.py"], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert done.returncode == 2 and "<failure" not in report.read_text() and 'name="test_ok"' in report.read_text()
    failed, blocked = gate.tests(report, done.stdout)
    assert gate.verdict(failed, blocked, gate.mismatches(json.dumps(GOOD), json.loads), done.returncode)[0] == "FAIL"


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
    assert "failed: tests.acceptance.test_lifecycle::t0: [redacted] in a log" in out.out  # why, not just which


@pytest.mark.parametrize("code", [2, 3])
def test_main_fails_a_clean_report_from_a_failed_pytest(tmp_path, monkeypatch, capsys, code):
    home = tmp_path / "prod"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps(GOOD))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AGENT_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "hermes_yaml", type("Y", (), {"safe_load": staticmethod(json.loads)}))
    (tmp_path / "out").mkdir()

    def run(argv, **kwargs):
        junit(tmp_path / "out", *CLEAN).replace(next(a for a in argv if a.startswith("--junitxml=")).split("=", 1)[1])
        return type("Done", (), {"stdout": "", "returncode": code})()

    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.main([]) == 1
    assert capsys.readouterr().out.startswith("FAIL\n  failed: pytest exited")


def test_all_passing_with_matching_config_is_the_one_pass(tmp_path, monkeypatch, capsys):
    home = tmp_path / "prod"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps(GOOD))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AGENT_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "hermes_yaml", type("Y", (), {"safe_load": staticmethod(json.loads)}))
    (tmp_path / "out").mkdir()

    def run(argv, **kwargs):
        junit(tmp_path / "out", *CLEAN).replace(next(a for a in argv if a.startswith("--junitxml=")).split("=", 1)[1])
        return type("Done", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.main([]) == 0 and capsys.readouterr().out == "PASS\n"


def test_without_the_hermes_checkout_the_gate_is_blocked(monkeypatch, capsys):
    monkeypatch.delenv("HERMES_AGENT_DIR", raising=False)
    assert gate.main([]) == 2
    assert capsys.readouterr().out == "BLOCKED: HERMES_AGENT_DIR unset\n"
