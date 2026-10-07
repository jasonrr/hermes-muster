import os
import re
import shutil
from pathlib import Path

DEFAULTS = {
    "label": "agent-ready",  # the approving label
    "bug_label": "bug",  # issues with it are briefed as bugs
    "approver_login": "",  # REQUIRED: GitHub login whose label event authorizes work (compared case-insensitively)
    "approver_id": 0,  # REQUIRED: that account's numeric id (login can be renamed; id cannot)
    "repos": [],  # REQUIRED: ["owner/name", "owner/name=/abs/clone/path", ...]
    "clone_root": "~/Code",  # clone = clone_root/<name> when no =path given
    "board": "muster",  # hermes kanban board slug; give muster its own board (idempotency keys are per board)
    "agent_kind": "claude",  # v1: claude only
    "agent_model": "opus",  # passed to the agent CLI
    "branch_prefix": "muster/",  # issue branch = f"{branch_prefix}{number}"
    "notify_platform": "telegram",
    "notify_chat_id": "",  # "" → DM: TELEGRAM_HOME_CHANNEL from $HERMES_HOME/.env
    "notify_user_id": "",
    "notify_chat_type": "group",
    "gh_config_dir": "",  # "" → your own gh login; else GH_CONFIG_DIR for gh/git in the pane, GH_TOKEN/GITHUB_TOKEN blanked
    "notes_dir": "",  # "" → data_dir()/"repos"
    "workflow_prompt_file": "",  # "" → <plugin>/prompts/workflow.md
    "worktrees": "~/.herdr/worktrees",
}
settings = dict(DEFAULTS)  # module-level; cli.main fills it; tests assign into it
SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(Exception):
    pass


def load(ctx):
    for key, default in DEFAULTS.items():
        settings[key] = ctx.get_config(key, default)


def require():
    missing = [k for k in ("approver_login", "repos") if not settings[k]]
    try:
        if int(settings["approver_id"]) <= 0:
            missing.append("approver_id")
    except (TypeError, ValueError):
        missing.append("approver_id")
    if not isinstance(settings["branch_prefix"], str) or not settings["branch_prefix"]:
        missing.append("branch_prefix")
    if missing:
        raise ConfigError(f"muster: set plugins.entries.muster.settings.{{{', '.join(missing)}}} in config.yaml")
    repos()  # raises on a malformed slug
    wf = workflow_path()
    if not wf.is_file() or not wf.read_text().strip():
        raise ConfigError(f"muster: workflow prompt file missing or empty: {wf}")
    if not shutil.which("hermes") and not (Path.home() / ".local/bin/hermes").exists():
        raise ConfigError("muster: cannot find the hermes executable on PATH or in ~/.local/bin")


def data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir("muster")
    except ImportError:
        p = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "plugin-data" / "muster"
        p.mkdir(parents=True, exist_ok=True)
        return p


def repos() -> dict[str, tuple[Path, str]]:
    out = {}
    for item in settings["repos"]:
        slug, _, path = str(item).partition("=")
        if not SLUG.match(slug):
            raise ConfigError(f"muster: repos entry {item!r} is not owner/name[=path]")
        name = slug.split("/", 1)[1]
        out[slug] = (Path(path).expanduser() if path else Path(settings["clone_root"]).expanduser() / name, name)
    return out


def notes_dir() -> Path:
    return Path(settings["notes_dir"]).expanduser() if settings["notes_dir"] else data_dir() / "repos"


def workflow_path() -> Path:
    if settings["workflow_prompt_file"]:
        return Path(settings["workflow_prompt_file"]).expanduser()
    return Path(__file__).resolve().parent.parent / "prompts" / "workflow.md"


def workflow_text() -> str:
    return workflow_path().read_text().strip()


def hermes_bin() -> str:  # call AFTER core.prepare_env() so PATH is extended
    return shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
