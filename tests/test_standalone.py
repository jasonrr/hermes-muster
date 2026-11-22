import sys
import types

import pytest

from muster import cli, config, core, decisions, launcher


def test_load_file_reads_settings(standalone_home):
    config.load_file()
    assert config.standalone is True
    assert config.settings["approver_login"] == "jasonrr"
    assert config.settings["board"] == config.DEFAULTS["board"]
    assert config.settings["notify_topics"] is False


def test_load_file_missing(standalone_home):
    (standalone_home / "config.toml").unlink()
    with pytest.raises(config.ConfigError, match="no settings file"):
        config.load_file()


def test_load_file_bad_toml(standalone_home):
    (standalone_home / "config.toml").write_text("repos = [")
    with pytest.raises(config.ConfigError, match="config.toml"):
        config.load_file()


def test_load_file_unknown_key(standalone_home):
    (standalone_home / "config.toml").write_text("bogus = 1\n")
    with pytest.raises(config.ConfigError, match="bogus"):
        config.load_file()


def test_load_file_unreadable(standalone_home):
    (standalone_home / "config.toml").unlink()
    (standalone_home / "config.toml").mkdir()
    with pytest.raises(config.ConfigError, match="config.toml"):
        config.load_file()


def test_home_is_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MUSTER_HOME", "m")
    assert config.home() == tmp_path / "m"


def test_home_defaults_to_dot_muster(monkeypatch):
    monkeypatch.delenv("MUSTER_HOME", raising=False)
    assert config.home() == config.Path("~/.muster").expanduser()


def test_data_dir_is_under_home(standalone_home):
    config.load_file()
    assert config.data_dir() == standalone_home / "data"
    assert (standalone_home / "data").is_dir()


def test_muster_name(standalone_home):
    assert config.muster_name() == "hermes muster"
    config.load_file()
    assert config.muster_name() == "muster"


def test_require_without_hermes(standalone_home, monkeypatch):
    config.load_file()
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    monkeypatch.setattr(config.Path, "home", lambda: standalone_home)
    config.require()


def test_operating_profile_is_default(standalone_home):
    config.load_file()
    assert core.operating_profile() == "default"


def test_hook_cmd_under_hermes(standalone_home, monkeypatch):
    monkeypatch.setattr(core, "hermes_cmd", lambda: ["/bin/hermes"])
    assert core.hook_cmd() == ["/bin/hermes", "muster", "hook"]


def test_hook_cmd_standalone(standalone_home, monkeypatch):
    config.load_file()
    monkeypatch.setattr(core, "hermes_cmd", lambda: pytest.fail("hermes_cmd called"))
    assert core.hook_cmd() == ["/bin/muster", "hook"]


def test_pane_env_standalone(standalone_home, monkeypatch):
    config.load_file()
    monkeypatch.setattr(core, "hermes_home", lambda: pytest.fail("hermes_home called"))
    env = launcher.pane_env()
    assert env[:2] == [f"MUSTER_HOME={standalone_home}", "MUSTER_PANE=1"]
    assert not any(pair.startswith("HERMES_") for pair in env)
    assert any(pair.startswith("GH_CONFIG_DIR=") for pair in env)


def test_pane_env_under_hermes(standalone_home):
    env = launcher.pane_env()
    assert env[0].startswith("HERMES_HOME=")
    assert not any(pair.startswith("MUSTER_") for pair in env)


def test_gateway_up_standalone(standalone_home):
    config.load_file()
    assert decisions.gateway_up() is True


def test_standalone_entry_runs_subcommand(standalone_home, monkeypatch):
    monkeypatch.setitem(sys.modules, "muster.intake", types.SimpleNamespace(tick=lambda args: 7))
    assert cli.standalone(["tick"]) == 7
    assert config.standalone is True
