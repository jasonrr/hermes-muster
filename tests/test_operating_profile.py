"""operating_profile: the profile Hermes resolves runs muster; the setting only confirms it, never routes elsewhere."""

import json
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from muster import cli, config, core, decisions, hold, launcher
from muster.channels import shared, slack, telegram
from tests.test_gateway_readiness import record, runtime  # noqa: F401 - runtime: PR #68's multiplex fixture


@pytest.fixture
def profiles(monkeypatch):
    """A stand-in hermes_cli.profiles: `running` is the profile this process serves, `known` the live ones."""
    mod = ModuleType("hermes_cli.profiles")
    mod.running, mod.known = "default", {"default", "bean"}
    mod.get_active_profile_name = lambda: mod.running
    mod.profile_exists = lambda name: name in mod.known
    package = ModuleType("hermes_cli")
    package.profiles = mod
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", mod)
    return mod


@pytest.mark.parametrize("running", ["default", "bean"])
def test_empty_means_the_profile_hermes_resolves(profiles, running):
    profiles.running = running
    assert config.settings["operating_profile"] == ""
    assert core.operating_profile() == running


def test_a_setting_naming_the_running_profile_confirms_it(profiles, monkeypatch):
    profiles.running = "bean"
    monkeypatch.setitem(config.settings, "operating_profile", "bean")
    assert core.operating_profile() == "bean"


@pytest.mark.parametrize("value, words", [
    ("ghost", "'ghost' is not a Hermes profile"),
    ("default", "'default' but muster is running in profile 'bean'"),
    (7, "must be a profile name"),
])
def test_an_unknown_other_or_malformed_profile_is_refused(profiles, monkeypatch, value, words):
    profiles.running = "bean"
    monkeypatch.setitem(config.settings, "operating_profile", value)
    with pytest.raises(config.ConfigError, match=words):
        core.operating_profile()


@pytest.mark.parametrize("argv, code", [(["tick"], 2), (["hook", "stop"], 0), (["hook", "propose", "d.md"], 1)])
def test_every_command_and_hook_refuses_a_mismatch_before_it_starts(profiles, monkeypatch, capsys, argv, code):
    """cli.main is the one seam: a command exits 2, a hook its usual failure code, and no subcommand runs."""
    import argparse

    profiles.running = "bean"
    monkeypatch.setitem(config.settings, "operating_profile", "default")
    ran = []
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: ran.append(name))
    parser = argparse.ArgumentParser()
    cli.setup(parser)

    class Ctx:
        def get_config(self, key, default):
            return config.settings.get(key, default)
    assert cli.main(parser.parse_args(argv), Ctx()) == code
    assert ran == [] and "running in profile 'bean'" in capsys.readouterr().err


def test_a_custom_home_is_never_pinned(profiles, monkeypatch):
    """Hermes names a HERMES_HOME outside its profiles `custom`; there is no profile to pass to -p."""
    profiles.running = "custom"
    assert core.hook_cmd() == [config.hermes_bin(), "muster", "hook"]


def test_outside_hermes_the_profile_is_default(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)  # import hermes_cli.profiles raises ImportError
    assert core.operating_profile() == "default"


# -- every identity path follows it ----------------------------------------------------------------

def subs(rows, calls):
    """A kanban stand-in for subscribe: notify-subscribe stamps a row as the flags say; notify-list returns them."""
    def kanban(*args):
        calls.append(args)
        if args[0] == "notify-subscribe":
            flag = dict(zip(args[2::2], args[3::2]))
            rows[:] = [{"chat_id": flag["--chat-id"], "user_id": flag["--user-id"], "chat_type": flag["--chat-type"],
                        "notifier_profile": flag["--notifier-profile"], "delivery_mode": flag["--delivery-mode"]}]
            return ""
        return json.dumps(rows)
    return kanban


@pytest.mark.parametrize("running, pin", [("bean", ["-p", "bean"]), ("default", [])])
def test_hooks_subscriptions_and_recover_text_carry_the_profile(profiles, monkeypatch, running, pin):
    profiles.running = running
    rows, calls = [], []
    monkeypatch.setattr(core, "kanban", subs(rows, calls))
    assert core.hook_cmd() == [config.hermes_bin(), *pin, "muster", "hook"]
    core.subscribe("t_card")
    assert rows[0]["notifier_profile"] == running
    rec = {"card": "t_held", "repo": "o/r", "issue": 3, "reason": "same", "candidates": [{"card": "t_c", "issue": 2}]}
    assert f"`{' '.join([config.hermes_bin(), *pin])} muster recover t_held`" in hold.message(rec)


def test_a_read_back_stamped_with_another_profile_is_refused(profiles, monkeypatch):
    profiles.running = "bean"
    rows, calls = [], []
    kanban = subs(rows, calls)

    def stamped_default(*args):
        out = kanban(*args)
        for row in rows:
            row["notifier_profile"] = "default"
        return out
    monkeypatch.setattr(core, "kanban", stamped_default)
    with pytest.raises(core.LaunchError, match="did not read back"):
        core.subscribe("t_card")


def test_a_mismatch_subscribes_nothing(profiles, monkeypatch):
    profiles.running = "bean"
    monkeypatch.setitem(config.settings, "operating_profile", "default")
    calls = []
    monkeypatch.setattr(core, "kanban", subs([], calls))
    with pytest.raises(config.ConfigError):
        core.subscribe("t_card")
    with pytest.raises(config.ConfigError):
        core.hook_cmd()
    assert calls == []


