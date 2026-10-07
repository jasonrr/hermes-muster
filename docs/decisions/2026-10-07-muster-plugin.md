# Design: muster — open-source Hermes plugin from rc-intake (approved 2026-10-07)

Copy of design s_dlypbrb33v3s1 from the hermes-rc-engineering-intake store. Approved by Jason 2026-10-07. Name: muster.

## Problem
rc-intake works, but it is one person's cron scripts full of RC constants. Goal: a real Hermes plugin anyone with GitHub + herdr can install. Claude Code first; a short path to codex/pi/hermes. Pitch: the opposite of a generic bot — local, your own agent CLI, one named human approves, and a per-repo production note carries project memory between runs.

## Prior art (verified 2026-10-07)
- Hermes catalog: nothing does label → coding agent.
- herdr-factory (github.com/razajamil/herdr-factory): strong overlap, herdr plugin, pre-beta, no license. Lacks approver gate, Hermes kanban, Telegram, per-repo memory. Name it in README as a peer.
- Sortie (sortie-ai/sortie): label → agent, no herdr, no interactive pane. Weak overlap.

## Chosen approach
Public repo `hermes-muster` (MIT), plugin name `muster`, Python stdlib only.
- `plugin.yaml`: name, version, description, manifest_version 2, license, requires_hermes >=0.21.5, config_schema.
- `__init__.py` `register(ctx)`: `ctx.register_cli_command("muster", ...)` → `hermes muster tick | launch | hook | recover | cleanup | flush`; `ctx.register_skill` for `skills/escalation` and `skills/run`. No tools, hooks or capabilities.
- Scheduling = `hermes cron create "every 1m" --no-agent --script muster-tick.sh` (script under $HERMES_HOME/scripts, `exec hermes muster tick`). Same for cleanup every 5m.
- Kanban = `hermes kanban` CLI via subprocess. State in `$HERMES_HOME/plugin-data/muster/`.
- Config in `plugins.entries.muster.settings`: label, bug_label, approver_login, approver_id, repos, clone_root, board, agent_kind, model, branch_prefix, notify_*, gh_config_dir, notes_dir, workflow_prompt_file, worktrees.
- Per-repo memory: `notes_dir/<owner>__<repo>.md`, six headings (Platforms; Where production runs; How to read config and logs; Tools the worker image needs; Read-only credentials to provision; Open questions). Pasted into the brief.
- Tally peeled out; its ideas live in `prompts/workflow.md` (13 numbered rules; see plan:muster Task 7). `workflow_prompt_file` overrides.
- Agent adapter seam: herdr already starts/prompts/waits 24 agent kinds. v1 ships `muster/claude.py` only; `get_adapter(kind)` refuses anything else. v1.1 adds one module per kind; hookless CLIs fall back to `herdr agent wait --until blocked|done` from the tick (experimental).

## Cut
GitHub Project status move, Sentry auto-approve, AUTO_REPOS, tally todo and dev-loop gate, bot gh identity as a requirement, adapter base class, Docker leftovers.

## Catalog rules
Public https repo; `hermes plugins validate .` passes; no network or subprocess at import/register time; README discloses gh/git/herdr shell-outs, interactive panes, gh login use, worktree writes; `register_cli_command` is skipped under `plugins.isolation: host`.

## Deviations

- Tests run from a project `.venv` (system python3 has no pytest); `.venv/` gitignored. (e77b87e)
- Task 2: `claude.is_ours` returns a bool; core's `check_agent` still raises the refusal. Issue runs register the same seven hooks as ad-hoc runs, including Stop, so `hook stop` without `--card` is a no-op. (c45ad9f, a563da7)
- Task 3: OWNER_FILE renamed `muster-launch-owner.json`; `is_bug()` replaces the tally skill routing; `board_exists()` (`hermes kanban boards list --json`) runs after the tick lock; the pane env carries an absolute `GH_CONFIG_DIR` and `HERMES_KANBAN_HOME` only when set and different; recover recomputes the whole pane env. (d36632d, abca6df)
- Task 4/1: `hook done` exits 1 on a config error or unexpected exception; the CLI catch-all had turned a failed `done` into a false 0. (7a10b38)
- Task 5: ad-hoc panes get `core.pane_env()`; reconcile checks agent kind only (as the source did), not name+cwd; rc_run's `status` subcommand not ported; `launch_run` defaults the model from config. (ce1da60, 42cba46)
- Task 6: cleanup claims a prefixed worktree only if it carries `muster-card.json` (a user's own `muster/foo` branch made every run exit 1); `branch_prefix` must be a non-empty string. (0bfb367)
- Task 7: a third cron script, `muster-flush.sh` (every 1m) — the source crontab ran `rc_run.py flush` every minute and the plan omitted it. (c0d111a)
- Task 8: setting `model` renamed `agent_model` — Hermes rejects plugin setting keys whose first segment is reserved (`model`, `plugins`, `security`, `settings`). A no-context `hook notification` writes no events.log line (by design). (2d3aceb)
- Review: config.require validates `agent_kind` and `branch_prefix` as a git ref prefix; notify ids stringified (`notify_target`); repo and label comparisons case-insensitive; label URL-quoted; one `core.READY`; a stale empty wait marker (>120 s) is reclaimed. (d984473)
- Review: `prepare_env` no longer pins `HERMES_KANBAN_HOME` to `HERMES_HOME` (Hermes shares one board root across profiles; pinning forked the board from the gateway), and `board_db()` asks Hermes' own `kanban_db_path` when running inside Hermes. (70d163a)
- Parked follow-ups (tally, tag `review:muster`): issue-run outbox (t_dlyswe6y4mpcp, p2), base branch hard-coded to main (t_dlyswe7pi4bsq, p2), ad-hoc agent-name collision (t_dlyswe89tmfkr, p3), p3 hardening batch (t_dlyswe8u0pl4s).
