#!/bin/bash
# muster-tick.sh — `hermes cron create "every 1m" --no-agent --script muster-tick.sh --name muster-tick`
exec "${HERMES_BIN:-$(command -v hermes || echo "$HOME/.local/bin/hermes")}" muster tick
