# Operating profile — plan

Design: `docs/plans/2026-10-22-operating-profile-design.md`, approved as option 2 (text field only, no upstream
draft). Branch `feat/configurable-operating-profile`.

**Interpreters.** Two run the suite:
- `uv run --group dev pytest -q` has no Hermes. The fake-module tests run; the real-Hermes tests skip.
- `~/.cache/muster-acceptance/venv/bin/python -m pytest -q -p no:cacheprovider`, with
  `PYTHONDONTWRITEBYTECODE=1`, has Hermes, so everything runs.

On main, `tests/test_decisions.py::test_gateway_up_reads_hermess_runtime_status` already fails in the Hermes
venv only. It stubs `hermes_constants.get_hermes_home` with `config.data_dir()`, which calls the real
`plugin_data_dir`, which calls the stub back, and that recursion fails the test. It is pre-existing and not
in scope.

**Patterns to follow:**
- Optional Hermes imports with an `ImportError` fallback: `core.board_db()`, `config.data_dir()`.
- Faking a Hermes module in tests: `tests/test_gateway_readiness.py` (the `runtime` fixture).
- Real-Hermes tests use `pytest.importorskip("hermes_cli")`.
- Config validation errors are `config.ConfigError` raised from `config.require()`, which tick, launch,
  recover, recommend and cleanup call first.

## Task 1: the setting and the resolver

- **Files:** `plugin.yaml`, `muster/config.py`, `muster/core.py`, `tests/test_operating_profile.py` (new).
- **Exists because:** the brief asks for a UI-selectable owner, never hardcoded. Hermes's native form renders
  `config_schema` (`hermes_cli/plugins_settings.py`), and a profile can only be checked, not routed to
  (design).
- **Test first** (fake `hermes_cli.profiles` module with `current_profile_name(default)` and `profile_exists(name)`):
  - Empty setting returns the running profile (`bean` and `default`).
  - Setting equal to the running profile returns it.
  - An unknown profile raises `ConfigError` naming it.
  - A name other than the running profile raises `ConfigError` naming both.
  - A non-string value raises.
  - Without Hermes (no module), the running profile is `"default"`.
  - `config.require()` raises the mismatch.
- **Code:**
  - `config.DEFAULTS["operating_profile"] = ""`.
  - plugin.yaml: `operating_profile: {type: str, default: "", label: Operating profile, description: ...}`.
  - `core.operating_profile()`:
    - Running = `current_profile_name("user") or "user"`; that is the native notify-subscribe default
      (`hermes_cli/kanban.py:1171`). Without Hermes, `"default"`.
    - Chosen = the setting. If chosen is set, it must be a str, exist (`profile_exists`) and equal running,
      else raise `ConfigError`.
    - Returns running.
  - `config.require()` calls it via a local import, as it does for `channels` and `claude`.
- **Verify:** `uv run --group dev pytest -q tests/test_operating_profile.py tests/test_config.py`.

## Task 2: route every identity path through it (the cutover, reworked)

- **Files:** `muster/core.py`, `muster/launcher.py`, `muster/hold.py`, `tests/test_operating_profile.py`.
- **Exists because:** this is the cutover patch (`hook_cmd`, `hermes_home`, `subscribe`) plus the live
  `pane_env` change the patch missed. Without these, a multiplexed served profile's panes and
  subscriptions run as the host's `default`.
  - Human-facing `recover` text: a human running it without `-p` hits the sticky active profile, which may
    be another muster's state.
- **Test first:**
  - Fake profiles module with running `bean`:
    - `hook_cmd()[1:3] == ["-p", "bean"]`.
    - `subscribe()` passes `--notifier-profile bean` and refuses a read-back stamped `default`.
    - hold's recover text contains `-p bean`.
  - Running `default`: `hook_cmd() == [hermes, "muster", "hook"]` and `--notifier-profile default`. The
    existing goldens are unchanged.
  - A mismatch raises in `subscribe()` before any `kanban` call (the fake records zero calls).
  - Real Hermes (importorskip), as in the backup's `test_multiplex_launch_owner`:
    - Root and `profiles/bean`, with a home override set to bean and the process `HERMES_HOME` at root.
    - `core.hermes_home() == bean`.
    - `pane_env()[:2] == [HERMES_HOME=bean, HERMES_KANBAN_HOME=root]`.
    - `hook_cmd` has `-p bean`, and `subscribe` stamps `bean`.
    - With `operating_profile: bean` it passes; with `default` it raises.
- **Code:**
  - `core.hermes_cmd()` = `[config.hermes_bin(), *(["-p", p] if (p := operating_profile()) != "default" else [])]`.
  - `hook_cmd()` = `[*hermes_cmd(), "muster", "hook"]`.
  - `hermes_home()` uses `get_hermes_home()`, falling back to `os.environ`.
  - `subscribe()` computes `owner = operating_profile()` before the first `kanban` call and uses it for the
    flag and the read-back.
  - `launcher.pane_env()`:
    - `home = core.hermes_home()`.
    - The board home comes from `hermes_cli.kanban_db.kanban_home()`; without Hermes, fall back to today's
      env rule.
    - Add `HERMES_KANBAN_HOME` when it differs from the home.
  - hold.py lines 168 and 221 use `' '.join(core.hermes_cmd())`.
- **Verify:** `uv run --group dev pytest -q tests/test_operating_profile.py tests/test_core.py tests/test_hold.py tests/test_runs.py`,
  then the same files under the Hermes venv.

## Task 3: no side effects from a change, readiness, and the native UI

