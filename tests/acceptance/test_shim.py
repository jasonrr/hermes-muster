"""The acceptance shim and world server, in a real child interpreter that loads muster the way Hermes does
(hermes_cli/plugins_loader.py:_load_directory_module: the plugin root by file location, its modules by relative
import). No Hermes needed: this runs in the ordinary suite."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from tests.acceptance import world as W

REPO = Path(__file__).resolve().parents[2]
SHIM = REPO / "tests" / "acceptance" / "shim"

CHILD = textwrap.dedent("""
    import importlib, importlib.util, json, sys, types
    sys.modules["hermes_plugins"] = types.ModuleType("hermes_plugins")
    sys.modules["hermes_plugins"].__path__ = []
    plugin = sys.argv[1]
    spec = importlib.util.spec_from_file_location("hermes_plugins.muster", plugin + "/__init__.py",
                                                  submodule_search_locations=[plugin])
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_plugins.muster"] = module
    spec.loader.exec_module(module)
    core = importlib.import_module("hermes_plugins.muster.muster.core")
    out = {"shim": core.run.__name__}
    for argv in json.loads(sys.argv[2]):
        try:
            out[argv[0] + " " + argv[-1]] = core.run(argv)
        except core.CommandError as caught:
            out[argv[0] + " " + argv[-1]] = "CommandError: " + str(caught)
    print(json.dumps(out))
""")


def child(tmp_path, calls, sock="/nonexistent/w.sock", **env):
    plugin = tmp_path / "plugins" / "muster"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    if not plugin.exists():
        plugin.symlink_to(REPO)
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "hermes").write_text("#!/bin/sh\necho real hermes \"$@\"\n")
    (bin_ / "hermes").chmod(0o755)
    base = {"PATH": f"{bin_}:/usr/bin:/bin", "PYTHONPATH": str(SHIM), "MUSTER_UNDER_TEST": str(REPO),
            "MUSTER_WORLD_SOCK": str(sock), "MUSTER_JOURNAL": str(tmp_path / "journal.jsonl"),
            "MUSTER_MARKERS": str(tmp_path / "markers"), "HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, "-c", CHILD, str(plugin), json.dumps(calls)], env={**base, **env},
                          capture_output=True, text=True, timeout=60, check=True)
    return json.loads(done.stdout)


def journal(tmp_path):
    path = tmp_path / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_the_shim_routes_each_command_and_journals_it_in_order(tmp_path):
    world = W.AcceptanceWorld(tmp_path, tmp_path / "journal.jsonl")
    sock = W.short_sock()
    server = W.serve(world, sock)
    try:
        out = child(tmp_path, [["git", "-C", "/c", "remote", "get-url", "origin"], ["bash", "-c", "true"],
                               ["hermes", "kanban"], ["gh", "api", "repos/x"]], sock)
    finally:
        server.shutdown()
        server.server_close()
        sock.unlink()
        sock.parent.rmdir()
    assert out["shim"] == "shim_run"
    assert out["git origin"] == W.ORIGIN + "\n"
    assert out["bash true"].startswith("CommandError: acceptance shim: refused bash")
    assert out["hermes kanban"] == "real hermes kanban\n"
    assert out["gh repos/x"] == "CommandError: unmodelled: gh api repos/x"
    assert world.unmodelled == [["gh", "api", "repos/x"]]
    assert [(e["argv"][0], "refused" in e, "error" in e) for e in journal(tmp_path)] == [
        ("git", False, False), ("bash", True, False), ("gh", False, True)]
    markers = [json.loads(p.read_text()) for p in (tmp_path / "markers").iterdir()]
    assert len(markers) == 1 and markers[0]["patched"] is True


def test_without_a_world_every_world_command_is_refused(tmp_path):
    out = child(tmp_path, [["herdr", "agent", "get", "p1"]])
    assert out["herdr p1"] == "CommandError: acceptance shim: no world"
    assert journal(tmp_path)[0]["refused"] is True


def test_another_module_named_core_is_left_alone(tmp_path):
    other = tmp_path / "other"
    (other / "muster").mkdir(parents=True)
    out = child(tmp_path, [], MUSTER_UNDER_TEST=str(other))
    assert out["shim"] == "run" and not (tmp_path / "markers").exists()


def test_the_chained_sitecustomize_runs_before_the_finder(tmp_path):
    chain = tmp_path / "chain.py"
    chain.write_text("import os, sys\nopen(os.environ['HOME'] + '/chained', 'w').write("
                     "str(any(type(f).__name__ == 'Finder' for f in sys.meta_path)))\n")
    out = child(tmp_path, [], MUSTER_CHAIN_SITECUSTOMIZE=str(chain))
    assert (tmp_path / "chained").read_text() == "False" and out["shim"] == "shim_run"
