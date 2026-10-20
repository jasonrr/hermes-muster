"""The world the harness's children see for herdr, gh and git: the repo's stateful fake, served on a Unix socket.

The shim (shim/sitecustomize.py) sends each argv as one JSON connection; the reply is {"stdout": …} or {"error": …}.
Every call is journaled. An argv nothing here models is an error the test fails on (`unmodelled`), never a guess.
"""

import json
import socketserver
import tempfile
import threading
from pathlib import Path

from muster import core
from tests import fake_herdr

ORIGIN = "https://github.com/demo/demo.git"


class AcceptanceWorld(fake_herdr.World):
    def __init__(self, tmp_path, journal):
        super().__init__(tmp_path)
        self.journal, self.unmodelled = journal, []
        self.pr = None  # the run's pull request url, once the builder has "opened" it

    def head(self, where):
        wt = self.worktrees.get(where)
        return self.branches.get((wt["clone"], wt["branch"]), self.origin) if wt else self.origin

    def pr_record(self):
        branch = next((w["branch"] for w in self.worktrees.values()), "")
        sha = self.head(next(iter(self.worktrees), ""))
        return {"url": self.pr, "state": "OPEN", "headRefOid": sha, "headRefName": branch, "baseRefName": "main",
                "isDraft": False, "isCrossRepository": False, "mergeStateStatus": "CLEAN"}

    def dispatch(self, argv):
        if argv[:3] == ["gh", "pr", "list"]:  # launcher.py:156 (none yet), adhoc.py:53 (verify)
            return json.dumps([self.pr_record()] if self.pr else [])
        if argv[:3] == ["gh", "pr", "view"] and self.pr and argv[3] == self.pr:  # decisions.py:249, events.py:540
            fields = argv[argv.index("--json") + 1].split(",")
            return json.dumps({k: v for k, v in self.pr_record().items() if k in fields})
        if argv[:3] == ["gh", "pr", "merge"] and argv[3] == self.pr:  # decisions.py:770: recorded, nothing merges
            return ""
        if argv[:1] == ["git"] and argv[3:] == ["remote", "get-url", "origin"]:  # adhoc.py:237
            return ORIGIN + "\n"
        if argv[:1] == ["git"] and argv[3:] == ["rev-parse", "HEAD"]:  # adhoc.py:50
            return self.head(argv[2]) + "\n"
        try:
            return super().dispatch(argv)
        except AssertionError:
            return None


def short_sock():
    """A socket path under AF_UNIX's 104-byte limit (macOS's $TMPDIR alone is ~50): the socket holds no state."""
    return Path(tempfile.mkdtemp(prefix="macc-", dir="/tmp")) / "w.sock"


def serve(world, sock_path):
    """Answer the shim on `sock_path` in a daemon thread, one call at a time; returns the server (shutdown() stops)."""

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            argv = json.loads(self.rfile.read())
            entry = {"argv": argv}
            try:
                out = world.handle(argv)
                if out is None:
                    world.unmodelled.append(argv)
                    entry["error"] = f"unmodelled: {' '.join(argv[:4])}"
                else:
                    entry["stdout"] = out
            except core.CommandError as caught:
                entry["error"] = str(caught)
            with open(world.journal, "a", encoding="utf-8") as out:
                out.write(json.dumps(entry) + "\n")
            self.wfile.write(json.dumps({k: v for k, v in entry.items() if k != "argv"}).encode())

    server = socketserver.UnixStreamServer(str(sock_path), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
