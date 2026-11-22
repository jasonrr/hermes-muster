# Standalone muster

## Problem

Muster only runs inside Hermes. Its command is `hermes muster`, its settings come from the plugin context, its
ledger is `hermes kanban`, its decisions are shown only by the Hermes gateway (Telegram, Slack), and its timers are
Hermes cron jobs. MausBot, Claude Code and Codex cannot start or direct a run, and a machine without Hermes cannot
run muster at all. Jason's decision (2026-11-21): Hermes becomes optional, one host among several.

## Chosen approach

Muster is a `muster` command with two backends, chosen by how it is started. `hermes muster` keeps today's
behaviour unchanged. `muster` alone uses files only. Any harness that can run a shell command can use it.

1. **Command.** `[project.scripts] muster = ...` in `pyproject.toml`. Both entries call the same `cli.main`.
   `core.hermes_cmd()` becomes the one place that says how muster is called (`muster`, or `hermes [-p x] muster`);
   pane hooks, briefs and prompts take it from there (`$hermes muster` in prompts becomes `$muster`).
2. **Settings.** Standalone reads `$MUSTER_HOME/config.toml` (default `~/.muster`, stdlib `tomllib`), same keys and
   same validation. Data lives in `$MUSTER_HOME/data`. `require()` asks for the `hermes` binary only on the Hermes
   backend.
3. **Ledger.** `core.kanban()` is already the one seam (about 55 callers). Standalone routes it to a new
   `muster/ledger.py`: one JSON file per card, a lock, the same verbs and the same JSON as `hermes kanban`
   (create, show, comment, block, unblock, complete, archive), plus `last_event` and `block_kind` in place of the
   two sqlite reads. No caller changes. `tests/test_kanban_contract.py` runs against both, so the two cannot drift.
   The notify verbs do nothing standalone: nobody is subscribed, and an event counts as delivered.
4. **Decisions.** `muster decisions list|show|answer` (JSON with `--json`). `answer` calls the existing
   `decisions.answer_part` / `transition`, then `decisions.execute`. The pane bridge already polls the file, so
   questions and permission prompts work with no gateway. `list` also names the cards that wait for a review, which
   is how the calling agent learns it must run `muster recommend`.
5. **Housekeeping the gateway did.** The channel-free part of `gateway.scan_loop` (settle designs, stale dead
   requests, recover after a crash) moves to a function that `muster flush` also runs.
6. **Coordinator.** The three skill files in `prompts/` are plain markdown with frontmatter. Docs say where to put
   them for Claude Code, Codex and MausBot. `README.md` and `AGENTS.md` change from "Hermes plugin" to "tool with a
   Hermes host".

Trade-off, stated plainly: this adds a second ledger implementation (about 200 lines) that must match Hermes
kanban. The shared contract test is the control. Without it, standalone is impossible, because the ledger is the
largest Hermes need.

## Cut and why

- **Telegram and Slack without Hermes.** They are Hermes adapters. Standalone decisions go through the calling
  harness. Add when someone runs muster with no Hermes and wants chat buttons.
- **A daemon or `muster wait`.** `tick`, `flush` and `cleanup` stay one-shot commands; docs give one cron line. The
  harness polls `muster decisions list`. Add a blocking wait when polling proves too slow.
- **An LLM for the duplicate check.** It already launches without the check when `ctx.llm` is absent.
- **A skills installer command.** Copying three files is one documented line.
- **Secret redaction standalone.** `core.display` uses Hermes's redactor when present, else shows the text as is:
  it goes to the local harness that already ran the command, not to a chat.
- **Moving data between backends.** A run stays on the backend it started on.

## Open questions

- MausBot: confirm where a bot's skills live, and whether its bots can reach a `muster` on `PATH`.
- Name: keep the repository name `hermes-muster`, or rename it.

## Deviations

- T1 commit 5823e50, e03d97a — deviation: review fixes (unreadable config.toml is a ConfigError; MUSTER_HOME made absolute so panes in worktrees find it); .gitignore gains *.egg-info/ from the editable install — parked: none
- T2 commit 0f0501b, 723cfc6 — deviation: review fix (block --kind restricted to the four kinds Hermes accepts); created_by stored as "" when omitted — parked: https://github.com/jasonrr/hermes-muster/issues/90
- T3 commit c9b915c, f6c242e — deviation: dropped the unused hermes= kwarg from adhoc.footer; review fix added standalone tests for the propose and recover text — parked: none
- T4 commit 1d16403, 9b3dcd8 — deviation: review fixes (rerun fills missing slots; a design-covered Approval question is hidden and refused, via new inbox.covered_slot since the channels keep that state in memory; bad cards skipped; failed execution exits 1; empty multi-select and unreadable show refused); list --json requests carry "tool". Known limit: a multi-question request whose Approval slot is covered cannot be answered from the CLI until the design request ends — parked: none
