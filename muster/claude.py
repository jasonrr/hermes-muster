import shlex
import sys
from pathlib import Path

from . import config

KIND = "claude"
# Claude Code notification_type values that mean a dialog waits on a person. Not idle_prompt: Claude
# sends it while background work runs and after the job is done (F11).
ASK_NOTIFICATIONS = "permission_prompt|elicitation_dialog|elicitation_url_dialog|worker_permission_prompt"


def hook_settings(hook_cmd: list[str]) -> dict:
    """Claude Code settings.json: every hook runs `<hook_cmd> <event>`. Same seven hooks for issue and ad-hoc runs."""
    def hook(event, matcher=None):
        # A short timeout: a hook waits on hermes, git and gh, and a killed one is retried by the flush.
        h = {"hooks": [{"type": "command", "command": " ".join(shlex.quote(a) for a in [*hook_cmd, event]), "timeout": 30}]}
        if matcher:
            h["matcher"] = matcher
        return [h]
    # PostToolUseFailure too: a rejected AskUserQuestion gets no PostToolUse, and its wait card
    # would otherwise stay open and swallow the corrected question.
    return {"hooks": {
        "Notification": hook("notification", ASK_NOTIFICATIONS),
        "PreToolUse": hook("notification", "AskUserQuestion"),
        "UserPromptSubmit": hook("prompt"), "PostToolUse": hook("prompt"), "PostToolUseFailure": hook("prompt"),
        "SessionEnd": hook("session-end"), "Stop": hook("stop"),
    }}


def launch_args(model: str, settings_path: Path) -> list[str]:
    return ["--model", model, "--permission-mode", "auto", "--settings", str(settings_path)]


def is_ours(agent: dict, name: str, cwd: Path) -> bool:
    """Port SRC check_agent (rc_intake.py:575-583): the `herdr agent get` json names this kind, this name, this cwd."""
    if agent.get("agent") != KIND or agent.get("name") != name:
        return False
    where = agent.get("foreground_cwd") or agent.get("cwd") or ""
    return where == str(cwd) or where.startswith(str(cwd) + "/")


def ignore(event: str, payload: dict) -> bool:
    """SRC rc_event.py:172: a session-end with reason 'clear' is not an end; the agent keeps working."""
    return event == "session-end" and payload.get("reason") == "clear"


def detail(payload: dict) -> str:
    """SRC rc_event.py:206-210: the message, or an AskUserQuestion's first question; whitespace-normalized."""
    try:
        asked = payload["tool_input"]["questions"][0]["question"]
    except (KeyError, IndexError, TypeError):
        asked = ""
    return " ".join(str(payload.get("message") or asked).split())


def get_adapter(kind: str):
    if kind != KIND:
        raise config.ConfigError(f"agent_kind {kind!r}: muster v1 supports claude only")
    return sys.modules[__name__]
