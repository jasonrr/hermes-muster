import shlex
import sys
from pathlib import Path

from . import config

KIND = "claude"
# Claude Code notification_type values that mean a dialog waits on a person. Not idle_prompt: Claude
# sends it while background work runs and after the job is done (F11).
ASK_NOTIFICATIONS = "permission_prompt|elicitation_dialog|elicitation_url_dialog|worker_permission_prompt"


def hook_settings(hook_cmd: list[str]) -> dict:
    """Claude Code settings.json: every hook runs `<hook_cmd> <event>`. Same eight hooks for issue and ad-hoc runs."""
    def hook(event, matcher=None):
        # A short timeout: a hook waits on hermes, git and gh, and a killed one's saved event is delivered by the flush.
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
        # Waits for the channel's answer to a dialog, up to a day (bridge.wait). Claude Code reads exit 2 as a
        # deny, and argparse exits 2 on a usage error, so any failure exits 0 and leaves the dialog to decide.
        "PermissionRequest": [{"hooks": [{"type": "command", "timeout": 86400, "command":
            " ".join(shlex.quote(a) for a in [*hook_cmd, "permission"]) + " || exit 0"}]}],
    }}


def launch_args(model: str, settings_path: Path) -> list[str]:
    return ["--model", model, "--permission-mode", "auto", "--settings", str(settings_path)]


def is_ours(agent: dict, name: str, cwd: Path) -> bool:
    """The `herdr agent get` json names this kind, this name, this cwd."""
    if agent.get("agent") != KIND or agent.get("name") != name:
        return False
    where = agent.get("foreground_cwd") or agent.get("cwd") or ""
    return where == str(cwd) or where.startswith(str(cwd) + "/")


def ignore(event: str, payload: dict) -> bool:
    """A session-end with reason 'clear' is not an end; the agent keeps working."""
    return event == "session-end" and payload.get("reason") == "clear"


def detail(payload: dict) -> str:
    """The message, or an AskUserQuestion's first question; whitespace-normalized."""
    try:
        asked = payload["tool_input"]["questions"][0]["question"]
    except (KeyError, IndexError, TypeError):
        asked = ""
    return " ".join(str(payload.get("message") or asked).split())


APPROVAL = "Approval"  # the question header that marks a design-approval request


def ask(payload: dict) -> list | None:
    """An AskUserQuestion's questions (each a dict: question, options, ...), as the agent wrote them."""
    if payload.get("tool_name") != "AskUserQuestion":
        return None
    tool_input = payload.get("tool_input")
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list):
        return None
    return [q for q in questions if isinstance(q, dict)] or None


def approval(payload: dict) -> bool:
    """An AskUserQuestion the agent marked as a design-approval request: a question headed `Approval`."""
    return any(q.get("header") == APPROVAL for q in ask(payload) or [])


def get_adapter(kind: str):
    if kind != KIND:
        raise config.ConfigError(f"agent_kind {kind!r}: muster v1 supports claude only")
    return sys.modules[__name__]