@pytest.mark.parametrize("setting", ["", "bean"])
def test_a_served_profile_binds_hooks_panes_and_subscription_with_real_hermes(tmp_path, monkeypatch, setting):
    """The multiplexed case, with Hermes's own resolvers: the gateway's process home is the host root, the served
    profile's home is a context-local override."""
    pytest.importorskip("hermes_cli.profiles")
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root = tmp_path / "root"
    bean = root / "profiles" / "bean"
    bean.mkdir(parents=True)
    for home in (root, bean):
        (home / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_PROFILE_NAME", "default")  # an env pin never renames the home's profile
    for key, value in {"operating_profile": setting, "notify_platform": "slack", "notify_chat_id": "C1",
                       "notify_user_id": "U1"}.items():  # the Bean shape: the host's shared Slack, a set channel
        monkeypatch.setitem(config.settings, key, value)
    rows, calls = [], []
    monkeypatch.setattr(core, "kanban", subs(rows, calls))
    token = set_hermes_home_override(bean)
    try:
        assert core.hermes_home() == bean
        assert core.hook_cmd()[1:] == ["-p", "bean", "muster", "hook"]
        assert launcher.pane_env()[:2] == [f"HERMES_HOME={bean}", f"HERMES_KANBAN_HOME={root}"]
        core.subscribe("t_card")
        assert rows[0]["notifier_profile"] == "bean"
        monkeypatch.setitem(config.settings, "operating_profile", "default")
        with pytest.raises(config.ConfigError, match="running in profile 'bean'"):
            core.subscribe("t_other")
    finally:
        reset_hermes_home_override(token)


# -- a change moves nothing; readiness and the native settings form --------------------------------

def test_a_mismatch_leaves_saved_runs_decisions_and_subscriptions_untouched(profiles, monkeypatch, capsys):
    """Saved state as a live install has it; a mismatching setting then stops tick, flush, cleanup and a hook before
    any command runs, so nothing is moved, rewritten or sent again."""
    import argparse
    import hashlib

    data = config.data_dir()
    for rel, body in {"decisions/d1.json": {"id": "d1", "status": "open"},
                      "decisions/archive/d0.json": {"id": "d0", "status": "done"},
                      "intake/t_run/launch.json": {"step": "done", "prompt": {"state": "done"}},
                      "bridge/t_run/receipts.json": {"acked": ["e1"]}}.items():
        core.save_json(data / rel, body)

    def snapshot():
        return {str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(data.rglob("*")) if p.is_file()}
    before = snapshot()
    profiles.running = "bean"
    monkeypatch.setitem(config.settings, "operating_profile", "default")
    commands = []
    monkeypatch.setattr(core, "run", lambda argv, env=None: commands.append(argv))
    parser = argparse.ArgumentParser()
    cli.setup(parser)

    class Ctx:
        def get_config(self, key, default):
            return config.settings.get(key, default)
    for argv in (["tick"], ["flush"], ["cleanup"], ["hook", "stop", "--card", "t_run"]):
        cli.main(parser.parse_args(argv), Ctx())
    assert commands == [] and snapshot() == before


def test_readiness_checks_the_same_home_the_setting_confirms(runtime, profiles, monkeypatch):
    """The PR #68 multiplex path with the setting pinned: the served profile's home is the one readiness asks
    Hermes about, and the host's shared Slack counts."""
    status, home = runtime
    profiles.running = "bean"
    monkeypatch.setitem(config.settings, "operating_profile", "bean")
    status.served = (42, record(slack="connected"))
    assert core.operating_profile() == "bean"
    assert decisions.gateway_up() is True
    assert status.lookups == [core.hermes_home()] == [home]


def test_the_native_settings_form_lists_and_saves_the_field(tmp_path):
    """Hermes's own Plugins settings backend (what the Desktop's plugins.manage list and settings calls run):
    the field is a plain string with its label, saves into this profile's config.yaml, and reads back."""
    pytest.importorskip("hermes_cli.plugins_settings")
    from pathlib import Path

    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_settings import plugin_settings_fields, save_plugin_settings

    repo = Path(__file__).resolve().parent.parent
    (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text("plugins:\n  enabled: [muster]\n")
    field = next(f for f in plugin_settings_fields("muster", repo) if f["key"] == "operating_profile")
    assert (field["type"], field["label"], field["value"]) == ("string", "Operating profile", "")
    assert save_plugin_settings("muster", repo, {"operating_profile": "bean"}) == ["operating_profile"]
    settings = load_config_readonly()["plugins"]["entries"]["muster"]["settings"]
    assert settings == {"operating_profile": "bean"}
    assert next(f for f in plugin_settings_fields("muster", repo) if f["key"] == "operating_profile")["value"] == "bean"


@pytest.mark.parametrize("channel", [slack, telegram])
def test_the_gateway_refuses_a_mismatch_before_any_handler_or_scan(profiles, monkeypatch, channel):
    """The gateway never passes cli.main: its connect hook and every tap and reply check (ensure_config) stop instead,
    before a handler, a scan or a kanban call."""
    profiles.running = "bean"
    monkeypatch.setitem(config.settings, "operating_profile", "default")

    class Ctx:
        def get_config(self, key, default):
            return config.settings.get(key, default)
    monkeypatch.setattr(shared, "CTX", Ctx())
    monkeypatch.setattr(shared, "configured", False)
    scans, commands = [], []
    monkeypatch.setattr(shared, "start_scan", lambda: scans.append(1))
    monkeypatch.setattr(core, "run", lambda argv, env=None: commands.append(argv))
    app = SimpleNamespace(client=None, add_handler=lambda *a: commands.append(a))
    (channel.factory if channel is slack else channel.telegram_factory)(app, object())
    with pytest.raises(config.ConfigError, match="running in profile 'bean'"):
        shared.ensure_config()
    assert scans == [] and commands == []
