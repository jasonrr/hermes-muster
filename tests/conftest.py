import os
import subprocess

import pytest

from muster import config, core

REPO_NAMES = ("radicalcandorai", "radicalcandorwebsite", "RC-Data-Tools", "hermes-rc-engineering-intake")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """No test touches the real lock, the real hermes home, a real gh config or a real clone."""
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    (hermes / ".env").write_text("OTHER=1\nTELEGRAM_HOME_CHANNEL=4242\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(hermes))
    # prepare_env() rewrites PATH; setenv first so pytest restores it afterwards.
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    bot = tmp_path / "gh-bot"
    bot.mkdir()
    (bot / "hosts.yml").write_text("github.com:\n  user: candor-code-factory\n")
    repos = []
    for name in REPO_NAMES:
        clone = tmp_path / "clones" / name
        clone.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        repos.append(f"Radical-Candor-LLC/{name}={clone}")
    for key, value in {
        "approver_login": "jasonrr", "approver_id": 108170, "repos": repos,
        "gh_config_dir": str(bot), "notify_chat_id": "", "worktrees": str(tmp_path / "worktrees"),
    }.items():
        monkeypatch.setitem(config.settings, key, value)
    monkeypatch.setattr(core, "_adapter", None)
    return tmp_path
