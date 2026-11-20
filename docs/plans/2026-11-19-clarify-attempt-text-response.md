# Typed clarify replies through `attempt_text_response` (#83)

Approved design: option A. NousResearch/hermes-agent#156130 (merged) deletes
`tools.clarify_gateway.resolve_text_response_for_session` and `attempt_text_response_for_session`. muster's Telegram
typed-reply path (`muster/channels/telegram.py`, `reply()`) calls the first one twice, so after the next Hermes
release a typed answer raises `AttributeError`. Switch to `attempt_text_response(clarify_id, response)`, the
per-question function Hermes's own gateway calls (upstream `tools/clarify_gateway.py:232`; local checkout line 245),
compared against `clarify_gateway.TEXT_RESOLVED`. Coercion (number → label, "1,3" multi-select, free text once
awaiting text) is unchanged, because both functions run the same `_attempt_text_response`.

Nearest pattern: the existing calls in `reply()` (`get_pending_for_session`, `mark_awaiting_text`) and their fakes in
`tests/fake_hermes.py:clarify_module`. Verify all with `"${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}" -m pytest -q` (the README's command; the repo has no linter configured).

## Task 1: fake Hermes matches upstream (test first)
- Files: `tests/fake_hermes.py`.
- Step: in `clarify_module`, replace `attempt_text_response_for_session` / `resolve_text_response_for_session` with
  `attempt_text_response(clarify_id, response)`: look up the entry by id in `mod._entries` under `mod._lock`, as
  upstream's `get_pending` does (unknown, or `event.is_set()` → `TEXT_NO_PENDING`), then coerce/resolve exactly as
  the removed fake did. Remove the two removed names from the module so a call to them fails. Update the module docstring if it names them.
- Verify (expect failures now): `"${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}" -m pytest -q tests/test_gateway.py -k "reply or awaiting or other"`
  fails with `AttributeError ... resolve_text_response_for_session`.
- Exists because: without the fake losing the removed names, the suite passes against code that crashes on new Hermes.

## Task 2: telegram.py calls `attempt_text_response`
- Files: `muster/channels/telegram.py` (`reply()`).
- Step: bound branch: after `mark_awaiting_text(entry.clarify_id)`, use
  `clarify_gateway.attempt_text_response(entry.clarify_id, text) == clarify_gateway.TEXT_RESOLVED`. Unbound branch:
  collect the pending entries (not session keys) from `get_pending_for_session(key)`; when exactly one, resolve it
  by its `clarify_id` the same way.
- Verify: `"${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}" -m pytest -q tests/test_gateway.py` passes.
- Exists because: these two lines are the `AttributeError` the issue reports.

## Task 3: the private-shape test checks the new name
- Files: `tests/test_hermes_private.py` (`test_the_clarify_functions_the_gateway_calls_exist_with_their_arguments`).
- Step: replace the `resolve_text_response_for_session` entry with `("attempt_text_response", ["clarify_id",
  "response"])`, and assert `TEXT_RESOLVED = "resolved"` appears in the source.
- Verify: `"${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}" -m pytest -q tests/test_hermes_private.py -k clarify_functions` passes against `~/.hermes/hermes-agent`;
  full suite `"${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}" -m pytest -q` passes, apart from failures that also occur on unchanged origin/main.
- Exists because: it is the loud failure if Hermes reshapes the function muster now depends on.
