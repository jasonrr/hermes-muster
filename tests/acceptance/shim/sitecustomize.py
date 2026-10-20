"""The acceptance shim: every child Python of the harness imports this at startup (it is first on PYTHONPATH).

It swaps `run` (muster's only subprocess seam) in the muster under test, matched by file path, for `shim_run`:
`hermes` runs for real against the test home, herdr, gh and git go to the test's world server, anything else is
refused. Each load leaves a marker the preflight reads (pid, argv). Design: docs/plans/2026-10-20-slack-acceptance-design.md §5.
"""

import importlib.machinery
import json
import os
import runpy
import socket
import sys
from pathlib import Path

if os.environ.get("MUSTER_CHAIN_SITECUSTOMIZE"):  # the stand-in's own shim (points slack_sdk at the stand-in)
    runpy.run_path(os.environ["MUSTER_CHAIN_SITECUSTOMIZE"])

WORLD = ("herdr", "gh", "git")


def append(path, entry):
    with open(path, "a", encoding="utf-8") as out:  # one write per line: concurrent children append whole lines
        out.write(json.dumps(entry) + "\n")


def ask_world(argv):
    """({"stdout": …} or {"error": …}) from the world server, one connection per call."""
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(60)  # a dead world thread is a refusal, never a hang
        conn.connect(os.environ["MUSTER_WORLD_SOCK"])
        conn.sendall(json.dumps(argv).encode())
        conn.shutdown(socket.SHUT_WR)
        return json.loads(b"".join(iter(lambda: conn.recv(65536), b"")))


def patch(module):
    original, error = module.run, module.CommandError

    def shim_run(argv, env=None):
        tool = os.path.basename(argv[0]) if argv else ""
        if tool == "hermes":
            return original(argv, env)
        if tool not in WORLD:
            append(os.environ["MUSTER_JOURNAL"], {"argv": list(argv), "refused": True, "pid": os.getpid()})
            raise error(f"acceptance shim: refused {' '.join(map(str, argv[:3]))}")
        try:
            reply = ask_world(list(argv))
        except OSError:
            append(os.environ["MUSTER_JOURNAL"], {"argv": list(argv), "refused": True, "pid": os.getpid()})
            raise error("acceptance shim: no world") from None
        if "error" in reply:
            raise error(reply["error"])
        return reply["stdout"]

    module.run = shim_run
    marker = Path(os.environ["MUSTER_MARKERS"]) / f"{os.getpid()}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pid": os.getpid(), "argv": sys.argv, "patched": module.run is shim_run}))


class Finder:
    """Wraps the loader of the one module whose file is the muster under test's core.py, under any dotted name."""

    def __init__(self, target):
        self.target = target

    def find_spec(self, name, path, target=None):
        if name.rpartition(".")[2] != "core":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None or not spec.origin or Path(spec.origin).resolve() != self.target:
            return None
        execute = spec.loader.exec_module

        def exec_module(module):
            execute(module)
            patch(module)

        spec.loader.exec_module = exec_module
        return spec


if os.environ.get("MUSTER_UNDER_TEST"):
    sys.meta_path.insert(0, Finder((Path(os.environ["MUSTER_UNDER_TEST"]) / "muster" / "core.py").resolve()))
