"""The guard on hermes_kit: every name and parameter the harness relies on, failing loudly when Hermes moves one."""

import inspect
import os

import pytest

if not os.environ.get("HERMES_AGENT_DIR"):
    pytest.skip("HERMES_AGENT_DIR unset", allow_module_level=True)

from tests.acceptance import hermes_kit as kit  # noqa: E402

SHAPE = {
    "GatewayUnderTest": ["llm_base_url", "config", "env", "ready"],
    "hermetic_env": ["home", "extra"],
    "Text": ["text", "chunk_chars", "delay_per_chunk"],
    "ToolCall": ["name", "args"],
}
METHODS = {
    "SlackStandin": ["block_action", "visible", "buttons", "ephemerals", "calls_of", "socket_count"],
    "Director": ["script"],
    "SlackDriver": ["gateway_config", "gateway_env", "connected"],
    "GatewayUnderTest": ["start", "stop", "run_cli", "alive"],
    "FakeLLMServer": ["start", "stop", "base_url"],
}


@pytest.mark.parametrize("name", SHAPE)
def test_each_parameter_the_harness_passes_exists(name):
    params = inspect.signature(getattr(kit, name)).parameters
    missing = [p for p in SHAPE[name] if p not in params]
    assert not missing, f"Hermes test kit shape changed: {name}({', '.join(missing)})"


@pytest.mark.parametrize("name", METHODS)
def test_each_method_the_harness_calls_exists(name):
    missing = [m for m in METHODS[name] if not hasattr(getattr(kit, name), m)]
    assert not missing, f"Hermes test kit shape changed: {name}.{', '.join(missing)}"


def test_the_stand_in_shim_and_muster_tests_are_both_reachable():
    from tests import fake_herdr  # muster's own tests package, not Hermes's

    assert kit.SLACK_SHIM.is_file() and hasattr(fake_herdr, "World")
