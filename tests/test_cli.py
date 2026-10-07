import argparse
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from muster import cli, config

ROOT = Path(__file__).resolve().parent.parent


class StubCtx:
    def get_config(self, key, default):
        return default


def parse(argv):
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    return parser.parse_args(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["tick", "--dry-run"],
        ["recover", "c1", "--adopt"],
        ["hook", "notification"],
        ["hook", "done", "https://github.com/o/r/pull/1"],
        ["launch", "--cwd", ".", "--branch", "feat/x", "--title", "t", "--brief", "b"],
        ["open", "--cwd", ".", "--label", "x"],
    ],
)
def test_parser_accepts(argv):
    assert parse(argv).muster_command == argv[0]


@pytest.fixture
def stub_core(monkeypatch):
    stub = types.SimpleNamespace(tick=lambda args: 7)
    monkeypatch.setitem(sys.modules, "muster.core", stub)
    return stub


def test_main_returns_stub_exit_code(stub_core):
    assert cli.main(parse(["tick"]), StubCtx()) == 7


@pytest.mark.parametrize(
    "argv, module, expected",
    [(["hook", "stop"], "events", 0), (["tick"], "core", 2)],
)
def test_main_config_error(monkeypatch, argv, module, expected):
    def boom(args):
        raise config.ConfigError("nope")

    stub = types.SimpleNamespace(tick=boom, hook=boom)
    monkeypatch.setitem(sys.modules, f"muster.{module}", stub)
    assert cli.main(parse(argv), StubCtx()) == expected


def test_root_loads_as_package_and_dispatches(monkeypatch):
    name = "hermes_plugins.muster_x"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "hermes_plugins", types.ModuleType("hermes_plugins"))
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    calls = {}

    class Ctx(StubCtx):
        def register_cli_command(self, **kw):
            calls.update(kw)

        def register_skill(self, name, path):
            pass

    ctx = Ctx()
    module.register(ctx)
    monkeypatch.setitem(
        sys.modules, f"{name}.muster.core", types.SimpleNamespace(tick=lambda args: 9)
    )
    try:
        assert calls["handler_fn"](parse(["tick"])) == 9
    finally:
        for key in [k for k in sys.modules if k.startswith(f"{name}.")]:
            sys.modules.pop(key, None)
