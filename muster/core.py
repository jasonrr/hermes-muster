"""core: the plumbing every muster module shares. Names of the files muster writes, the data paths, the one
subprocess seam (run, which tests replace) and kanban(), the environment, the board check, the chat that
muster pings and the notify+wake subscription, and save_json. No route logic lives here: the issue route is
muster.intake, the ad-hoc route muster.adhoc, the launch engine both use muster.launcher, and every run's
event delivery muster.runs.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import claude, config

# Names every module shares: the card file, the brief copy, the wait kind and the links comment.
CARD_FILE = "muster-card.json"
BRIEF_FILE = "muster-brief.md"
SETTINGS_FILE = "muster-settings.json"
CREATED_BY = "muster"
WAIT_KIND = "muster-wait"
PIN_FILE = "muster-pin"  # the proposal an approval request carried, for the bridge (bridge.write_pin)
LINKS_PREFIX = "muster links:"
# Hermes wakes the coordinator with a generic "[kanban] Task ... completed" turn whose only card-specific text is
# this summary's first line (gateway/kanban_watchers_notifier.py _fmt_completed): naming the skill there is what
# makes the woken agent review and record a recommendation instead of answering in prose.
REVIEW_CUE = config.prompt("design-review-cue")
PROVENANCE = "Made by muster (hermes muster). The card id and pane are the provenance; see the card comments."

_adapter = None


def adapter():
    return _adapter or claude.get_adapter(config.settings["agent_kind"])


def hook_cmd():
    return [config.hermes_bin(), "muster", "hook"]


def intake_dir():
    """One directory per intake card: launch.json (the launch record), launch.lock, prompt-seen.jsonl."""
    return config.data_dir() / "intake"


def worktrees_dir():
    """herdr's own worktree layout (<worktrees.directory>/<repo>/<branch-slug>); passed as --path so a
    launch knows its checkout before it exists."""
    return Path(config.settings["worktrees"]).expanduser()


def log_path(name):
    return config.data_dir() / "logs" / f"{name}.log"


def log(name, line):
    """One timestamped line, whitespace folded, in <data dir>/logs/<name>.log."""
    path = log_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as out:
        out.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {' '.join(str(line).split())}\n")


def lock_path(name):
    """<data dir>/logs/<name>.lock: one tick, one cleanup at a time."""
    return config.data_dir() / "logs" / f"{name}.lock"


def hermes_home():
    return Path(os.environ["HERMES_HOME"])


def board_db():
    """Where hermes keeps the board. Inside hermes, hermes says (profiles share one root; HERMES_KANBAN_DB pins it).
    Outside (tests): <kanban home>/kanban.db for `default`, else <kanban home>/kanban/boards/<board>/."""
    board = config.settings["board"]
    try:
        from hermes_cli.kanban_db import kanban_db_path
    except ImportError:
        pass
    else:
        return kanban_db_path(board)
    root = Path(os.environ.get("HERMES_KANBAN_HOME", "").strip() or hermes_home()).expanduser()
    return root / "kanban.db" if board == "default" else root / "kanban" / "boards" / board / "kanban.db"


SECRET = re.compile(r"\b(?:gh[pousr]_\w+|github_pat_\w+)")  # GitHub token shapes


class CommandError(Exception):
    """A command exited non-zero; the message names the command and its exit code."""


class LaunchError(Exception):
    """A launch precondition or readback failed; the message says which."""


def run(argv, env=None):
    """Run one command and return its stdout. The only subprocess seam; tests replace it.
    `env` replaces the environment when given (the human's own gh login for a merge)."""
    # cron starts a tick every minute; a hung command must not let ticks pile up.
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False, env=env)
    except subprocess.TimeoutExpired:
        raise CommandError(SECRET.sub("[redacted]", f"{' '.join(argv[:3])}: no answer in 300 s")) from None
    if result.returncode != 0:
        # Redacted here, where every CommandError is made: its text reaches card bodies and logs.
        raise CommandError(SECRET.sub("[redacted]", f"{' '.join(argv[:3])}: exit {result.returncode}\n"
                                                    f"{result.stderr.strip()}"))
    return result.stdout


def kanban(*args):
    return run(["hermes", "kanban", "--board", config.settings["board"], *args])


def prepare_env():
    """Point every hermes call at the configured home, and find hermes and herdr under cron's bare PATH."""
    os.environ["HERMES_HOME"] = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    os.environ["PATH"] = f"{Path.home() / '.local/bin'}:/opt/homebrew/bin:{os.environ.get('PATH', '')}"


