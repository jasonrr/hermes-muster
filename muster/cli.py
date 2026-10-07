import importlib
import sys

from . import config

SUBCOMMANDS = {  # name → (sibling module, function); resolved relative to this package
    "tick": ("core", "tick"),
    "recover": ("core", "recover"),
    "launch": ("runs", "launch"),
    "flush": ("runs", "flush"),
    "hook": ("events", "hook"),
    "cleanup": ("cleanup", "cleanup"),
    "open": ("cleanup", "open_workspace"),
}


def setup(parser):
    sub = parser.add_subparsers(dest="muster_command", required=True)
    sub.add_parser("tick", help="poll the label and launch new work").add_argument("--dry-run", action="store_true")
    p = sub.add_parser("recover", help="resume or resend a launch")
    p.add_argument("card")
    p.add_argument("--resend", action="store_true")
    p.add_argument("--adopt", action="store_true")
    p = sub.add_parser("launch", help="ad-hoc coding run (no issue)")
    for flag in ("--cwd", "--branch", "--title", "--brief"):
        p.add_argument(flag, required=True)
    p.add_argument("--base", default=None, help="default: the repos entry's @base, else origin's default branch")
    p.add_argument("--model", default=None)
    sub.add_parser("flush", help="deliver hook events a pane could not")
    p = sub.add_parser("hook", help="agent hook entry point; reads the hook payload on stdin")
    p.add_argument("event", choices=["notification", "prompt", "session-end", "stop", "done"])
    p.add_argument("url", nargs="?")
    p.add_argument("--card", default=None)
    sub.add_parser("cleanup", help="close finished workspaces").add_argument("--dry-run", action="store_true")
    p = sub.add_parser("open", help="open an analysis workspace muster owns")
    p.add_argument("--cwd", required=True)
    p.add_argument("--label", required=True)


def main(args, ctx):
    is_hook = args.muster_command == "hook"
    failed = 1 if is_hook and args.event == "done" else 0  # `done` is run by the pane agent, which must see a failure
    try:
        config.load(ctx)
        module, fn = SUBCOMMANDS[args.muster_command]
        return getattr(importlib.import_module(f"{__package__}.{module}"), fn)(args) or 0
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return failed if is_hook else 2  # a hook never fails the agent
    except Exception as e:  # noqa: BLE001 — same rule; anything else is a bug, logged not raised
        if not is_hook:
            raise
        print(f"muster hook: {e}", file=sys.stderr)
        return failed
