import importlib
import sys

from . import config

AGENT_RUN = ("done", "propose", "duplicate")  # hook events the pane agent runs from its shell
SUBCOMMANDS = {  # name → (sibling module, function); resolved relative to this package
    "tick": ("intake", "tick"),
    "recover": ("intake", "recover"),
    "launch": ("adhoc", "launch"),
    "flush": ("runs", "flush"),
    "hook": ("events", "hook"),
    "cleanup": ("cleanup", "cleanup"),
    "open": ("cleanup", "open_workspace"),
    "recommend": ("decisions", "recommend"),
    "recommend-design": ("decisions", "recommend_design"),
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
    p.add_argument("event", choices=["notification", "prompt", "session-end", "stop", "done", "propose", "permission",
                                       "duplicate"])
    p.add_argument("url", nargs="?", help="done: the pull request URL; propose: the design file; "
                                          "duplicate: the held report's issue number")
    p.add_argument("verdict", nargs="?", choices=["same", "different"], help="duplicate: your evaluation")
    p.add_argument("--card", default=None)
    sub.add_parser("cleanup", help="close finished workspaces").add_argument("--dry-run", action="store_true")
    p = sub.add_parser("recommend", help="record the coordinator's recommendation on a finished build")
    p.add_argument("ledger")
    p.add_argument("--pr", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--choice", required=True, choices=["merge", "send-back", "nothing"])
    p.add_argument("--review", required=True, help="file with the review text")
    p.add_argument("--feedback", default=None, help="file with the feedback to send the agent (needed for send-back)")
    p.add_argument("--summary", default=None, help="file with the card's short summary; the review stays whole above it")
    p = sub.add_parser("recommend-design", help="record the coordinator's review of one proposal version")
    p.add_argument("ledger")
    p.add_argument("--proposal", required=True, help="v<n>:<sha>, as in the design review card's heading")
    p.add_argument("--choice", required=True, choices=["approve", "revise", "judgment"])
    p.add_argument("--review", required=True, help="file with the review text")
    p.add_argument("--feedback", default=None, help="file with the revisions Send back gives the agent (needed for revise)")
    p.add_argument("--summary", default=None, help="file with the card's short summary; the review stays whole above it")
    p = sub.add_parser("open", help="open an analysis workspace muster owns")
    p.add_argument("--cwd", required=True)
    p.add_argument("--label", required=True)


def main(args, ctx):
    is_hook = args.muster_command == "hook"
    # `done`, `propose` and `duplicate` are run by the pane agent, which must see a failure.
    failed = 1 if is_hook and args.event in AGENT_RUN else 0
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
