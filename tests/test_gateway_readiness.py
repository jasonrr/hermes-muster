"""Readiness follows the gateway serving Muster, not another bot's local status file."""

import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from muster import config, decisions


@pytest.fixture
def runtime(monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    status = ModuleType("gateway.status")
    status.own = None
    status.served = None
    status.reads = []
    status.lookups = []

    def read(path=None):
        status.reads.append(path)
        return status.own

    def served(profile_dir):
        status.lookups.append(profile_dir)
        return status.served

    status.read_runtime_status = read
    status.get_runtime_status_running_pid = lambda rec, *, expected_home: (rec or {}).get("live_pid")
    status.runtime_status_pid_is_live = lambda rec: bool((rec or {}).get("live_pid"))
    status.runtime_status_is_stale = lambda rec: not rec or rec.get("stale", False)
    status.multiplexer_liveness_for_profile = served
    status.profile_name_for_home = lambda path: "bean"
    status.profile_platforms_from_multiplexer = lambda rec, name: {
        key[len(name) + 1:]: value for key, value in rec.get("platforms", {}).items()
        if key.startswith(name + ":")
    }
    gateway = ModuleType("gateway")
    gateway.status = status
    constants = ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.status", status)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(config.settings, "notify_platform", "slack")
    return status, home


def record(**platforms):
    return {"live_pid": 42, "platforms": {name: {"state": state} for name, state in platforms.items()}}


def test_missing_local_status_uses_the_serving_multiplexer_and_shared_slack(runtime):
    status, home = runtime
    status.served = (42, record(slack="connected", **{"jane:telegram": "disconnected"}))
    assert decisions.gateway_up() is True
    assert status.reads == [home / "gateway_state.json"]
    assert status.lookups == [home]


@pytest.mark.parametrize("own_state, shared_state, ready", [
    ("connected", "disconnected", True),
    ("disconnected", "connected", False),
    ("connecting", "connected", False),
])
def test_profile_transport_takes_precedence_over_shared_transport(runtime, own_state, shared_state, ready):
    status, _ = runtime
    status.served = (42, record(slack=shared_state, **{"bean:slack": own_state}))
    assert decisions.gateway_up() is ready


@pytest.mark.parametrize("rec", [None, {}, record(slack="connected")])
def test_no_live_serving_gateway_is_not_ready(runtime, rec):
    status, _ = runtime
    if rec:
        rec["live_pid"] = None
    status.own = rec
    assert decisions.gateway_up() is False


@pytest.mark.parametrize("change", ["stale", "dead", "disconnected", "missing"])
def test_bad_host_snapshot_is_not_ready(runtime, change):
    status, _ = runtime
    rec = record(slack="connected")
    if change == "stale":
        rec["stale"] = True
    elif change == "dead":
        rec["live_pid"] = None
    elif change == "disconnected":
        rec["platforms"]["slack"]["state"] = "disconnected"
    else:
        rec["platforms"] = {"jane:slack": {"state": "connected"}}
    status.served = (42, rec)
    assert decisions.gateway_up() is False


def test_live_standalone_gateway_is_used_without_borrowing_another_bot(runtime):
    status, _ = runtime
    status.own = record(slack="connected")
    assert decisions.gateway_up() is True
    assert status.lookups == []
    status.own["platforms"]["slack"]["state"] = "disconnected"
    status.served = (42, record(slack="connected"))
    assert decisions.gateway_up() is False
    assert status.lookups == []


def test_stale_local_record_does_not_hide_the_live_multiplexer(runtime):
    status, _ = runtime
    status.own = {"live_pid": None, "stale": True, "platforms": {}}
    status.served = (42, record(slack="connected"))
    assert decisions.gateway_up() is True