def board_exists():
    """The kanban board must exist before the first tick: a missing board is a CommandError on every call."""
    board = config.settings["board"]
    try:
        boards = [b.get("slug") for b in json.loads(run(["hermes", "kanban", "boards", "list", "--json"]))]
    except (CommandError, ValueError, TypeError, AttributeError) as error:
        raise config.ConfigError(f"muster: cannot list kanban boards: {' '.join(str(error).split())}") from None
    if board not in boards:
        raise config.ConfigError(f"muster: kanban board {board!r} does not exist; "
                                 f"create it with `hermes kanban boards create`")


def telegram_home():
    """The human's Telegram DM chat id from $HERMES_HOME/.env. Only this one key is read; no value is ever printed."""
    for line in (hermes_home() / ".env").read_text().splitlines():
        if line.startswith("TELEGRAM_HOME_CHANNEL="):
            value = line.split("=", 1)[1].strip().strip("\"'")
            if value:
                return value
    raise LaunchError("TELEGRAM_HOME_CHANNEL is not set in $HERMES_HOME/.env")


def approver_entries(value):
    """notify_user_id's entries, stripped: one id, ids separated by commas ("42,43"), an int or a YAML list. A blank
    entry is kept, so validation can refuse it."""
    items = value if isinstance(value, list) else [] if value in (None, "") else str(value).split(",")
    return [str(v).strip() for v in items]


def approvers():
    """Who may answer a muster prompt: the configured ids in a chat (a group with no notify_user_id authorizes
    nobody); in the DM fallback, the DM's human, as only they are in it."""
    if not config.settings["notify_chat_id"]:
        return [notify_target()["user_id"]]
    return [i for i in approver_entries(config.settings["notify_user_id"]) if i]


def notify_target():
    """The notify target: the configured chat, or the channel's home (Telegram: the human's DM) when notify_chat_id
    is empty."""
    s = config.settings
    if s["notify_chat_id"]:
        # YAML parses an unquoted Telegram id as an int; argv needs strings.
        return {"chat_id": str(s["notify_chat_id"]), "user_id": ([*approvers(), str(s["notify_chat_id"])])[0],
                "chat_type": s["notify_chat_type"]}
    from . import channels  # channels imports config only; its modules import core

    chat = channels.get(s["notify_platform"]).home()  # Telegram: the human's DM; Slack has none and raises
    return {"chat_id": chat, "user_id": chat, "chat_type": "dm"}


def subscribe(card, ledger=None, mode="notify+wake"):
    """notify+wake: the gateway pings the human, then queues a fresh agent turn, for every card event (`wake`: the
    turn only, no ping). With the
    run's ledger, in the run's topic when it has one: Hermes's notifier sends the ping and runs the wake in the
    subscription's thread, so the coordinator's commentary lands there too."""
    from . import conversation  # conversation imports core

    target = conversation.target(ledger) if ledger else {**notify_target(), "thread_id": None}
    thread = target.pop("thread_id")
    kanban("notify-subscribe", card, "--platform", config.settings["notify_platform"], "--chat-id", target["chat_id"],
           *(["--thread-id", thread] if thread else []), "--user-id", target["user_id"], "--chat-type",
           target["chat_type"], "--notifier-profile", "default", "--delivery-mode", mode)
    subs = json.loads(kanban("notify-list", card, "--json"))
    want = {**target, **({"thread_id": thread} if thread else {}), "notifier_profile": "default",
            "delivery_mode": mode}
    if not any(all(str(s.get(k)) == str(v) for k, v in want.items()) for s in subs):
        raise LaunchError(f"the {mode} subscription did not read back")
    if ledger:
        conversation.add_card(ledger, card)


def switch_mode(card, old, new):
    """Each of the card's `old` subscriptions in `new` delivery mode, in place: Hermes keeps one row per (card, platform,
    chat, thread), and a notify-subscribe naming only that key and the mode changes only the mode."""
    for sub in json.loads(kanban("notify-list", card, "--json")):
        if sub.get("delivery_mode") == old:
            kanban("notify-subscribe", card, "--platform", sub["platform"], "--chat-id", str(sub["chat_id"]),
                   *(["--thread-id", str(sub["thread_id"])] if sub.get("thread_id") else []), "--delivery-mode", new)


def gh_config_dir():
    """The bot's gh config dir, absolute: herdr --env is not shell-expanded."""
    return Path(config.settings["gh_config_dir"]).expanduser()


def save_json(path, data):
    """Write whole or not at all: a crash mid-write leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as out:
        out.write(json.dumps(data))
        out.flush()
        os.fsync(out.fileno())  # else a power cut after the rename can leave an empty file
    os.replace(tmp, path)
