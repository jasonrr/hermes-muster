# Slack acceptance harness

Design: `docs/plans/2026-10-20-slack-acceptance-design.md`. Plan: `docs/plans/2026-10-20-slack-acceptance.md`.

## Test interpreter (once)

```sh
export HERMES_AGENT_DIR=~/.hermes/hermes-agent PYTHONDONTWRITEBYTECODE=1  # never write .pyc into the checkout
PY=~/.cache/muster-acceptance/venv/bin/python
S=$(mktemp -d); git -C "$HERMES_AGENT_DIR" status --ignored --porcelain > $S/before; touch $S/stamp
UV_PROJECT_ENVIRONMENT=~/.cache/muster-acceptance/venv uv sync --project "$HERMES_AGENT_DIR" --frozen --no-install-project --group dev --extra slack
echo "$HERMES_AGENT_DIR" > ~/.cache/muster-acceptance/venv/lib/python*/site-packages/hermes-checkout.pth
$PY -c "import pytest, aiohttp, slack_bolt, slack_sdk, hermes_yaml, hermes_cli"
(cd "$HERMES_AGENT_DIR" && $PY -m pytest -p no:cacheprovider tests/e2e/core/platforms/test_slack_contract.py -q -x)
git -C "$HERMES_AGENT_DIR" status --ignored --porcelain | diff $S/before -
find "$HERMES_AGENT_DIR" -newer $S/stamp -not -path '*/.git/*' -not -path "$HERMES_AGENT_DIR/.git" -print
```

The last two must print nothing. The contract test refuses to start while a production gateway runs under launchd:
Hermes gives its temp home the bare service label (see `hermes_kit.py`). The harness's own rig avoids that.

## Runs

```sh
python3 -m pytest -q                                   # ordinary suite: the shim and gate unit tests only
MUSTER_ACCEPTANCE=1 $PY -m pytest tests/acceptance -q  # the lifecycle against a real gateway, offline
$PY tests/acceptance/gate.py                           # before installing: only PASS proceeds
```
