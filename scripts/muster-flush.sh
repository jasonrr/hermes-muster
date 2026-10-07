#!/bin/bash
# muster-flush.sh — `hermes cron create "every 1m" --no-agent --script muster-flush.sh --name muster-flush`
exec "${HERMES_BIN:-$(command -v hermes || echo "$HOME/.local/bin/hermes")}" muster flush
