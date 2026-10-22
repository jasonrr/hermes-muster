import os
import subprocess
import sys
from pathlib import Path

import pytest

from muster import core


def test_sandbox_identity_is_hermess_native_default(tmp_path, monkeypatch):
    """The sandbox home names the profile, as Hermes derives it; the launching shell's pins never leak in."""
    pytest.importorskip("hermes_cli")
    from hermes_cli.profiles import current_profile_name

    assert {n for n in os.environ if n.startswith("HERMES_")} == {"HERMES_HOME", "HERMES_KANBAN_HOME"}
    assert core.board_db().is_relative_to(tmp_path)
    assert current_profile_name("user") == "default"  # a custom HERMES_HOME is its own root
    monkeypatch.setenv("HERMES_PROFILE_NAME", "bean")
    assert current_profile_name("user") == "bean"


def test_suite_ignores_the_shell_profile(tmp_path):
    """Run from a shell pinned to another profile and board (a Hermes worker's), the suite still holds."""
    pytest.importorskip("hermes_cli")
    env = {**os.environ, "HERMES_PROFILE": "bean", "HERMES_PROFILE_NAME": "bean",
           "HERMES_KANBAN_DB": str(tmp_path / "live-kanban.db")}
    node = "tests/test_sandbox.py::test_sandbox_identity_is_hermess_native_default"
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node], env=env,
                          cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
