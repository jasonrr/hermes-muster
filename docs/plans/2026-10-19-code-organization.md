# Plan: architecture guide and clear module ownership (approved design: Proposal v1 6611b3c72640, card t_a86618e5)

Behavior-preserving move. No function is renamed and no body changes except qualifying a name with the module
it now lives in. Baseline: `env -u PYTHONPATH -u PYTHONHOME uv run --isolated --group dev python -m pytest -q`
→ 1194 passed, 1 skipped.

## Task 1: move code out of core.py and runs.py (one scripted move)
Files: `muster/core.py`, `muster/runs.py`, new `muster/launcher.py`, `muster/intake.py`, `muster/adhoc.py`.
- core keeps: shared names (CARD_FILE … PROVENANCE), `adapter`, `hook_cmd`, paths (`intake_dir`, `worktrees_dir`,
  `log_path`, `log`, `lock_path`, `hermes_home`, `board_db`), `SECRET`, `CommandError`, `LaunchError`, `run`,
  `kanban`, `prepare_env`, `board_exists`, `telegram_home`, `approver_entries`, `approvers`, `notify_target`,
  `subscribe`, `gh_config_dir`, `save_json`.
- launcher gets: `startup_prompt`, `pane_env`, `setup_trouble`, the whole "pane launch" section (AGENT_NAME …
  `ensure`), `pull_requests`, `block_kind`, `card_status`, `recoverable`, `report_failure`, `trouble`, `topic`.
- intake gets: `approval` … `approve`, `is_bug`, `production_note`, `approved_by`, `brief`, `card_argv`, `timeline`,
  `agent_settings`, `project_status`, `intake_known`, `links`, `relaunch`, `step_of`, `launch`, `start`, `recover`,
  `recover_card`, `intake`, `tick`; core's module docstring.
- runs keeps: UNACKED_AFTER, ERRORS, `runs_dir`, `run_dir`, `log_path`, `log`, `load`, `enqueue`, `pending`, `owned`,
  `sent`, `wait_card`, `last_event`, `acked`, `note`, `read_entry`, `drain`, `closed`, `flush`.
- adhoc gets: IDLE_AFTER, BRANCH, ORIGIN, `verify`, `complete`, `clear_dead_claim`, `deliver`, `reconcile`,
  `footer`, `relaunch`, `launch_run`, `launch`, `caller_brief`, `recover`, `hook`.
- Moved code reaches other modules' names through the module (`core.run`, `launcher.note`), found with
  `symtable` so locals named like globals (`plan`, `note`) are untouched. `runs.drain`/`flush` import adhoc lazily
  (existing pattern: events.py "lazy: runs imports events").
Verify: `ruff check --select F muster tests` clean; `python -c "import muster.cli"`.
Exists because: core and runs each mix shared machinery with one route's entrypoints.

## Task 2: callers, CLI map, tests
Rewrite `core.X`/`runs.X` (code, comments, `monkeypatch.setattr(core, "X")`) to the new module for every moved
X, add the imports, and map `cli.SUBCOMMANDS`: tick/recover → intake, launch → adhoc, flush → runs.
Verify: targeted tests (test_core, test_launch, test_runs, test_events, test_hold, test_decisions, test_progress,
test_prompts, test_startup_prompt, test_kanban_contract, test_cli), then the full suite.
Exists because: the moves break every caller and patch otherwise.

## Task 3: docs/architecture.md
Reading path, module map matching the final code, the three processes (CLI/cron, pane hook, gateway), state
locations and owners, how to trace the issue route, the ad-hoc route and one hook. Link from README "Development"
and "Repository docs". Verify: every module and path named exists (`grep`).
Exists because: no newcomer reading path exists.
