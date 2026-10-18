"""channels: where muster talks to the human, one module per platform, picked by `notify_platform`'s name.

A channel module is a peer of the others and never imports muster.gateway; the neutral helpers and the answer hub
they share are in muster.channels.shared. The contract every channel module meets (docstring only, no base class;
tests/test_channels.py is its test):

- `name`: Hermes's platform name. `durable_prompts`: True when a sent prompt stays answerable across a gateway
  restart (Slack: muster reads the request file on each tap), False when each boot presents open requests again
  (Telegram: Hermes keeps its prompts in memory).
- `register(ctx) -> bool`: wire the Hermes surfaces; False when this Hermes lacks them.
- `validate(settings) -> list[str]`: the names of missing or invalid settings; `config.require` joins them.
- `home() -> str`: the chat used when `notify_chat_id` is empty.
- `async destination(req) -> (ready, meta)`: where the request's run talks now, lifecycle-aware.
- `async present(req, meta, questions) -> Sent`: post only the question indexes given.
- `async present_approval(req, meta) -> Sent`. `Sent = {"messages": {str(n): [new ids]}, "failure": str | None}`.
- `async outcome(req) -> bool`: edit every message of an ended request to `req["outcome"]`.
- `async edit(chat, mid, text)`: only on a channel without durable prompts, for a superseded message.
- `release(rid)`, `release_ended(live)`, `track(req)`: the in-process state of a request (no-ops on Slack).
- `async reply(event) -> dict | None`: pre_gateway_dispatch; {"action": "skip"} when muster took the message.
- `async steps()`: one lifecycle pass over the conversations (topics, threads).
- `async show(ledger, line, saved) -> dict | None | False`: the run's one status message (muster.progress): post
  it once or edit it; {mid, thread} once shown, None when it cannot be shown yet (nothing sent), False when the
  platform refused (tried again after progress.RETRY). Raises only when a post's outcome is unknown.
"""

import importlib

from ..config import ConfigError

NAMES = ("telegram", "slack")


def get(name):
    if name not in NAMES:
        raise ConfigError(f"muster: notify_platform {name!r} is not one of {', '.join(NAMES)}")
    return importlib.import_module(f"{__name__}.{name}")
