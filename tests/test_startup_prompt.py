"""Lossless startup briefs and the real Herdr argument-validation boundary."""

import json
import os
import shutil
import subprocess

import pytest
import muster.core as core


@pytest.mark.parametrize("text", [
    "first line\nsecond line\n",
    "tabs\tand\r\nWindows lines\r",
    "quotes: ' \" ` $() ; \\ and Unicode: → café",
    "".join(chr(i) for i in range(128)),
])
def test_startup_prompt_round_trips_without_literal_control_characters(text):
    prompt = core.startup_prompt(text)
    assert prompt.startswith("Execute the authorized task")
    assert not any(ord(c) < 32 or ord(c) == 127 for c in prompt)
    assert json.loads(prompt.split("Task brief (JSON): ", 1)[1]) == text


@pytest.mark.skipif(
    os.environ.get("HERDR_ARGUMENT_PROBE") != "1" or shutil.which("herdr") is None,
    reason="Opt-in real Herdr probe; no agent is launched or pane changed",
)
def test_real_herdr_accepts_the_two_phase_calls_and_still_rejects_a_raw_brief_as_an_agent_argument():
    """Why the brief is never a Claude argument: herdr refuses raw newlines there. The two calls the
    launch makes instead pass argument validation and stop at the (deliberately missing) target."""
    def code(*argv):
        result = subprocess.run(["herdr", *argv], capture_output=True, text=True, timeout=15)
        assert result.returncode == 1
        return json.loads(result.stdout or result.stderr)["error"]["code"]
    text = "Authorized task.\nPreserve quotes ' \" ` $() and tabs\t exactly.\n"
    start = ["agent", "start", "repro-encoding", "--kind", "claude", "--pane", "nonexistent-encoding-probe", "--"]
    assert code(*start, text) == "invalid_agent_argument"
    assert code(*start, "--model", "opus", "--permission-mode", "auto") == "agent_pane_not_found"
    assert code("agent", "prompt", "nonexistent-encoding-probe", core.startup_prompt(text),
                "--wait", "--until", "working", "--until", "blocked", "--timeout", "5000") == "agent_not_found"
