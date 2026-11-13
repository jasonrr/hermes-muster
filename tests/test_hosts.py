import pytest

import muster.config as config
import muster.hosts as hosts


def test_a_record_without_a_host_is_herdrs():
    assert hosts.of({}).NAME == "herdr"
    assert hosts.of(None).NAME == "herdr"


def test_a_record_names_its_host_itself_or_through_its_launch():
    assert hosts.of({"host": "orca"}).NAME == "orca"
    assert hosts.of({"launch": {"host": "orca"}}).NAME == "orca"


def test_an_unknown_host_is_a_config_error():
    with pytest.raises(config.ConfigError, match="host 'tmux' is not one of herdr, orca"):
        hosts.get("tmux")


def test_configured_follows_the_setting(monkeypatch):
    monkeypatch.setitem(config.settings, "host", "orca")
    assert hosts.configured().NAME == "orca"


def test_program_names_the_program():
    assert hosts.program("-/bin/zsh -l") == "zsh"
    assert hosts.program("/usr/local/bin/claude --model x") == "claude"
    assert hosts.program("") == ""
