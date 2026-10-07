import re

import pytest

from muster import config


@pytest.fixture(autouse=True)
def fresh_settings():
    config.settings.clear()
    config.settings.update(config.DEFAULTS)
    yield
    config.settings.clear()
    config.settings.update(config.DEFAULTS)


class StubCtx:
    def get_config(self, key, default):
        return default


def test_load_with_defaults_leaves_settings_equal_defaults():
    config.settings["agent_model"] = "changed"
    config.load(StubCtx())
    assert config.settings == config.DEFAULTS


def test_repos_parses_both_forms(tmp_path):
    config.settings["clone_root"] = str(tmp_path)
    config.settings["repos"] = ["o/a", "o/b=~/clones/b"]
    out = config.repos()
    assert out["o/a"] == (tmp_path / "a", "a")
    assert out["o/b"][0].is_absolute()
    assert out["o/b"][0].name == "b"
    assert "~" not in str(out["o/b"][0])
    assert out["o/b"][1] == "b"


def test_repos_rejects_malformed_slug():
    config.settings["repos"] = ["foo"]
    with pytest.raises(config.ConfigError):
        config.repos()


def test_require_names_missing_keys_for_defaults():
    with pytest.raises(config.ConfigError) as e:
        config.require()
    for key in ("approver_login", "repos", "approver_id"):
        assert key in str(e.value)


@pytest.mark.parametrize("bad", ["x", -1])
def test_require_rejects_bad_approver_id(bad):
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=bad)
    with pytest.raises(config.ConfigError, match="approver_id"):
        config.require()


def test_require_passes_when_configured():
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=7)
    config.require()


def test_data_dir_honors_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert config.data_dir() == tmp_path / "plugin-data" / "muster"
    assert config.notes_dir() == tmp_path / "plugin-data" / "muster" / "repos"


def test_workflow_text_reads_bundled_prompt():
    lines = config.workflow_text().splitlines()
    assert lines[0] == "# How to work"
    assert sum(1 for line in lines if re.match(r"\d+\. ", line)) == 13


def test_workflow_prompt_file_override(tmp_path):
    f = tmp_path / "w.md"
    f.write_text("  custom  \n")
    config.settings["workflow_prompt_file"] = str(f)
    assert config.workflow_text() == "custom"


@pytest.mark.parametrize("prefix", [None, "", 7])
def test_require_rejects_a_branch_prefix_that_is_not_a_non_empty_string(prefix):
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=7, branch_prefix=prefix)
    with pytest.raises(config.ConfigError, match="branch_prefix"):
        config.require()


def test_setting_keys_avoid_hermes_reserved_roots():
    # ctx.get_config rejects these first segments (hermes_cli/plugins_state.py)
    assert not {"model", "plugins", "security", "settings"} & set(config.DEFAULTS)
