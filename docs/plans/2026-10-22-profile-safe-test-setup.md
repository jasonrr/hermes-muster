# Profile-safe test setup: plan

The design is `docs/plans/2026-10-22-profile-safe-test-setup-design.md` (approved as proposal v1 82e110ec0bcf).

Every verify command uses `P="${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}"`, an explicit scratch
`TMPDIR`, and `env -u HERMES_KANBAN_DB`. Run them from the worktree root.

## Task 1: regression test (written first, must fail)

- **File:** new `tests/test_sandbox.py`. Follow the style of `tests/test_config.py`: plain functions with
  `monkeypatch`.
- **`test_sandbox_identity_is_hermess_native_default`:**
  - `pytest.importorskip("hermes_cli")`.
  - `from hermes_cli.profiles import current_profile_name`.
  - Assert the `HERMES_*` names in `os.environ` are exactly `{"HERMES_HOME", "HERMES_KANBAN_HOME"}`.
  - Assert `core.board_db()` lies under the sandbox (`tmp_path`).
  - Assert `current_profile_name("user") == "default"`.
  - Set `HERMES_PROFILE_NAME=bean` and assert it returns `bean`.
- **`test_suite_ignores_the_shell_profile`:**
  - Run `sys.executable -m pytest -q -p no:cacheprovider tests/test_sandbox.py::test_sandbox_identity_is_hermess_native_default`
    with `cwd=Path(__file__).resolve().parent.parent`.
  - Use `os.environ` plus `HERMES_PROFILE=bean`, `HERMES_PROFILE_NAME=bean` and
    `HERMES_KANBAN_DB=<tmp_path>/live-kanban.db`.
  - Assert the return code is 0, showing the child's output on failure.
  - The child's own asserts prove the pins didn't reach it. Checking whether the DB file exists would be vacuous:
    Hermes only computes that path and never creates it, as plan review confirmed.
- **Test first:** run `HERMES_PROFILE=bean $P -m pytest -q tests/test_sandbox.py`. It must fail before Task 2: an
  ImportError on `hermes_yaml`, or `bean != default`.
- **Exists because:** the brief requires explicit regression coverage for native profile selection, and proof
  that the run doesn't depend on the shell.

## Task 2: conftest fix

- **File:** `tests/conftest.py`.
- **Module level, after imports:** if `importlib.util.find_spec("hermes_cli")` has an origin, append
  `str(Path(origin).parent.parent)` to `sys.path` when it isn't there yet.
- **First lines of `sandbox`:** `for name in [n for n in os.environ if n.startswith("HERMES_")]:
  monkeypatch.delenv(name)`. This must come before the existing `setenv("HERMES_HOME", ...)`.
- **Verify:**
  - `$P -m pytest -q tests/test_sandbox.py` passes.
  - Under `HERMES_PROFILE=bean`: `$P -m pytest -q tests/test_core.py tests/test_runs.py` passes on source, and on a
    scratch copy of the install with this conftest.
- **Exists because:** of causes 1 and 2 in the design.

## Task 3: recursing gateway test

- **File:** `tests/test_decisions.py`, `test_gateway_up_reads_hermess_runtime_status`.
- **Change:** set `home = config.data_dir().parent.parent` before building the `hermes_constants` stub, and make
  the stub `lambda: home`.
- **Test first:** `$P -m pytest -q tests/test_decisions.py -k gateway_up_reads` fails with `RecursionError` before
  the change and passes after it.
- **Exists because:** this is the one failure on main under the documented interpreter.

## Task 4: docs

- **`README.md` Development:** replace `python3 -m pytest -q` with
  `"${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}" -m pytest -q`, plus one line saying why
  (Hermes's interpreter; the sandbox clears the inherited Hermes environment).
- **`docs/learnings.md`:** add one bullet under `## profile-safe-test-setup (2026-10-22)`.
- **Verify:** `git diff --check`.
- **Exists because:** the brief requires one clear documented command, and the repo convention requires a
  learning for each surprise.

## Final verification

- The full suite on source with `$P`, under no pin, under `HERMES_PROFILE=bean`, and with a temporary custom
  `HERMES_HOME`.
- `tests/test_core.py` and `tests/test_runs.py` together and separately, on source and on a scratch copy of the
  install with only the conftest swapped in.
- `hermes plugins validate .`
- `git diff --check`.
- No file under `~/.hermes` newer than the run's marker apart from live gateway/session activity. Check with
  `find -newer`.
