# Architecture

This page shows how a piece of work moves through muster's code. The [README](../README.md) covers what muster does and how to configure it.

## The path of one run

1. **Approved work.** There are two routes in:
   - **Issue route:** the human labels a GitHub issue. The `tick` cron reads the label event and checks who applied it (`intake.approve`). It then creates one kanban card for that event (`intake.intake`).
   - **Ad-hoc route:** the Hermes agent runs `hermes muster launch` with a brief. That creates a card keyed `run:<repo>:<branch>` (`adhoc.launch_run`).
2. **Builder launch.** Both routes subscribe the card notify+wake to the human's chat (`core.subscribe`). Both then call the same launch engine, `launcher.ensure`. It creates a trusted herdr worktree, opens a tab, starts Claude with no task, and sends the brief as the first prompt. Each step is recorded before it runs, so `hermes muster recover <card>` resumes the launch where it stopped (`intake.recover`).
3. **Agent events.** The pane's Claude Code hooks run `hermes muster hook <event>` (`events.hook`); an ad-hoc pane passes `--card`, which goes to `adhoc.hook`. Every event is saved to the run's outbox first (`runs.enqueue`), then delivered in order (`runs.drain`). An issue run's events go through `events.replay`, an ad-hoc run's through `adhoc.deliver`. The `flush` cron (`runs.flush`) retries anything a hook could not deliver. For ad-hoc runs, it also notices hooks that never ran (`adhoc.reconcile`).
4. **Human decisions.** When the builder asks a question or needs approval, the hook records a request in `muster.decisions` (`bridge` waits on permission prompts). The gateway shows the request in Telegram or Slack (`gateway`, `channels`) and passes the human's answer back. A finished build or design gets the coordinator's recommendation (`hermes muster recommend` / `recommend-design`). The human's tap carries it out (`decisions.execute`).
5. **Verified completion.** A turn end alone is not success. An issue run's card completes when the builder runs `hook done <PR url>` and gh names the run's branch as that pull request's head (`events`). An ad-hoc run's card completes only when:
   - exactly one open or merged, non-draft pull request from its branch into its base has the worktree's HEAD as its head, and
   - no tracked file has uncommitted changes.

   `adhoc.verify` checks both conditions on the `stop` hook and on the flush, and `adhoc.complete` completes the card.
6. **Resource cleanup.** The `cleanup` cron (`cleanup.cleanup`) closes a finished workspace once its work is merged and its panes have been quiet. It first backs up anything that would be lost.

## Three processes

| Process | What starts it | Entry | Main modules |
|---|---|---|---|
| CLI / cron | `hermes muster <command>`: cron runs `tick`, `flush` and `cleanup`; the Hermes agent runs `launch`, `recover`, `recommend` and `open` | `cli.SUBCOMMANDS` | `intake`, `adhoc`, `runs`, `cleanup`, `decisions` |
| Pane hook | Claude Code in the builder's pane runs `hermes muster hook <event>`. The builder also runs `hook propose`, `done` and `duplicate` itself | `events.hook` → `adhoc.hook` with `--card` | `events`, `adhoc`, `runs`, `bridge`, `progress`, `launcher.prompt_seen` |
| Hermes gateway | loads the plugin at startup (`__init__.register`) | `gateway.setup`, `gateway.on_dispatch` | `gateway`, `channels/*`, `decisions`, `conversation`, `progress` |

The three processes share no memory. They communicate only through files under `<data dir>` (`$HERMES_HOME/plugin-data/muster/`), through the kanban board, and through Hermes's notifier.

## Module map

