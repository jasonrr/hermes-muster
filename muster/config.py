import os
import re
import shutil
import subprocess
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
    approver = settings["approver_id"]  # an int: a quoted YAML id or a float is a typo, not an id
    if not isinstance(approver, int) or isinstance(approver, bool) or approver <= 0:
        missing.append("approver_id")
    missing += [k for k in ("label", "board", "branch_prefix")
                if not isinstance(settings[k], str) or not settings[k].strip()]
    if missing:
        raise ConfigError(f"muster: set plugins.entries.muster.settings.{{{', '.join(missing)}}} in config.yaml")
    from . import claude

    claude.get_adapter(settings["agent_kind"])  # raises ConfigError: fail before any worktree or card exists
    try:
        valid = subprocess.run(["git", "check-ref-format", "--branch", f"{settings['branch_prefix']}1"],
                               capture_output=True).returncode == 0
    except OSError:
        raise ConfigError("muster: cannot run git to check branch_prefix") from None
    if not valid:
        raise ConfigError(f"muster: branch_prefix {settings['branch_prefix']!r} does not make a valid branch name")
    names = {}
    for slug, (clone, _, base) in repos().items():  # repos() raises on a malformed slug
        if base and subprocess.run(["git", "check-ref-format", "--branch", base], capture_output=True).returncode:
            raise ConfigError(f"muster: repos entry {slug}: @{base} is not a valid base branch name")
        # herdr lays worktrees out by the clone's directory name (core.worktree_path); lower: APFS ignores case.
        other = names.setdefault(clone.name.lower(), slug)
        if other != slug:
            raise ConfigError(f"repos {other} and {slug} share the clone directory name {clone.name}; "
                              f"give one a path with repos: owner/name=/other/dir")
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


def repos() -> dict[str, tuple[Path, str, str | None]]:
    """slug -> (clone, name, base branch or None when origin's default decides)."""
    out = {}
    for item in settings["repos"]:
        entry, at, base = str(item).rpartition("@")
        if not at or entry.endswith("/"):  # no suffix; "/@" starts a path segment (node_modules/@scope)
            entry, base = str(item), None
        elif not base:
            raise ConfigError(f"muster: repos entry {item!r} has an empty @base")
        slug, _, path = entry.partition("=")
        if not SLUG.match(slug):
            raise ConfigError(f"muster: repos entry {item!r} is not owner/name[=path][@base]")
        name = slug.split("/", 1)[1]
        out[slug] = (Path(path).expanduser() if path else Path(settings["clone_root"]).expanduser() / name, name, base)
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
