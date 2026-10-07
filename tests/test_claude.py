import shlex
from pathlib import Path

import pytest

from muster import claude, config


def test_seven_hooks():
    h = claude.hook_settings(["hermes", "muster", "hook"])["hooks"]
    assert set(h) == {"Notification", "PreToolUse", "UserPromptSubmit", "PostToolUse",
                      "PostToolUseFailure", "SessionEnd", "Stop"}


def test_notification_matcher_excludes_idle_prompt():
    h = claude.hook_settings(["x"])["hooks"]
    assert h["Notification"][0]["matcher"] == claude.ASK_NOTIFICATIONS
    assert "idle_prompt" not in claude.ASK_NOTIFICATIONS
    assert h["PreToolUse"][0]["matcher"] == "AskUserQuestion"


def test_commands_end_with_event_and_are_quoted():
    h = claude.hook_settings(["/opt/my dir/hermes", "muster", "hook"])["hooks"]
    expect = {"Notification": "notification", "PreToolUse": "notification", "UserPromptSubmit": "prompt",
              "PostToolUse": "prompt", "PostToolUseFailure": "prompt", "SessionEnd": "session-end",
              "Stop": "stop"}
    for key, event in expect.items():
        cmd = h[key][0]["hooks"][0]
        assert shlex.split(cmd["command"]) == ["/opt/my dir/hermes", "muster", "hook", event]
        assert cmd["timeout"] == 30


def test_launch_args():
    assert claude.launch_args("opus", Path("/w/s.json")) == [
        "--model", "opus", "--permission-mode", "auto", "--settings", "/w/s.json"]


def test_is_ours():
    cwd = Path("/w/repo")
    ok = {"agent": "claude", "name": "n", "foreground_cwd": "/w/repo/sub"}
    assert claude.is_ours(ok, "n", cwd)
    assert claude.is_ours({"agent": "claude", "name": "n", "cwd": "/w/repo"}, "n", cwd)
    assert not claude.is_ours({**ok, "agent": "codex"}, "n", cwd)
    assert not claude.is_ours({**ok, "name": "other"}, "n", cwd)
    assert not claude.is_ours({**ok, "foreground_cwd": "/w/repo2"}, "n", cwd)
    assert not claude.is_ours({"agent": "claude", "name": "n"}, "n", cwd)


def test_ignore():
    assert claude.ignore("session-end", {"reason": "clear"})
    assert not claude.ignore("session-end", {"reason": "logout"})
    assert not claude.ignore("stop", {"reason": "clear"})


def test_detail():
    assert claude.detail({"message": "  needs \n  you "}) == "needs you"
    q = {"tool_input": {"questions": [{"question": "Which  one?"}]}}
    assert claude.detail(q) == "Which one?"
    assert claude.detail({}) == ""


def test_get_adapter():
    assert claude.get_adapter("claude") is claude
    with pytest.raises(config.ConfigError, match="codex"):
        claude.get_adapter("codex")
