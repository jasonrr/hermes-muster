#!/bin/bash
# muster-cleanup.sh — `hermes cron create "every 5m" --no-agent --script muster-cleanup.sh --name muster-cleanup`
exec "${HERMES_BIN:-$(command -v hermes || echo "$HOME/.local/bin/hermes")}" muster cleanup
