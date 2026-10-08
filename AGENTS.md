# hermes-muster: agent rules

## Be a good Hermes plugin citizen

muster's goal is to write as little standalone code as possible. Reuse Hermes, so muster inherits what Hermes builds next.

- **Use Hermes's approved surfaces and APIs.** These are the plugin context, the documented hooks, `register_*` handlers, adapter methods, the `tools.*` services and `hermes kanban`. Use them even when our own version would be shorter or nicer. Before building any UI, queue, timeout, retry, state or message format, find the Hermes feature that already does it and use that.
- **Go further only when the approved surface makes the feature impossible.** Then:
  1. Put the workaround in `muster/hermes_private.py`, the only module that may touch a private Hermes name (a leading underscore, or internal state). Give each workaround a comment naming the Hermes file and line it relies on, and the upstream issue or PR that would make it unnecessary.
  2. Propose official support upstream: draft an issue or PR for Hermes describing the public API muster needs. Ask the human before filing anything; never file it unasked.
  3. Add a test that fails loudly when the private shape changes, rather than degrading silently.
- **Prefer the host's native feature everywhere.** That means Claude Code hooks and settings, herdr's CLI, `gh` and the standard library, before code of our own.
- **Never cut a stated requirement because the clean API is missing.** Build it on a segregated workaround and say so.
