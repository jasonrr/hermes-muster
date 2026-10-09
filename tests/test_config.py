import re
from pathlib import Path

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
    assert out["o/a"] == (tmp_path / "a", "a", None)
    assert out["o/b"][0].is_absolute()
    assert out["o/b"][0].name == "b"
    assert "~" not in str(out["o/b"][0])
    assert out["o/b"][1] == "b"


def test_repos_takes_an_optional_base_suffix(tmp_path):
    config.settings["clone_root"] = str(tmp_path)
    config.settings["repos"] = ["o/a@develop", "o/b=/c/b@release/2", "o/c=/x/node_modules/@scope/c"]
    out = config.repos()
    assert out["o/a"] == (tmp_path / "a", "a", "develop")
    assert out["o/b"] == (Path("/c/b"), "b", "release/2")
    assert out["o/c"] == (Path("/x/node_modules/@scope/c"), "c", None)  # "/@" belongs to the path


@pytest.mark.parametrize("entry", ["o/a@bad..ref", "o/a@"])
def test_require_rejects_a_base_that_is_not_a_branch_name(entry):
    config.settings.update(approver_login="me", repos=[entry], approver_id=7)
    with pytest.raises(config.ConfigError, match="base"):
        config.require()


def test_repos_rejects_malformed_slug():
    config.settings["repos"] = ["foo"]
    with pytest.raises(config.ConfigError):
        config.repos()


def test_require_names_missing_keys_for_defaults():
    with pytest.raises(config.ConfigError) as e:
        config.require()
    for key in ("approver_login", "repos", "approver_id"):
        assert key in str(e.value)


@pytest.mark.parametrize("bad", ["x", -1, "7", 7.0, True])
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


def test_require_rejects_an_unknown_agent_kind():
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=7, agent_kind="codex")
    with pytest.raises(config.ConfigError, match="agent_kind"):
        config.require()


@pytest.mark.parametrize("prefix", ["bad prefix/", "a..b/", "x~"])
def test_require_rejects_a_branch_prefix_that_is_not_a_valid_ref(prefix):
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=7, branch_prefix=prefix)
    with pytest.raises(config.ConfigError, match="branch_prefix"):
        config.require()


@pytest.mark.parametrize("key, bad", [("label", ""), ("label", "  "), ("label", None), ("board", 5), ("board", "")])
def test_require_rejects_a_label_or_board_that_is_not_a_non_empty_string(key, bad):
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=7, **{key: bad})
    with pytest.raises(config.ConfigError, match=key):
        config.require()


@pytest.mark.parametrize("second", ["b/tools", "b/Tools=/elsewhere/Tools"])
def test_require_rejects_two_repos_whose_clone_directories_share_a_name(tmp_path, second):
    """herdr's worktree layout is <worktrees>/<clone name>/<branch>: the two would share every worktree."""
    config.settings.update(approver_login="me", repos=["a/tools", second], approver_id=7, clone_root=str(tmp_path))
    name = second.split("=")[-1].rsplit("/", 1)[-1]
    with pytest.raises(config.ConfigError) as e:
        config.require()
    assert str(e.value) == (f"repos a/tools and {second.split('=')[0]} share the clone directory name {name}; "
                            f"give one a path with repos: owner/name=/other/dir")


def test_require_accepts_same_named_repos_cloned_under_different_names(tmp_path):
    config.settings.update(approver_login="me", repos=["a/tools", "b/tools=/x/b-tools"], approver_id=7,
                           clone_root=str(tmp_path))
    config.require()


SENTRY = {"login": "sentry[bot]", "id": 39604003, "repos": ["o/a"]}


@pytest.mark.parametrize("auto", [[], [SENTRY], [{**SENTRY, "label": "auto-ok", "repos": ["O/A"]}]])
def test_require_accepts_auto_approvers(auto):
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=7, auto_approvers=auto)
    config.require()


@pytest.mark.parametrize("auto", [
    None, SENTRY, ["sentry[bot]"],
    [{k: v for k, v in SENTRY.items() if k != "login"}], [{**SENTRY, "login": ""}],
    [{k: v for k, v in SENTRY.items() if k != "id"}], [{**SENTRY, "id": "39604003"}], [{**SENTRY, "id": True}],
    [{**SENTRY, "id": 0}],
    [{k: v for k, v in SENTRY.items() if k != "repos"}], [{**SENTRY, "repos": []}], [{**SENTRY, "repos": "o/a"}],
    [{**SENTRY, "repos": ["o/b"]}], [{**SENTRY, "label": ""}], [{**SENTRY, "label": 5}],
    [{**SENTRY, "label": "Agent-Ready"}], [{**SENTRY, "lable": "x"}],
])
def test_require_rejects_a_malformed_auto_approver(auto):
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=7, auto_approvers=auto)
    with pytest.raises(config.ConfigError, match="auto_approvers"):
        config.require()


@pytest.mark.parametrize("change,key", [
    ({"project_owner": "o", "project_number": 0}, "project_number"),
    ({"project_owner": "o", "project_number": "5"}, "project_number"),
    ({"project_owner": "o", "project_number": True}, "project_number"),
    ({"project_owner": "", "project_number": 5}, "project_owner"),
    ({"project_owner": " ", "project_number": 5}, "project_owner"),
    ({"project_owner": "o", "project_number": 5, "project_status_field": ""}, "project_status_field"),
    ({"project_owner": "o", "project_number": 5, "project_status_value": None}, "project_status_value"),
])
def test_require_rejects_a_half_set_project(change, key):
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=7, **change)
    with pytest.raises(config.ConfigError, match=key):
        config.require()


@pytest.mark.parametrize("owner,number", [("", 0), ("o", 5)])
def test_require_passes_with_no_project_or_a_whole_one(owner, number):
    config.settings.update(approver_login="me", repos=["o/a"], approver_id=7, project_owner=owner, project_number=number)
    config.require()


@pytest.mark.parametrize("change, missing", [
    ({"notify_chat_id": ""}, "notify_chat_id"),
    ({"notify_user_id": ""}, "notify_user_id"),
    ({"notify_platform": "slack"}, "notify_platform"),
    ({"notify_topics": "yes"}, "notify_topics"),
])
def test_require_rejects_topics_without_a_telegram_group(change, missing):
    config.settings.update({"approver_login": "me", "repos": ["o/a"], "approver_id": 7, "notify_topics": True,
                            "notify_chat_id": "-100123", "notify_user_id": "42", **change})
    with pytest.raises(config.ConfigError, match=missing):
        config.require()


def test_topics_are_off_by_default():
    assert config.DEFAULTS["notify_topics"] is False