- **Files:** `tests/test_operating_profile.py`.
- **Exists because:** the brief's build checks 3–5 and the UI check 1.
- **Tests:**
  - **Mismatch before side effects.** Seed `decisions/`, `decisions/archive/`, `intake/*/launch.json` and
    `logs/` under `config.data_dir()`, and subscriptions in the fake board. Set a mismatching profile. Then
    `intake.tick(dry_run=False)` and `adhoc.launch` raise `ConfigError` with zero `core.run` calls.
    `core.subscribe` raises with zero kanban calls. A sha256 of every file under the data dir is
    identical before and after.
  - **Readiness with shared Slack.** Reuse `test_gateway_readiness`'s `runtime` fixture shape. With
    `operating_profile: bean` and running `bean`, the served multiplexer's shared Slack makes
    `gateway_up()` True.
  - **Native UI (real Hermes, importorskip).** Isolated `HERMES_HOME`:
    - It contains `config.yaml` with `plugins: {enabled: [muster]}` and `plugins/muster` as a copy of
      the repo's `plugin.yaml`, `__init__.py`, `muster/` and `prompts/`.
    - Call the Desktop RPC handlers in `tui_gateway/methods_tools.py`: `_plugin_rows()` must list
      `operating_profile` as `type: string` with its label and default `""`.
    - `_plugins_settings(rid, {"key": "muster", "values": {"operating_profile": "bean"}})` must report it
      as written.
    - The isolated `config.yaml` must then hold `plugins.entries.muster.settings.operating_profile: bean`,
      and a fresh `_plugin_rows()` must show the value `bean`.
    - Report this as observed at the RPC boundary. The Desktop's rendering of a string field is Hermes's
      own `plugin-settings.test.tsx`.
- **Verify:** both interpreters, as in Task 2.

## Task 4: docs

- **Files:** `docs/setup.md`, `docs/reference.md` (if it lists settings), `docs/learnings.md`.
- **Exists because:** the brief requires the setting, scope, lifecycle and reload, and rollback to be
  documented.
- **Content:** one table row, plus a short "Operating profile" section:
  - What it checks; that it never moves muster.
  - Reload: commands at their next run; the gateway at restart.
  - Rollback: clear the field.
  - Owner moves need a separate approved handoff.
  - Learnings: one bullet on the per-profile plugin and state model.
- **Verify:** `git diff --stat`; read the rendered section.

## Final

- Full suite under both interpreters.
- `MUSTER_ACCEPTANCE=1 $PY -m pytest tests/acceptance -q` (offline, temp homes). `gate.py` reads the live
  `config.yaml`, so it runs only if it can be pointed at an isolated home; otherwise report it as not run, and why.
- Fresh review, then the PR with an Activation section.

## Plan review (two passes by a fresh sub-agent) and what changed

| Finding | Severity | Outcome |
|---|---|---|
| A `ConfigError` from `subscribe()` inside a hook or `events.py:174` could leave half-done work; `require()` never runs for hooks | p2 | Fixed: the check moved to the one seam every command and hook passes, `cli.main`, right after `config.load`. A hook exits with its usual failure code and runs nothing. `subscribe()` keeps it as a backstop before any kanban call. |
| `current_profile_name` returns `custom` for a home outside Hermes's profiles; `-p custom` would break hooks | p1 | Fixed: `hermes_cmd()` pins `-p` only for an existing named profile. Test `test_a_custom_home_is_never_pinned`. The notifier stamp stays what Hermes's own notify-subscribe stamps. |
| A shell's `HERMES_PROFILE_NAME`/`HERMES_PROFILE` would rename the test home's profile under real Hermes | p2 | Fixed: conftest unsets both. |
| The readiness test would pass with or without the feature | p1 | Fixed: it asserts that readiness asked about `core.hermes_home()`, the home the setting confirms. It reuses PR #68's `runtime` fixture. |
| The UI test would import the private `tui_gateway` `_plugin_rows`/`_plugins_settings` | p1 | Fixed: the committed test uses the public `plugin_settings_fields`/`save_plugin_settings` those RPCs call. The RPC itself is exercised once by hand in an isolated home and reported in the PR, not committed. |
| Real-Hermes test: `HERMES_KANBAN_HOME` is pinned by conftest | p2 | The test sets it to the root, as the shared board home is. |
| `hold.wire()` (the pane agent's duplicate handoff) keeps bare `hermes` | p3 | Kept: the pane's `HERMES_HOME` (now the served profile's) carries it. `kanban()` stays unpinned: the board root is shared, and the owner is stamped explicitly. |
| Snapshot hashing of the data dir would see new `logs/` | p2 | The mismatch test stops in `cli.main` before `start()` makes any log or lock file. The snapshot is taken after seeding. |

## Final review and what changed

| Finding | Severity | Verdict | Outcome |
|---|---|---|---|
| The gateway never passes `cli.main`; a mismatch would surface mid-action after a card exists | p2 | CONFIRMED (trace) | Fixed: `shared.ensure_config` (every tap and reply check, all fail closed) and both channel factories check first. A mismatch starts no scan and registers no handler. Test `test_the_gateway_refuses_a_mismatch_before_any_handler_or_scan`. |
| Hook events during a mismatch are dropped, not queued | p2 | PLAUSIBLE | Documented in setup.md. Queuing them would re-deliver later, which is the replay the brief forbids. |
| An env pin (`HERMES_PROFILE_NAME`) could name a profile the home does not, stamping subscriptions no gateway delivers | p3 | CONFIRMED (probe) | Fixed: the resolver uses `get_active_profile_name()`, the home-derived name the gateway itself gives its notifier (`gateway/run.py` `_active_profile_name`). This also removes the `"user"` fallback. |
| `' '.join` in the recover text | p3 | CONFIRMED | Fixed: `shlex.join`. |
