# Profile-safe test setup: design

## Problem

Bean ran the installed `tests/test_core.py` and `tests/test_runs.py` with Hermes's interpreter and got 84 failures and
8 errors. Reproduced in scratch on a copy of the install:

| Run (Hermes `.venv` python, installed copy) | Result |
|---|---|
| `HERMES_PROFILE=bean`, no PYTHONPATH | 84 failed, 115 passed, 8 errors (Bean's run 1) |
| `HERMES_PROFILE=bean`, `PYTHONPATH=<hermes root>` | 72 failed, 135 passed (Bean's run 2) |
| no profile pin, `PYTHONPATH=<hermes root>` | 207 passed |

There are two causes. Neither is a production fault.

1. **The sandbox leaks the launching profile's environment.** The `sandbox` fixture in `tests/conftest.py` replaces
   `HERMES_HOME` and `HERMES_KANBAN_HOME` but keeps every other inherited `HERMES_*` variable. Hermes's
   `current_profile_name` uses `HERMES_PROFILE_NAME`/`HERMES_PROFILE` ahead of the home
   (`hermes_cli/profiles.py:2054-2072`). Inside Bean, the installed core therefore subscribes as `bean`, while
   the fakes read back the sandbox home's own identity, `default`. A custom `HERMES_HOME` is its own root, so it
   resolves to `default` (`hermes_constants.get_default_hermes_root`). The same leak carries `HERMES_KANBAN_DB` and
   `HERMES_KANBAN_BOARD` (`hermes_cli/kanban_db.py:464,571`). Once Hermes imports, `core.board_db()`
   (`muster/core.py:74-83`) would resolve a test's board to the live database.
2. **Hermes's dev venv cannot import Hermes's newer root modules.** The `.venv` editable install has a static
   module map, generated before `hermes_yaml.py` existed, so `import hermes_cli.profiles` raises
   `No module named 'hermes_yaml'`. Hermes's own launcher (`.hermes/bin/hermes`) doesn't use that map: it puts the
   checkout root on `sys.path`.

On main, one more test depends on the environment. `test_gateway_up_reads_hermess_runtime_status` (from 1616ae5)
recurses when real Hermes is importable: the stubbed `get_hermes_home` calls `config.data_dir()`, which calls
Hermes's real `plugin_data_dir`, which calls the stub again. It fails on main under Hermes's interpreter with or
without PYTHONPATH (1 failed, 1324 passed).

Two failures in the full installed copy are unrelated to profiles, and this fix doesn't touch them:

- The install ships main's multiplex `decisions.gateway_up` but its older test, so that test fails with
  `TypeError` (the lambda takes 0 arguments). Main's test, with the fix below, matches the installed code.
- `test_prompts` runs `git ls-files`, which fails in a code-only, non-git copy.

The README's command `python3 -m pytest -q` doesn't work on this machine: system `python3` has no pytest
(`docs/learnings.md:5`).

## Approach (tests and docs only)

- **`tests/conftest.py`, `sandbox`:** delete every inherited `HERMES_*` variable before setting the sandbox's own.
  Hermes then derives identity natively from the sandbox home (`default`). There is no pin and no mock, so tests
  that check another profile set their own pin, as Bean's `test_notifier_profile.py` already does. This file is
  byte-identical in the install, so the same fix applies there.
- **`tests/conftest.py`, top:** when the interpreter has Hermes (`importlib.util.find_spec("hermes_cli")`), append
  that checkout's root to `sys.path`, the same root Hermes's own launcher uses. No path is hard-coded. When Hermes
  is absent or installed normally, this changes nothing. Appending, rather than prepending, keeps Muster's own
  `tests` and `muster` packages first.
- **`README.md` Development:** name the interpreter as Hermes's own convention does (`HERMES_PYTHON`, Hermes
  `CONTRIBUTING.md` "Run tests"):
  `"${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}" -m pytest -q`.
- **`test_gateway_up_reads_hermess_runtime_status`:** compute the home once before stubbing, so the stub doesn't
  call back into itself. The test's assertions stay the same.
- **Regression test** (`tests/test_sandbox.py`):
  - Inside the sandbox, real `hermes_cli.profiles.current_profile_name()` returns `default`, and a test-set pin
    returns `bean`. This covers native selection and is not mocked.
  - A subprocess runs that test with `HERMES_PROFILE=bean`, `HERMES_PROFILE_NAME=bean` and a bogus
    `HERMES_KANBAN_DB` in its environment, and must pass. This proves the suite doesn't depend on the shell's
    profile.
  - When Hermes isn't importable, the test skips with a reason. This is not a skip of the failing cases.

## Cut, and why

- **Repairing Hermes's venv** (re-running its editable install): this changes the live install, which the brief
  forbids. It would also go stale again with the next new root module, so I'll list it only as an optional
  activation note.
- **Using `hermes --run-module pytest` as the native runner:** tried in scratch. With a temporary home it builds a
  1 GB dependency environment per data root, which is too heavy for a test command.
- **A global `HERMES_PROFILE_NAME=default` pin in the sandbox:** this would hide native profile selection, and
  the brief rejects it.
- **Rewriting the hardcoded `"default"` read-backs in `test_core.py`/`test_runs.py`:** once the leak is gone,
  they're correct for the sandbox home's real identity, so the two files keep their meaning unchanged.

## Decision needed

Approve this approach: clear the inherited `HERMES_*` environment, put the Hermes checkout root on `sys.path` in
conftest, document the `HERMES_PYTHON` command, and fix the one recursing test on main.
