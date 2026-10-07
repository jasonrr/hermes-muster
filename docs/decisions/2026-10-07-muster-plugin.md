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
