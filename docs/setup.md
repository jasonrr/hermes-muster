# Set up Muster

This guide installs Muster, connects it to your repositories and chat, and checks the setup. For what
Muster does, start with the [README](../README.md).

## Requirements

- Hermes >= 0.21.5, with its gateway running and connected to Telegram or Slack.
- herdr >= 0.9.3, `git`, `gh` (logged in) and Claude Code (`claude`). Claude Code is the only supported
  coding agent.
- macOS or Linux.
- Local clones of the repositories Muster will work in.

## Install

1. Install the plugin:

   ```bash
   hermes plugins install <path-or-git-url>
   ```

2. Enable it and add its settings in `$HERMES_HOME/config.yaml`. Hermes loads the plugin only if it is in
   `plugins.enabled`:

   ```yaml
   plugins:
     enabled: [muster]
     entries:
       muster:
         settings:
           approver_login: your-github-login
           approver_id: 123456      # gh api user --jq .id
           repos: ["you/app", "you/site=/srv/site"]
           board: muster
           agent_model: opus
   ```

   For Slack, also set the [Slack settings](channels.md#slack-setup).

3. Create Muster's kanban board. The slug is your `board` setting:

   ```bash
   hermes kanban boards create muster
   ```

   Then add `"human_notices": true` to the board's `board.json`
   (`${HERMES_HOME:-$HOME/.hermes}/kanban/boards/muster/board.json`). This is Hermes's own setting. With it,
   a status ping reads "✔ <task>" followed by Muster's text. Without it, the ping reads
   "✔ [muster] Kanban t_… done — <task>", which says "done" while review is still under way.

4. Copy the cron scripts and schedule them:

   ```bash
   cp scripts/*.sh "${HERMES_HOME:-$HOME/.hermes}/scripts/"   # from this repo
   hermes cron create "every 1m" --no-agent --script muster-tick.sh --name muster-tick
   hermes cron create "every 5m" --no-agent --script muster-cleanup.sh --name muster-cleanup
   hermes cron create "every 1m" --no-agent --script muster-flush.sh --name muster-flush
   ```

   `tick` starts newly approved issues. `cleanup` removes finished workspaces. `flush` delivers events a
   pane could not deliver itself.

5. Restart the Hermes gateway. It registers Muster's chat handlers only when it starts, so restart it
   after every install or update. The gateway and the panes must use the same `HERMES_HOME`.

6. Check the setup without launching anything:

   ```bash
   hermes muster tick --dry-run
   ```

The `hermes muster` commands do not exist under `plugins.isolation: host`, because Hermes then skips
plugin CLI commands.

### Before installing an update

Run the acceptance gate against the checkout you are about to install, and install only on `PASS`:

```bash
HERMES_AGENT_DIR=~/.hermes/hermes-agent ~/.cache/muster-acceptance/venv/bin/python tests/acceptance/gate.py
```

`tests/acceptance/README.md` builds that interpreter once. `FAIL`, `BLOCKED` (a skip, xfail or xpass) and
`CONFIG MISMATCH` all stop the install; the gate prints the reason or the differing key names. It reads only
the display and Muster keys it checks from `config.yaml`.

The gate expects Hermes to clean up the coordinator's tool-progress bubbles in Slack once a turn succeeds.
That is one line in `config.yaml`, for you to add (it applies to every Slack turn, your own chats with your
Hermes agent included):

```yaml
display:
  platforms:
    slack:
      cleanup_progress: true
```

## Configuration

Settings go under `plugins.entries.muster.settings`. Three are required: `approver_login`, `approver_id`
and `repos`. Commands that launch, recover, recommend or clean up refuse to run while a setting is missing
or malformed.

### Who approves and which repositories

| Key | Default | Meaning |
|---|---|---|
| `approver_login` | (required) | The GitHub login allowed to approve work. Compared case-insensitively; `approver_id` is what authorizes. |
| `approver_id` | (required) | That account's numeric GitHub id (`gh api user --jq .id`), unquoted. A login can be renamed; an id cannot. |
| `label` | `agent-ready` | The label that authorizes work when the approver applies it. |
| `bug_label` | `bug` | Issues with this label are briefed as bugs; all others as features. |
| `auto_approvers` | `[]` | Bots allowed to start work on their own issues. See [automatic approvals](#automatic-approvals). |
| `repos` | (required) | A list of `owner/name` (cloned at `clone_root/<name>`) or `owner/name=/abs/clone/path`. Add `@base` to name the base branch (`you/app@develop`, `you/site=/srv/site@master`). See [base branch](#base-branch). |
| `clone_root` | `~/Code` | Where clones live when an entry gives no path. |
| `worktrees` | `~/.herdr/worktrees` | herdr's worktree directory. |
| `branch_prefix` | `muster/` | Issue branches are named `<prefix><issue number>`. Must be non-empty and used only by Muster, because cleanup treats worktrees on those branches as its own. |

### The agent and the board

| Key | Default | Meaning |
|---|---|---|
| `agent_kind` | `claude` | The coding agent. Only `claude` is accepted. |
| `agent_model` | `opus` | The model passed to Claude Code. |
| `board` | `muster` | The Hermes kanban board. Give Muster its own board, and create it before the first tick. |
| `coordinator_name` | `Coordinator` | The name Muster's messages give your Hermes agent ("Ella is reviewing design v2"). |
| `button_hints` | `true` | `false` drops the "What each button does" list and the input hints from decision cards. The recommendation, the feedback, the reason and the deploy line always stay. |
| `gh_config_dir` | `""` | Empty: the pane uses your own `gh` login. Set it to a `GH_CONFIG_DIR` holding another login, such as a bot's, and the pane's `gh` and `git push` act as that login. The directory must contain `hosts.yml`. |
| `notes_dir` | `""` | Where [production notes](#production-notes) live. Empty means `$HERMES_HOME/plugin-data/muster/repos`. |
| `workflow_prompt_file` | `""` | A non-empty file that replaces [`prompts/builder-workflow.md`](../prompts/builder-workflow.md) in every brief. |

### Chat

| Key | Default | Meaning |
|---|---|---|
| `notify_platform` | `telegram` | `telegram` or `slack`. |
| `notify_chat_id` | `""` | Telegram: empty sends DMs to `TELEGRAM_HOME_CHANNEL` from `$HERMES_HOME/.env`; a group id needs `notify_user_id` too. Slack: the channel id (`C…`), required. |
| `notify_user_id` | `""` | Who may answer Muster's prompts: one id, or several separated by commas (`"42,43"`). The first is the user card subscriptions are made for. |
| `notify_chat_type` | `group` | The chat type of `notify_chat_id`. The Telegram DM fallback uses `dm`. Slack needs `group`. |
| `notify_topics` | `false` | One Telegram forum topic or Slack thread per run. Needs `notify_chat_id` and `notify_user_id`. See [run topics](channels.md#run-topics). |

### GitHub Project status (optional)

| Key | Default | Meaning |
|---|---|---|
| `project_owner` | `""` | With `project_number`, names a GitHub Project (v2) whose status moves when an issue's builder gets its brief. Empty turns this off. |
| `project_number` | `0` | That Project's number. Set both keys or neither. |
| `project_status_field` | `Status` | The single-select field to set. |
| `project_status_value` | `In Progress` | The option to set it to. |

The move happens once, after the brief is delivered, and is never retried or allowed to block a launch.
Its outcome is one card comment, such as `project: In Progress` or `project: not in Project #<n>` (only the
first 300 items are searched). The `gh` login the tick uses needs the `project` scope:
`gh auth refresh -s project`.

### Base branch

Worktrees are cut from, and pull requests target, the repository's base branch. Without an `@base` suffix,
Muster reads `origin/HEAD` in the clone, runs `git remote set-head origin -a` once if it is unset, and
falls back to `main` with a note on the card.

An `@` right after `/` belongs to the path (`node_modules/@scope`); any other `@` in a path is read as the
suffix, so give such a path an explicit `@base`. Two clones may not share a directory name, because herdr
names worktrees `<worktrees>/<clone dir name>/<branch>`; give one an explicit path.

## Production notes

Each repository can have a note at `<notes_dir>/<owner>__<repo>.md`, for example `you__app.md`. It goes
into every brief for that repository, issue or ad-hoc, and your Hermes agent reads it to say what a merge
triggers. Cover where production runs, how to read its config and logs, the tools and read-only credentials
the work needs, and what the builder must not touch. Also cover activation, which the builder's handoff and
your agent's check build on: what a merge triggers (often nothing), how a merged change is installed or
deployed, how to check it is live, and how to roll it back. Without a note, the brief tells the builder to
ask. You (or your agent) edit the note; the builder only reads it. See
[activation after merge](reference.md#activation-after-merge).

## Automatic approvals

A bot such as an error tracker can start work on the issues it opens. Add it to `auto_approvers`:

```yaml
auto_approvers:
  - login: "sentry[bot]"
    id: 39604003
    repos: ["you/app"]
    # label: automatic-approval   # optional; must differ from `label`
```

`login`, `id` and `repos` are required, and `repos` must be a subset of `repos` above. An issue is approved
automatically when the bot opened it in one of those repositories and the bot made the newest event for
both `label` and the entry's own label. Entries are tried in order. These runs are briefed as bugs, and
the brief and card say the bot approved them, not you.

Before an automatic approval launches, Muster asks a model whether it duplicates a live run (see
[duplicate reports](reference.md#duplicate-reports)). Muster does not pick that model. To use a cheap one:

```yaml
auxiliary:
  muster_dedupe:
    model: <a cheap model your provider offers>
```

Next: [connect your chat](channels.md).