| Module | Owns |
|---|---|
| `cli` | The `hermes muster` subcommands and which module runs each |
| `config` | Settings, their validation, the prompt files, and `data_dir()` |
| `core` | Shared plumbing: the files muster writes, the data paths, the one subprocess seam (`run`, which tests replace), `kanban()`, the environment, the board check, the chat target and `subscribe()`, and `save_json` |
| `launcher` | The launch engine both routes use: the launch record (`plan`), `ensure` and its worktree, tab, agent and prompt steps, the launch lock, the prompt-seen evidence, and launch-failure reporting and `recoverable` |
| `intake` | The issue route: approval rules, the issue brief, the card, `launch`, `recover_card`, `intake` and `tick`, plus `recover`, which dispatches `hermes muster recover` for every kind of card |
| `hold` | Duplicate holds: a cheap model judges an automatic approval before it launches |
| `adhoc` | The ad-hoc route: `launch_run`, `recover`, its `hook --card`, and what its events mean (`deliver`, `reconcile`, `verify`, `complete`) |
| `runs` | Every run's directory `<data dir>/runs/<card>/` and its durable event delivery: `enqueue`, `drain`, gateway acks and `flush` |
| `events` | The issue route's hooks, wait cards, design proposals and their approval gate, and `replay` for issue-run outbox entries |
| `bridge` | The pane side of a permission prompt: record it, wait for the human's answer, then print it as the hook's decision |
| `decisions` | The request store (one JSON file per question, approval or build) and the actions a tap carries out |
| `gateway`, `channels/` | The gateway side: present requests in Telegram or Slack and take the answers |
| `conversation` | A run's topic or thread, and where its messages go |
| `progress` | The evidence behind each run's status message |
| `cleanup` | Close finished workspaces, after backing up unsaved work (Git preservation lives here) |
| `claude` | The Claude Code adapter: launch arguments, hook settings, and payload parsing |
| `hermes_private` | The only module that touches private Hermes names (see [AGENTS.md](../AGENTS.md)) |

## Where durable state lives

| Path | Written by | Holds |
|---|---|---|
| kanban board (`board` setting) | `intake`, `adhoc`, `events`, `runs`, `decisions` | One ledger card per run; wait and review cards; card comments such as the links and proposal versions |
| `<data dir>/intake/<card>/` | `intake`, `launcher`, `hold` | `launch.json` (the issue launch record), `launch.lock`, `prompt-seen.jsonl`, and a held duplicate's record |
| `<data dir>/runs/<card>/` | `runs`, `adhoc`, `events`, `progress`, `conversation` | `outbox/`, `sent/` and `lock` for every run. Only an ad-hoc run has `run.json` (its launch record is `run.json`'s `launch`), `brief.md`, `caller-brief.md`, `settings.json`, `launch.lock` and `prompt-seen.jsonl`. Every run has `proposals/`, `activity/`, `progress.json` and `conversation.json` |
| `<git dir>/muster-card.json`, `muster-brief.md`, `muster-launch-owner.json` | `intake`, `launcher` | The issue worktree's link to its card, the audit copy of its brief, and which launch owns the checkout |
| `<data dir>/decisions/` | `decisions`, `bridge`, `gateway` | One request per file; finished requests move to `archive/` |
| `<data dir>/workspaces/`, `<data dir>/backups/cleanup/` | `cleanup` | Analysis workspaces muster opened, and backups made before cleanup |
| `<data dir>/logs/` | all | Cron and hook logs, and the `tick` lock |

## Tracing

- **Issue launch:** `intake.tick` → `intake.intake` → `intake.launch` → `launcher.ensure`. Its record is `<data dir>/intake/<card>/launch.json`, and the cron's output says which step failed.
- **Ad-hoc launch:** `adhoc.launch` → `adhoc.launch_run` → `adhoc.relaunch` → `launcher.ensure`. Its record is `<data dir>/runs/<card>/run.json`.
- **Recovery:** `intake.recover` takes the tick lock, then routes the card. A card with `run.json` goes to `adhoc.recover`. A held card goes to `hold.release`. Anything else goes to `intake.recover_card`. Both routes resume through `launcher.ensure`, from the step the record names.
- **One hook:** `events.hook`, or `adhoc.hook` with `--card`, saves the event to `runs/<card>/outbox/`, then calls `runs.drain`. A delivered event moves to `sent/` until the gateway acks it, and `<data dir>/logs/runs.log` records every delivery and failure. If an event is stuck, look at the entry left in `outbox/`: its `error` field says why.
