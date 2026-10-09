# hermes-muster

A Hermes plugin: one named human labels a GitHub issue, and muster gathers a coding agent in a herdr pane to work it.

On a one-minute cron tick muster looks for issues carrying your approving label (default `agent-ready`) that the configured approver applied. For each one it makes a Hermes kanban card, opens a herdr worktree pane on your machine, starts your coding agent with a fixed brief, and moves the card as the agent asks questions, stops, or finishes. You are pinged (Telegram) and the Hermes agent wakes to summarize.

1. The approver labels an issue in a configured repository.
2. `hermes muster tick` (cron) sees the label event, checks the approver's login and numeric id, and makes one ledger card per approval.
3. The card is subscribed `notify+wake` to your chat, then a herdr worktree on branch `<branch_prefix><issue number>` is created.
4. The agent starts empty in the pane, then the brief (rules, the "how to work" prompt, the repo's production note) is submitted as its first prompt.
5. The agent works the issue. When it asks a question or a permission prompt waits, you get a Telegram message with the question, its context and every option as a button; tap one or reply to the message, and muster hands your answer to the waiting agent and edits the message to say what happened (sent to Claude, answered in the pane, ...). The pane still works too: whichever answers first wins.
6. The agent opens exactly one pull request whose body ends with `Closes #<n>` and runs `hermes muster hook done <PR url>`; the ledger card completes and you get the link. The Hermes agent then reviews the pull request and sends one recommendation (merge, send back, or do nothing) with buttons; see [Decisions in the channel](#decisions-in-the-channel).
7. `hermes muster cleanup` (cron) closes the workspace once the PR is merged, the checkout is clean and pushed, and every pane has been quiet for 30 minutes.

## Why not a bot

- One named human approves. The label event must come from `approver_login` with `approver_id`; a label applied or re-applied by anyone else refuses the issue. The one exception is a bot you list in `auto_approvers` (empty by default), under your standing rule.
- Local. It runs on your machine, in a visible pane you can type into. Nothing is hosted.
- Your own agent CLI, your own `gh` login (or a bot login you name).
- Per-repo memory. A production note per repository (where it runs, how to read logs, what the agent may not touch) is pasted into every brief.

Peers: [herdr-factory](https://github.com/razajamil/herdr-factory) is a herdr plugin with strong overlap; muster adds the approver gate, Hermes kanban cards, Telegram pings and per-repo notes. [Sortie](https://github.com/sortie-ai/sortie) also turns a label into an agent run, but without herdr or an interactive pane.

## Requirements

- Hermes >= 0.21.5, herdr >= 0.9.3, `gh` logged in, `git`, Claude Code (`claude`).
- macOS or Linux.

## Install

```bash
hermes plugins install <path-or-git-url>
```

Enable the plugin and add its settings in `$HERMES_HOME/config.yaml` (the `plugins.enabled` list is the gate):

```yaml
plugins:
  enabled: [muster]
  entries:
    muster:
      settings:
        approver_login: your-github-login
        approver_id: 123456      # gh api user --jq .id
        repos: ["you/app", "you/site=/srv/site"]
        # auto_approvers:          # optional: a monitoring bot whose own issues start work
        #   - login: "sentry[bot]"
        #     id: 39604003
        #     repos: ["you/app"]
        board: muster
        agent_model: opus
```

Create the board muster uses (the slug is your `board` setting); a tick fails on a missing board:

```bash
hermes kanban boards create muster
```

Install the cron scripts and schedule them:

```bash
cp scripts/*.sh "${HERMES_HOME:-$HOME/.hermes}/scripts/"   # from this repo
hermes cron create "every 1m" --no-agent --script muster-tick.sh --name muster-tick
hermes cron create "every 5m" --no-agent --script muster-cleanup.sh --name muster-cleanup
hermes cron create "every 1m" --no-agent --script muster-flush.sh --name muster-flush
```

Check the setup without launching anything:

```bash
hermes muster tick --dry-run
```

## Configuration

Set under `plugins.entries.muster.settings`. Required: `approver_login`, `approver_id`, `repos`.

| Key | Default | Meaning |
|---|---|---|
| `label` | `agent-ready` | Label whose application by the approver authorizes work. |
| `bug_label` | `bug` | Issues carrying it are briefed as bugs, others as features. |
| `approver_login` | (required) | GitHub login allowed to approve. Compared case-insensitively, but `approver_id` is what authorizes. |
| `approver_id` | (required) | The approver's numeric GitHub id (`gh api user --jq .id`), unquoted: an integer. A login can be renamed; an id cannot. |
| `auto_approvers` | `[]` | Bots that may approve without you. Each entry is `{login, id, label, repos}`. `login` and `id` are required: a `[bot]` login belongs to exactly one GitHub App. `label` is optional and defaults to `automatic-approval`. `repos` is required and must be a subset of `repos`. An issue is approved automatically when its repo is in the entry's `repos`, the entry's login and id opened it, it still carries the entry's `label`, and the newest `label` event and the newest event for the entry's label were both made by that account. Entries are tried in order and the first match wins; when none matches, only you can approve. An automatic approval is briefed as a bug, and the brief and card say the bot approved it, never you. A bot re-applying `label` makes a new card, as a relabel by you does; re-applying only its own label does not. |
| `repos` | (required) | List of `owner/name` (clone at `clone_root/<name>`) or `owner/name=/abs/clone/path`, each with an optional `@base` suffix (`you/app@develop`, `you/site=/srv/site@master`): the branch worktrees are cut from and pull requests target. Without it, muster reads `origin/HEAD` in the clone (running `git remote set-head origin -a` once if unset), else uses `main` and notes that in the launch record. An `@` right after `/` belongs to the path (`node_modules/@scope`); any other `@` in a path is read as the suffix, so give such a path an explicit `@base`. `launch --base` overrides it for an ad-hoc run. Two clones may not share a directory name (herdr names worktrees `<worktrees>/<clone dir name>/<branch>`): give one an explicit path. |
| `clone_root` | `~/Code` | Where clones live when no path is given. |
| `board` | `muster` | Hermes kanban board slug. Give muster its own board; idempotency keys are per board. It must exist before the first tick. |
| `agent_kind` | `claude` | Coding agent. v1: `claude` only. |
| `agent_model` | `opus` | Model passed to the agent CLI. |
| `branch_prefix` | `muster/` | Issue branches are `<prefix><issue number>`. Must be a non-empty string, and a namespace muster owns: cleanup treats worktrees on `<prefix><n>` branches as its own. |
| `notify_platform` | `telegram` | Gateway platform for pings. |
| `notify_chat_id` | `""` | Empty = DM the `TELEGRAM_HOME_CHANNEL` from `$HERMES_HOME/.env`. When you set it to a group, also set `notify_user_id` (the code falls back to the chat id as the user id). |
| `notify_user_id` | `""` | The user the subscription is for. |
| `notify_chat_type` | `group` | Chat type used with `notify_chat_id` (a DM fallback uses `dm`). |
| `gh_config_dir` | `""` | Empty = the pane uses your own `gh` login. Set it to a `GH_CONFIG_DIR` holding another login (for example a bot): the pane then gets `GH_CONFIG_DIR=<dir>` and blank `GH_TOKEN`/`GITHUB_TOKEN`, so `gh` and `git push` act as that login. The launch refuses if `hosts.yml` is missing there. |
| `notes_dir` | `""` | Per-repo production notes. Empty = `$HERMES_HOME/plugin-data/muster/repos`. |
| `workflow_prompt_file` | `""` | Replaces `prompts/workflow.md` in the brief. |
| `worktrees` | `~/.herdr/worktrees` | herdr's worktree directory. |
| `project_owner` | `""` | Optional. With `project_number`, the GitHub Project (v2) whose Status moves when an issue's agent gets its brief (`gh project view <number> --owner <owner>`). Empty = off: no `gh project` call at all. |
| `project_number` | `0` | That Project's number, an integer. Set both or neither. |
| `project_status_field` | `Status` | The single-select field to set. |
| `project_status_value` | `In Progress` | The option it is set to. |

The Project move runs after each launch or recover delivers the brief, and is never retried. Ids are resolved every time. The outcome is one card comment: `project: In Progress`, `project: not in Project #<n>` (or `not in the first 300 items of Project #<n>`: only 300 are searched), `project: field/option not found`, or `project: failed: <error>`. It never blocks a launch. The tick's `gh` login needs the `project` scope: `gh auth refresh -s project`.

## Per-repo production notes

`<notes_dir>/<owner>__<repo>.md`, for example `you__app.md`. Its text goes into every brief for that repository; with no file, the brief says production is unknown and to ask. Use six headings: Platforms; Where production runs; How to read config and logs; Tools the worker image needs; Read-only credentials to provision; Open questions. You edit it (or ask your agent to); the pane agent only reads it.

## How work flows

- The **ledger card** is the record of one approval (or ad-hoc run). It is blocked when the agent's launch fails or its session ends without a pull request, and completed when the pull request is done.
- A **wait card** opens each time the agent stops to wait for you (a question or permission prompt) and is archived once it is answered (in the channel or the pane). When muster can deliver the question itself (Telegram, a pane started with the bridge hook, the gateway side running) the card takes no subscription (no plain ping, no Hermes agent turn): muster's own message is the one notice. Otherwise it pings as before. Questions after the pull request (a reviewer asks for a change) still open one and ping you; only an archived ledger is silent. After 10 idle minutes without a finished run, an ad-hoc run's wait card says so.
- A **proposal** is the plan or design an agent asks you to approve. The brief tells it to write the whole thing to a file and run `hermes muster hook propose <file>` (an ad-hoc run: `hermes muster hook --card <card> propose <file>`) before it asks. Each distinct text becomes the next version, `Proposal v<n> <sha256[:12]>`: one ledger comment headed so, and a copy in `<data dir>/runs/<ledger>/proposals/v<n>.md`. Earlier versions stay. Once the comment reads back on the ledger, the version is armed for the agent's approval request: an AskUserQuestion with a question headed `Approval`. That request's wait card body carries the version id, the full text and every question and option the agent asked, and the ping names the version. Other questions and permission prompts never take a proposal. An approval request is refused before its dialog shows (the agent sees why) when no proposal is armed (none saved, the comment not on the ledger yet, or the armed one already carried by an earlier approval request), or when another wait card is still open; a refusal keeps the armed version for the next request. The armed version is used up only once a wait card carries it. To review: `hermes kanban --board <board> show <wait card> --json` (the body), and `show <ledger> --json` (every version, in its comments). The file is capped at 48 KB (and each option preview on the card at 2 KB), and GitHub tokens in it are redacted.
- Every card is subscribed `notify+wake`: the gateway pings you, then queues a Hermes agent turn. The skills `muster:escalation` and `muster:run` tell that agent to inform you and never act on the pane, the issue or the pull request.
- If a launch failed ("The coding agent did not start", "did not become ready", "its first prompt may not have arrived", "The launch was refused"), fix the cause and run `hermes muster recover <ledger card id>`. For "may not have arrived", look at the pane first and add `--resend` only if it shows no brief. Add `--adopt` when a launch refused a branch that already has commits ahead of base, or a worktree muster did not make, and you want the agent to work on it as is (it also resumes an ad-hoc run from before launch records). An issue card from before launch records cannot be recovered: remove and re-apply the label. Never re-launch the work on a new branch.

## Decisions in the channel

Herdr is optional: every routine decision can be made from Telegram. Pane ids appear on messages as provenance only.

- **Questions.** Each pane starts with a Claude Code `PermissionRequest` hook (`hermes muster hook permission`). It runs beside the pane's own dialog, records the dialog as a decision request in `<data dir>/decisions/`, and waits up to 24 hours for your answer. Inside the Hermes gateway, muster shows it with Hermes's own clarify prompt: the question, the agent's context, every option with its description, and the proposal version when it asks for approval. Tap an option, tap Other and type, or reply to the message with text; for several choices type the numbers (`1,3`). Hermes parses the reply.
- **Permission prompts.** These use Hermes's own approval card: the command as code, then Allow once and Deny. There is no "allow for the session": a session rule did not stop Claude asking again for the same command, and neither did Claude's own "don't ask again" (seen live), so each prompt is decided once. A reply to the card denies it and tells the agent your words. Hermes's `approvals.timeout` applies; an unanswered card is denied, and the agent is told why. A subagent's permission prompt works the same way and names the subagent. Claude shows a subagent's dialog in the pane only after the hook returns, so while muster waits, the pane cannot answer it; with the gateway down, the hook returns at once and the dialog shows.
- **Either way**, answering in the pane first works as before (Claude stops the hook). If anything goes wrong (the gateway down, an error), the hook gives no decision and the pane's dialog decides: muster never allows on error.
- **Builds.** When a pull request is ready (or revised, below), the Hermes agent reads the diff, the code around it, the checks and the repo's production note, and runs `hermes muster recommend <ledger> --pr <url> --head <sha> --choice merge|send-back|nothing --review <file> [--feedback <file>]`. You get its review, what it verified versus what the coding agent claimed, the deploy consequence, and three buttons:
  - **Do nothing** records the choice; the pull request stays open.
  - **Merge** re-checks that the pull request is still the run's own (same branch, base and repository, not a fork or draft), that its head is still the reviewed sha, and that GitHub reports it mergeable (`mergeStateStatus` CLEAN, HAS_HOOKS or UNSTABLE: required checks are GitHub's call, so on a repository without branch protection a red check does not block), then runs `gh pr merge --squash --match-head-commit <sha>` with your own `gh` login (the bot's `GH_TOKEN`, `GITHUB_TOKEN` and `GH_CONFIG_DIR` are removed) and reads back MERGED before saying so. A merge never implies a deploy.
  - **Send back** shows the proposed revision instructions; send them as written, reply to add instructions, or start the reply with `replace:` to replace them. They go to the same agent in the same pane, and count as sent only once the pane's prompt hook confirms they arrived. A busy or gone agent gets nothing, and you get a Retry.
- **Re-review.** After a delivered send-back, the agent's next `hook done` (an issue run) or finished turn (an ad-hoc run) with a pushed, clean, non-draft pull request at a new head opens a review card, "revised, ready for re-review", which pings you and wakes the Hermes agent to recommend again. One card per reviewed head, however often done runs. New commits make older recommendations stale: their buttons answer "stale". An unfinished revision makes `hook done` exit 1 and tell the agent why.
- **Safety.** Only `notify_user_id` (or, for a DM, the DM's user) in the notify chat can tap or reply to a muster prompt. Being allowed to talk to the Hermes agent is not enough. Anyone else's tap is refused and logged, and anyone else's message is never taken as an answer (it reaches the Hermes agent as an ordinary message). Each message is edited to what happened (Sent to Claude, Allow sent to Claude, Deny sent to Claude, Answered in the pane, No answer in time: deny sent to Claude). "Sent" means muster handed your answer to Claude. Claude takes whichever answer comes first, so that is not confirmation it was applied, and an allow is permission, not a command that succeeded. Every request records an audit trail (each status change with its time and result). A double tap, a replay or a gateway restart never repeats an effect. After a restart, open requests are shown again (older messages say "Superseded"), and an interrupted merge or send-back is read back from GitHub or the pane, never redone without a new tap.

The gateway side registers Telegram callback guards (muster prompts answer only the notify user; the reply prompt after Other) and a `pre_gateway_dispatch` hook, so **restart the Hermes gateway after installing or upgrading muster**. The gateway and the panes must resolve the same `HERMES_HOME` (the requests live in its plugin data). Panes started before the upgrade have no bridge hook; their questions ping as before. Slack: see `docs/decisions/2026-10-08-decision-channels.md`.

## Ad-hoc runs

For coding work that is not a labeled issue:

```bash
hermes muster launch --cwd ~/Code/app --branch feat/thing --title "Add thing" --brief brief.md [--base main] [--model opus]
```

The branch must match `feat|fix|chore|deps/<name>`. It makes a card, opens the pane, delivers the brief, and prints the run as JSON. Hook events the pane could not deliver are queued and delivered by `hermes muster flush`, which the `muster-flush` cron job runs every minute. For research with no pull request, `hermes muster open --cwd <dir> --label <label>` opens an analysis workspace muster owns, so cleanup can close it.

## Cleanup

`hermes muster cleanup` covers only workspaces muster has a record for: issue worktrees, ad-hoc runs, and analysis workspaces. A coding worktree is removed (never with `--force`; the branch stays) only when its pull request(s) are merged with HEAD as the head, the checkout is clean (agent scaffolding copied from the main checkout aside), everything is pushed, and every pane has been quiet for 30 minutes (agents idle or done, no job running under a shell). An analysis workspace needs only the quiet window; closing it deletes no file. Any failed check restarts the 30-minute window.

A coding worktree that is quiet but has unsaved work is never removed: unless a pull request from its branch is still open (work under way), once per state a blocked card "Unsaved work in <repo>" pings you and asks whether to push or commit, discard, or keep. `hermes muster cleanup --dry-run` reports what it would do without acting. `hermes muster open` is described under Ad-hoc runs.

## Agents

v1 supports Claude Code. To add a kind, write one module in `muster/` with `KIND`, `hook_settings`, `launch_args`, `is_ours`, `ignore` and `detail` (see `muster/claude.py`). A CLI without hooks would need a `herdr agent wait` fallback; that is planned, not built.

## Security and disclosure

- Shells out to `gh`, `git`, `herdr` and `hermes`. Reads GitHub issues and timelines, the board's sqlite (read-only), `$HERMES_HOME/.env` for the DM chat id, and the per-repo notes.
- Writes: kanban cards, worktrees under `worktrees`, state under `$HERMES_HOME/plugin-data/muster/`, and per-pane agent settings (the hooks) in that data directory. Nothing global in your agent's config changes.
- The agent is interactive, runs as you, and is not sandboxed. Claude Code starts with `--permission-mode auto`, so it runs most tools without asking. The brief forbids it to push to main, merge, approve, deploy or force-push, to edit `.github/`, CI, deployment config, secrets, lockfiles or agent-instruction files, or to add dependencies. Issue text never enters the brief and is treated as data.
- Human review of the pull request is the control: nothing merges or deploys without you. muster merges only when you tap Merge on a recommendation, squash, pinned to the reviewed head, with your own `gh` login.
- Inside the Hermes gateway, muster runs a scan every 2 s over `<data dir>/decisions/`, a Telegram callback guard, and a `pre_gateway_dispatch` hook that swallows only replies to its own messages from you.
- Under `plugins.isolation: host`, Hermes skips `register_cli_command`, so the `hermes muster` commands do not exist.

## Development

```bash
python3 -m pytest -q
hermes plugins validate .
```

Tests are fully mocked: `tests/fake_herdr.py` stands in for `herdr`, `gh` and `hermes`.

## License

MIT. See `LICENSE`.
