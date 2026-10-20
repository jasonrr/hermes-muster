"""The only module that imports Hermes's own test kit (its `tests.*` package, not a public API). Test-only reading of
AGENTS.md: muster/hermes_private.py stays the only production module touching private Hermes names; this is its
test-side twin. Upstream ask, noted and not filed: ship the gateway harness and the Slack stand-in as plugin test
fixtures. test_kit.py fails loudly ("Hermes test kit shape changed: <name>") when a name below moves.

Known Hermes gap (hermes_cli/gateway.py:2263-2288, 2863-2878, 4335-4369 at 3c2f1cf86c): the kit's child gets
HOME=<tmp>, HERMES_HOME=<tmp>/.hermes, so Hermes takes that home for the native default, gives it the bare service
label, finds the real account's launchd plist and refuses to start while production's gateway runs. The rig gives
the child its own HOME (conftest.py). Upstream ask, not filed: derive the bare label from the real account's home.

Loaded with Hermes's checkout first on sys.path, then every `tests.*` module it brought is renamed `hermes_tests.*`
and muster's own `tests` package is put back, so muster's tests are never shadowed.
"""

import importlib
import os
import sys
from pathlib import Path

HERMES = Path(os.environ["HERMES_AGENT_DIR"]).expanduser().resolve()


def _ours(name):
    return name == "tests" or name.startswith("tests.")


saved = {k: sys.modules.pop(k) for k in [k for k in sys.modules if _ours(k)]}
sys.path.insert(0, str(HERMES))
try:
    _helpers = importlib.import_module("tests.e2e.core.platforms._helpers")  # e2e/core/platforms/_helpers.py
    _drv = importlib.import_module("tests.e2e.core.platforms._drv_slack")    # e2e/core/platforms/_drv_slack.py
    _standin = importlib.import_module("tests.fakes.platforms._standin")     # fakes/platforms/_standin.py
    _slack = importlib.import_module("tests.fakes.platforms.slack_standin")  # fakes/platforms/slack_standin.py
    _llm = importlib.import_module("tests.fakes.fake_llm_provider")          # fakes/fake_llm_provider.py
finally:
    sys.path.remove(str(HERMES))
    for k in [k for k in sys.modules if _ours(k)]:
        sys.modules["hermes_tests" + k[len("tests"):]] = sys.modules.pop(k)
    sys.modules.update(saved)


def _get(module, name):
    try:
        return getattr(module, name)
    except AttributeError:
        raise RuntimeError(f"Hermes test kit shape changed: {name}") from None


GatewayUnderTest = _get(_helpers, "GatewayUnderTest")  # _helpers.py:103: a real `hermes gateway run` child
hermetic_env = _get(_helpers, "hermetic_env")  # _helpers.py:42: strips real SLACK_*/HERMES_*/*_TOKEN, applies extra
Director = _get(_helpers, "Director")  # _helpers.py:66: scripts the fake model per [in:<token>]
SlackDriver = _get(_drv, "SlackDriver")  # _drv_slack.py:36: stand-in tokens, gateway env and config
SLACK_SHIM = _get(_drv, "_SHIM") / "sitecustomize.py"  # _drv_slack.py:26: points slack_sdk at the stand-in
SlackStandin = _get(_slack, "SlackStandin")  # slack_standin.py:56: the real adapter's Slack, read back after edits
Call = _get(_standin, "Call")  # _standin.py:26: one request served (method, params, at, response)
wait_until = _get(_standin, "wait_until")  # _standin.py:59
FakeLLMServer = _get(_llm, "FakeLLMServer")  # fake_llm_provider.py:122
ToolCall = _get(_llm, "ToolCall")  # fake_llm_provider.py:58
Text = _get(_llm, "Text")  # fake_llm_provider.py:44
