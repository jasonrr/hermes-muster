# hermes-muster

Muster is a Hermes plugin that brings human-directed coding agents into your workflow, from dispatch and design approval through implementation, review and cleanup. You make the decisions in Telegram or Slack, and the work stays visible in herdr.

You approve work by labeling a GitHub issue, or you ask the Hermes agent for an ad-hoc run. Muster then opens a herdr worktree pane on your machine and starts Claude Code in it with a brief. It tracks the run on a Hermes kanban card. It sends each question, permission prompt, design and finished build to your chat as a message with buttons. Merges happen only when you tap. Once the work is merged, muster removes the finished workspace, after backing up anything it would otherwise lose.

- [Who does what](#who-does-what)
- [Requirements](#requirements) · [Install](#install) · [Configuration](#configuration)
- [Starting work](#starting-work): [labeled issues](#labeled-issues), [automatic approvals](#automatic-approvals-and-duplicate-reports), [ad-hoc runs](#ad-hoc-runs), [analysis workspaces](#analysis-workspaces)
- [During a run](#during-a-run): [how the builder works](#how-the-builder-works), [questions and permissions](#questions-and-permission-prompts), [design approval](#design-approval), [build review](#build-review-merge-and-revisions), [progress](#run-progress), [cards](#cards-on-the-board), [recovery](#recovery)
- [Cleanup](#cleanup)
- [Channels](#channels): [Telegram](#telegram), [run topics](#run-topics), [Slack](#slack)
- [Prompts](#prompts) · [Security and disclosure](#security-and-disclosure) · [Development](#development) · [Repository docs](#repository-docs)

## Who does what

- **You, the human**, approve work, answer the builder's questions and permission prompts, approve designs, and decide every merge or send-back. Only the GitHub account you configure can approve an issue, and only the chat members you list can answer muster's messages.
- **The builder** is Claude Code in a herdr pane. It works one branch in one worktree. It asks you through its own question tool, and it opens exactly one pull request. Its brief forbids it to push to the base branch, merge, approve, deploy or force-push.
- **The coordinator** is the Hermes agent you already talk to; `coordinator_name` sets the name muster's messages give it. When a card wakes it, it tells you what happened. It reviews each design version and each finished build, then records one recommendation, and your tap acts on it. It never types into a pane, answers the builder, moves a card or merges anything itself. Its instructions are the skills `muster:escalation`, `muster:review-standard` and `muster:dispatch`.
- **Muster** is the plumbing between them: cron jobs, the pane's Claude Code hooks, and the gateway-side channel code.

## Requirements

- Hermes >= 0.21.5 with its gateway running and connected to Telegram or Slack.
- herdr >= 0.9.3, `git`, `gh` (logged in), and Claude Code (`claude`). Claude Code is the only supported agent.
- macOS or Linux.
- Local clones of the repositories you configure.

## Install

```bash
hermes plugins install <path-or-git-url>
```

Enable the plugin and add its settings in `$HERMES_HOME/config.yaml`. Hermes loads the plugin only if it is on the `plugins.enabled` list:

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

Create the board muster uses. The slug is your `board` setting, and a tick fails if the board is missing:

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

`tick` looks for newly approved issues. `cleanup` removes finished workspaces and closes conversations. `flush` delivers hook events that a pane could not deliver itself.

Restart the Hermes gateway after installing or updating muster. The gateway registers muster's channel handlers and its `pre_gateway_dispatch` hook only when it starts. The gateway and the panes must resolve the same `HERMES_HOME`, because the decision requests live in its plugin data.

Check the setup without launching anything:

```bash
hermes muster tick --dry-run
```

Under `plugins.isolation: host`, Hermes skips `register_cli_command`, so the `hermes muster` commands do not exist.

## Configuration

Set these under `plugins.entries.muster.settings`. Required: `approver_login`, `approver_id`, `repos`. The commands that launch, recover, recommend or clean up check every setting first, and refuse to run while one is missing or malformed.

| Key | Default | Meaning |
|---|---|---|
| `label` | `agent-ready` | The label that authorizes work when the approver applies it. |
| `bug_label` | `bug` | Issues that carry it are briefed as bugs; all others are briefed as features. |
| `approver_login` | (required) | The GitHub login allowed to approve. It is compared case-insensitively, but `approver_id` is what authorizes. |
| `approver_id` | (required) | The approver's numeric GitHub id (`gh api user --jq .id`), written unquoted as an integer. A login can be renamed; an id cannot. |
| `auto_approvers` | `[]` | Bots that may approve without you. Each entry is `{login, id, label, repos}`. `login` and `id` are required: a `[bot]` login belongs to exactly one GitHub App. `label` is optional and defaults to `automatic-approval`; it must differ from `label`. `repos` is required and must be a subset of `repos`. See [automatic approvals](#automatic-approvals-and-duplicate-reports). |
| `repos` | (required) | A list of `owner/name` (cloned at `clone_root/<name>`) or `owner/name=/abs/clone/path`. Each entry takes an optional `@base` suffix (`you/app@develop`, `you/site=/srv/site@master`), which names the branch that worktrees are cut from and pull requests target. Without the suffix, muster reads `origin/HEAD` in the clone. If that is unset, it runs `git remote set-head origin -a` once; if that also fails, it uses `main` and notes this on the card. An `@` right after `/` belongs to the path (`node_modules/@scope`). Any other `@` in a path is read as the suffix, so give such a path an explicit `@base`. Two clones may not share a directory name, because herdr names worktrees `<worktrees>/<clone dir name>/<branch>`; give one of them an explicit path. |
| `clone_root` | `~/Code` | Where clones live when an entry gives no path. |
| `board` | `muster` | The Hermes kanban board slug. Give muster its own board, because idempotency keys are per board. The board must exist before the first tick. |
| `agent_kind` | `claude` | The coding agent. Only `claude` is accepted. |
| `agent_model` | `opus` | The model passed to Claude Code. |
| `branch_prefix` | `muster/` | Issue branches are named `<prefix><issue number>`. The prefix must be a non-empty string in a namespace muster owns, because cleanup treats worktrees on `<prefix><n>` branches as its own. |
| `notify_platform` | `telegram` | Where muster talks to you: `telegram` or `slack`. Anything else is refused. |
| `notify_chat_id` | `""` | On Telegram, empty means muster DMs the `TELEGRAM_HOME_CHANNEL` from `$HERMES_HOME/.env`. If you set it to a group, also set `notify_user_id`; otherwise muster uses the chat id as the user id. On Slack, this is the channel id (`C…`) and is required. |
| `notify_user_id` | `""` | The approvers in the chat: one id, or several separated by commas (`"42,43"`). Only they may answer a muster prompt. The first id is the user that card subscriptions are made for. |
| `notify_chat_type` | `group` | The chat type used with `notify_chat_id`. A DM fallback uses `dm`. Slack needs `group`. |
| `notify_topics` | `false` | Gives each run its own conversation: a Telegram forum topic in the `notify_chat_id` group, or a Slack thread. It needs `notify_chat_id` and `notify_user_id`. See [run topics](#run-topics). |
| `coordinator_name` | `Coordinator` | The Hermes agent's name in muster's messages ("Ella is reviewing design v2"). |
| `gh_config_dir` | `""` | Empty means the pane uses your own `gh` login. Set it to a `GH_CONFIG_DIR` that holds another login, for example a bot's. The pane then gets `GH_CONFIG_DIR=<dir>` with `GH_TOKEN` and `GITHUB_TOKEN` blanked, so `gh` and `git push` act as that login. A launch is refused if `hosts.yml` is missing from that directory. |
| `notes_dir` | `""` | Where the per-repo production notes live. Empty means `$HERMES_HOME/plugin-data/muster/repos`. |
| `workflow_prompt_file` | `""` | A file that replaces `prompts/builder-workflow.md` in every brief. It must exist and must not be empty. |
| `worktrees` | `~/.herdr/worktrees` | herdr's worktree directory. |
| `project_owner` | `""` | Optional. Together with `project_number`, it names a GitHub Project (v2) whose Status moves when an issue's builder gets its brief (`gh project view <number> --owner <owner>`). Empty turns this off, and no `gh project` call is made. |
| `project_number` | `0` | That Project's number, as an integer. Set both `project_owner` and `project_number`, or neither. |
| `project_status_field` | `Status` | The single-select field to set. |
| `project_status_value` | `In Progress` | The option to set it to. |

The Project move runs after each launch or recover delivers the brief, and it is never retried. Ids are looked up each time. The outcome is one card comment: `project: In Progress`, `project: not in Project #<n>` (or `not in the first 300 items of Project #<n>`, since only 300 items are searched), `project: field/option not found`, or `project: failed: <error>`. It never blocks a launch. The `gh` login that the tick uses needs the `project` scope: `gh auth refresh -s project`.

### Per-repo production notes

Each repository can have a note at `<notes_dir>/<owner>__<repo>.md`, for example `you__app.md`. Its text goes into every issue brief for that repository, and the coordinator reads it to state what a merge deploys. Without a note, the brief says that nothing records what a merge deploys and tells the builder to ask. A useful note covers where production runs, how to read its config and logs, the tools and read-only credentials the work needs, and what the builder must not touch. You edit the note, or ask your agent to; the builder only reads it.

## Starting work

### Labeled issues

1. The approver applies `label` to an issue in a configured repository.
2. On its next tick (every minute), `hermes muster tick` reads the issue's timeline. It checks that the newest event for the label came from `approver_login` with `approver_id`; a label applied or re-applied by anyone else refuses the issue. It then makes one ledger card for that approval. Re-applying the label later makes a new card.
3. The card is subscribed to your chat. Muster creates a herdr worktree on branch `<branch_prefix><issue number>`, cut from the repository's base branch, and starts Claude Code in it with no prompt.
4. Muster then submits the brief as Claude Code's first prompt. The brief contains the rules, [how to work](#how-the-builder-works), [how to ask you](#questions-and-permission-prompts) and the repository's production note. Issue text never enters the brief: the builder reads the issue with `gh` and treats it as data.
5. The builder comments "Picked up by muster." on the issue and works it. Questions, permission prompts and design approvals reach you in the chat.
6. The builder opens one pull request whose body ends with `Closes #<n>`, then runs `hermes muster hook done <PR url>`. Muster refuses it unless `gh` names the run's branch as the pull request's head. The ledger card is completed, and the coordinator [reviews the build](#build-review-merge-and-revisions).
7. Once the pull request is merged and the panes are quiet, [cleanup](#cleanup) removes the workspace.

### Automatic approvals and duplicate reports

A bot listed in `auto_approvers`, such as an error tracker, can start work on the issues it opens. Muster approves an issue automatically when all of these hold: its repository is in the entry's `repos`; the entry's login and id opened it; it still carries the entry's `label`; and the newest `label` event and the newest event for the entry's label were both made by that account. Entries are tried in order and the first match wins; when none matches, only you can approve. An automatic approval is briefed as a bug, and the brief and card say that the bot approved it, never you. A bot re-applying `label` makes a new card, just as a relabel by you does; re-applying only its own label does not.

Error trackers often file several issues for one incident. Before an automatic approval launches, muster compares the issue with the repository's live runs: issue runs whose ledger is not archived and whose builder is still in its pane. It makes one model call through Hermes (`ctx.llm`, under muster's auxiliary task `muster_dedupe`). The model can answer only by naming one of those runs, or none. Muster does not pick a model; to use a cheap one, set it in `$HERMES_HOME/config.yaml`:

```yaml
auxiliary:
  muster_dedupe:
    model: <a cheap model your provider offers>
```

- **No duplicate, or any failure** (no model, a timeout, an unusable answer): the issue launches normally. A failure is also noted on its card ("dedupe check failed").
- **A possible duplicate of a live run:** the issue's card is held. It is blocked with "Possible duplicate of …; not dispatched", and it gets no worktree and no builder. Muster hands the judgment to the live run's builder in its pane, as one line plus JSON data, once that builder is idle and none of your questions is open there. The handoff counts as delivered only when the pane's prompt hook confirms it arrived. The builder then decides:
  - **Same incident:** it comments on the report, adds `Closes #<n>` to its own pull request body, and runs `hermes muster hook duplicate <n> same`. The held card records this and stays held. When that pull request merges, GitHub closes the report and the tick archives the held card.
  - **Different incident:** it runs `hermes muster hook duplicate <n> different`, and the next tick launches the report normally, after checking the approval again.
- **You decide instead:** `hermes muster recover <held card>` launches the report, and closing the issue drops it. A reopened issue is not dispatched again on its old approval; only a new approving label event makes a new card.
- **Notices:** the block pings you once. At most one more card follows, if the live run's builder is gone, or if nothing was delivered or answered within 24 hours.
- Issues you labeled yourself are never checked, since you judged each one when you labeled it.

### Ad-hoc runs

For coding work that has no labeled issue, ask the Hermes agent to dispatch it. Its `muster:dispatch` skill writes your request to a brief file and runs:

```bash
hermes muster launch --cwd ~/Code/app --branch feat/thing --title "Add thing" --brief brief.md [--base develop] [--model opus]
```

The branch must match `(feat|fix|chore|deps)/<name>`, where the name uses lowercase letters, digits, `.`, `_` and `-`, and each branch can be used for only one run. `--base` defaults to the repository's `@base` in `repos`, and otherwise to origin's default branch. `--model` defaults to `agent_model`. The command makes a card, opens the pane, delivers the brief and prints the run as JSON.

The builder's prompt is your brief followed by a footer that names the run's card, branch and base, the same "Asking the human" and "How to work" text that issue runs get, and the rule never to push to the base, merge, deploy or force-push. If your brief already states the approach and scope, it counts as the approved design and the builder goes straight to building. Otherwise the builder proposes a design first.

An ad-hoc builder does not report its own completion. Each time its turn ends, muster checks GitHub. The run is finished when exactly one open or merged, non-draft pull request goes from the branch into the base, its head is the worktree's HEAD, and no tracked file has uncommitted changes. If the builder sits idle for 10 minutes without a finished pull request, a wait card says so.

### Analysis workspaces

For research or analysis that ends in no pull request, the Hermes agent runs `hermes muster open --cwd <dir> --label "<short label>"`. This opens a herdr workspace that muster records, so cleanup can close it after every pane has been quiet for 30 minutes. Closing it deletes no file. The command prints `workspace`, `pane` and `terminal` as JSON. No card tracks the workspace.

## During a run

### How the builder works

Every brief includes the same house workflow, [`prompts/builder-workflow.md`](prompts/builder-workflow.md), unless `workflow_prompt_file` replaces it. In short:

- **Bugs:** reproduce the bug, find its root cause, write a failing test, then fix it at the source. An architectural or multi-file fix is handled like a feature.
- **Features:** read the code, then write a short design covering the problem, the chosen approach, what was cut and why, and open questions. **You approve the design**, and no code is written before you do.
- **Implementation plan:** after the design is approved, the builder writes a step-by-step plan to `docs/plans/YYYY-MM-DD-<slug>.md` in the worktree, and a fresh sub-agent attacks it until it holds. That attack is the plan's approval: you approve the design, not the plan, and you are never asked to approve the plan.
- **Build:** tests come first. A cheap sub-agent reviews each task, and a fresh sub-agent reviews the whole branch before the pull request is opened, using the repository's `docs/review-lenses.md` if it has one.
- **Pull request:** one pull request. Its body is your evidence: every review finding with its severity, verdict and outcome, the probe log, the test and lint commands that were run, and any deviations from the plan.
- Trivial one-file changes skip the design and the plan.

### Questions and permission prompts

The builder asks with Claude Code's AskUserQuestion tool, because a question in plain text pings no one. Each pane starts with Claude Code hooks pointing at `hermes muster hook`. When a dialog waits for you, muster opens a wait card, records the dialog as a decision request in `<data dir>/decisions/`, and the gateway shows it to you as its own message. You can answer there or in the pane, and whichever answer comes first wins.

- **Questions** show the question, the builder's context, every option with its description, and the design version when the question asks for approval. Tap an option, or answer in your own words (see [Telegram](#telegram) and [Slack](#slack) for how each channel takes them). A question waits up to 24 hours.
- **Permission prompts** show the command and offer Allow once and Deny; you can also deny with a message, which the builder is told. Each prompt is decided once, and there is no "allow for the session". An unanswered prompt is denied, and the builder is told why. A subagent's permission prompt works the same way and names the subagent. While muster waits, the pane cannot answer that prompt, because Claude Code shows a subagent's dialog only after the hook returns.
- **Fail closed:** if anything goes wrong, such as the gateway being down or an error, the hook makes no decision and the pane's own dialog decides. Muster never allows anything on error.
- **What a message says afterwards:** each message is edited to show what happened, for example "Answer sent to the builder", "Allowed once · sent to the builder" or "Answered in the pane". "Sent" means muster handed your answer to Claude Code. It does not confirm the answer was applied, and an allow is permission, not proof that the command succeeded. Every request keeps an audit trail of its status changes, with times and results. A double tap, a replay or a gateway restart never repeats an effect.
- When muster cannot deliver a question itself, for example while the gateway side is down, the wait card pings you the ordinary Hermes way instead.

### Design approval

Before asking you to approve a design, the builder writes all of it to a file and runs `hermes muster hook propose <file>`; an ad-hoc builder runs `hermes muster hook --card <card> propose <file>`. Each distinct text becomes the next version, `Proposal v<n> <sha256[:12]>`. The version is saved as one ledger comment with that heading, and as a copy in `<data dir>/runs/<ledger>/proposals/v<n>.md`. Earlier versions are kept. A proposal file is capped at 48 KB, each option preview on the card at 2 KB, and GitHub tokens in it are redacted.

The builder then asks with AskUserQuestion, giving the approval question the header `Approval`. Muster refuses that question before its dialog shows, and tells the builder why, in three cases: no version has been saved and read back on the ledger, the saved version was already carried by an earlier approval request, or another wait card is still open. To read a version yourself, run `hermes kanban --board <board> show <ledger> --json`; every version is in its comments.

Each version also wakes the coordinator, without pinging you, so it can review that exact version. The coordinator records its review with `hermes muster recommend-design <ledger> --proposal v<n>:<sha> --choice approve|revise|judgment --review <file> --summary <file> [--feedback <file>]`. You get its full review and the design's full text, then a card with its summary, the complete revisions it would send, and three buttons: **Approve design v<n>**, **Send revisions to builder** and **Leave pending**. One decision is made per version:

- Until the review arrives, the builder's question says "Design v<n> ready · <coordinator> is reviewing. No action needed", and its approve button reads "… without <coordinator>'s review". Once the review card is posted, the question points at it and loses its buttons, so the review card is the one place to decide. If you choose Leave pending, the question shows again.
- A tap made before the builder asks is kept, with its exact words, and given to the builder when it asks about that version. A newer version cancels the tap ("Nothing was approved" / "The revisions were not sent"). A tap answers only the Approval question of a request that asks several; the other questions keep their own messages and answers.
- The card shows what happened: "recorded · delivering to the builder", or "recorded · awaiting your other answer above", then "delivered to the builder" once the pane's hook has taken it. If you answered the builder's question first, that answer stands. The review then arrives as a short note with no buttons, and nothing asks you again.
- A tap never acts on another version, and never moves on to another question once one has been answered. The coordinator cannot answer the question itself.

### Build review, merge and revisions

When a pull request is ready, the ping says "PR #<n> opened · <coordinator> is reviewing. No action needed". The coordinator reads the diff, the code around it, the checks, the builder's review log in the pull request body and the repository's production note. It records one recommendation with `hermes muster recommend <ledger> --pr <url> --head <sha> --choice merge|send-back|nothing --review <file> --summary <file> [--feedback <file>]`. Its full review is posted as its own message. It covers what the coordinator verified versus what the builder claimed, the gaps, and the deploy consequence. Below it, a decision card shows the recommendation and the summary. The summary is at most 900 characters (a longer one is refused, never cut) and must state the deploy consequence. The card has three buttons:

- **Leave PR open** records your choice, and the pull request stays open.
- **Merge** first checks the pull request again: it must still be the run's own (same branch, base and repository, not a fork or a draft), its head must still be the reviewed sha, and GitHub must report it mergeable (`mergeStateStatus` CLEAN, HAS_HOOKS or UNSTABLE). Required checks are GitHub's call, so on a repository without branch protection a red check does not block the merge. Muster then runs `gh pr merge --squash --match-head-commit <sha>` with your own `gh` login, removing the bot's `GH_TOKEN`, `GITHUB_TOKEN` and `GH_CONFIG_DIR`. It reads back MERGED before saying the merge happened. A merge never implies a deploy.
- **Review feedback…** opens a confirmation card that shows the complete proposed revision instructions. You can choose **Send feedback to builder**, add your own words, or start your words with `replace:` to replace the instructions. They go to the same builder in the same pane, and count as sent only once the pane's prompt hook confirms they arrived. A busy or absent builder gets nothing, and you get a Retry button.

**Re-review.** After a send-back has been delivered, the builder pushes a revision. On an issue run it runs `hook done` again; on an ad-hoc run its turn ends. Once the pull request is pushed, clean and non-draft at a new head, muster opens a review card, "PR #<n> revised", and the coordinator recommends again. There is one card per reviewed head. New commits make older recommendations stale, and their buttons then answer "stale". An unfinished revision makes `hook done` exit 1 and tell the builder why.

### Run progress

Each run gets one status message that is edited in place and never posted again, so you can tell a started build from a quiet or waiting one without asking. On Slack with run threads, it is the thread's parent message. Otherwise muster posts it once, when the brief is sent, in the run's topic or the main chat. Later questions appear below it.

Each line comes from the pane's own Claude Code hooks, never from herdr's screen state or the pane's content:

| Line | Evidence |
|---|---|
| Dispatched 14:01 · waiting for the builder to start | Muster sent the brief, but the prompt hook has not reported it and nothing stronger has been seen. |
| Started 14:02 · no tool activity yet | The UserPromptSubmit hook reported the brief's hash. |
| Working · last activity 3 min ago (14:37) | The newest PostToolUse or PostToolUseFailure, from the main agent or a subagent. |
| Quiet 40 min · last tool 14:37 · long tools can be quiet | No tool has finished for 15 minutes. This means unknown, not failed, and muster takes no action. |
| Needs your decision: question, design v2 · since 14:41 | An open request you must answer: a question, a permission prompt, or a design, build or feedback card. An Approval question still under the coordinator's review does not count. |
| Ella is reviewing design v2 · no action needed | A design version was saved and has no recommendation yet. |
| PR #9 opened · Ella is reviewing · no action needed | A verified pull request (an issue run's `hook done`, an ad-hoc run's verified completion, or a verified revision) with no recommendation yet. |
| PR #9 open · Ella reviewed it | A recommendation was recorded and decided. |
| Feedback delivered 15:10 · revision pending | The pane's hooks confirmed that a send-back or design revisions arrived, and there is no newer review or design. |
| Turn ended 15:20 · the builder is waiting at its prompt, no question open | The Stop hook fired, with nothing newer. |
| Session ended 15:30 · the builder is not running | The SessionEnd hook fired (not on `/clear`). |
| Finished · PR #9 merged / Archived | Cleanup saw the merge (with run topics), or the ledger reads archived. This line is final and never edited again. |

- **Cadence.** The message is edited when the state changes. While the state stays the same and only its times age, it is edited at most once every 5 minutes. There is no heartbeat, and nothing polls GitHub or the pane.
- **It never acts.** A quiet or unknown status triggers no retry, redispatch, approval, merge or cleanup.
- **Restarts and replays** post nothing new. A post or edit that Telegram or Slack refused is retried a minute later. A post whose reply was lost is not retried, so there is never a second message, though that run then has none.
- **Gaps.** A long tool, such as a test suite, fires no hook until it ends. A plain-text question reads as "Turn ended". An MCP elicitation dialog, or a subagent's permission prompt while the gateway is down, creates no request, so it does not read as waiting (its Hermes ping still goes out). Without `notify_topics`, nothing records the merge, so the line stays on the pull request until the ledger is archived. Times use the gateway host's local clock.

### Cards on the board

- The **ledger card** is the record of one approval or one ad-hoc run. It is blocked when the builder's launch fails or its session ends without a pull request, and completed when the pull request is done. For an issue run, its `muster links: {...}` comment names the issue, the pane and the worktree.
- A **wait card** opens each time the builder stops to wait for you, and is archived once you answer, in the chat or in the pane. When muster delivers the question itself, the card takes no subscription, so muster's own message is your only notice. Questions asked after the pull request, such as when a reviewer asks for a change, still open a wait card.
- **Review cards** wake the coordinator: one per design version, and one per revised pull request head.
- **Cleanup warnings** ("Workspace kept · …", "Work finished · workspace kept …") are explained under [cleanup](#cleanup).

A card that should reach you is subscribed `notify+wake`: the gateway pings you, then queues a coordinator turn. Use `hermes kanban --board <board> show <card> --json` to read any card.

### Recovery

If a launch failed ("The coding agent did not start", "did not become ready", "its first prompt may not have arrived", "The launch was refused"), fix the cause, then run `hermes muster recover <ledger card id>`.

- For "may not have arrived", look at the pane first, and add `--resend` only if the pane shows no brief.
- Add `--adopt` when a launch refused a branch that already has commits ahead of the base, or a worktree muster did not make, and you want the builder to work on it as it is. When a launch reuses a checkout that has work in it, the brief tells the builder to keep that work.
- Never relaunch the same work on a new branch.

The coordinator never runs `recover` itself; it tells you when the retry is yours.

## Cleanup

`hermes muster cleanup` runs every 5 minutes. It touches only workspaces muster has a record for: issue worktrees, ad-hoc runs and analysis workspaces. `hermes muster cleanup --dry-run` reports what it would do without acting.

**Finished work.** A coding worktree is removed, and its branch kept, once its pull requests are merged and every pane has been quiet for 30 minutes. Quiet means the agents are idle or done and no job is running under a shell. A clean, pushed checkout at the merged head is removed as it is; agent scaffolding copied from the main checkout is backed up and deleted first.

**Anything else is preserved first.** That covers uncommitted or untracked files, local commits, and a HEAD past the merged head. Cleanup writes a self-contained git bundle of the history plus a stash-shaped commit of the index, the tracked files on disk and the untracked files git does not ignore. It verifies the bundle by fetching it back into an empty repository. The backup goes under `<data dir>/backups/cleanup/<time>-<name>/`, with a `manifest.json` holding the one `restore` command. Only then is the worktree removed with `--force`. Files that git ignores (`.env`, build output, dependencies) are never read or kept. Backups are never deleted automatically, and each manifest records its `bundle_bytes`.

**What keeps a worktree.** These cannot be preserved faithfully, so the worktree stays: a merge conflict in progress, an intent-to-add entry, a repository inside the checkout, submodules, or index flags that hide edits. A worktree also stays while it has an open decision request or an undelivered event. Any failed check restarts the 30-minute window, and a read that fails or comes back incomplete keeps the workspace.

**Work that ended without a merge.** Such a run's workspace is removed under the same clean, pushed and quiet checks once its ledger card is archived and no pull request from its branch is open. Nothing may still own the run: no open decision, no undelivered hook event, and no launch or recover under way.

**Retiring runs.**
- A run whose issue was closed as not planned or as a duplicate, with no open pull request, is retired: it gets one "retired" comment that keeps the close reason, then its ledger is archived.
- A run whose ledger is `done` and whose worktree is already gone, whoever removed it, is retired the same way, once no pull request from its branch is open and no prompt or event still waits. Its topic is then deleted.

**Warnings that ask you.**
- *"Workspace kept · pull request closed in <repo>":* a quiet run whose pull requests all closed unmerged, while its issue stays open. The card says what archiving the ledger does (it ends the run's prompts, deletes its topic and lets cleanup remove the workspace; the branch is kept) and how to keep the workspace instead.
- *"Workspace kept · unsaved work in <repo>":* a quiet coding worktree that has unsaved work cleanup may not remove, because the work is unfinished or cannot be preserved, and no open pull request. For unfinished work, the card offers two choices: commit, push and open a pull request, or archive the ledger so cleanup backs up the work and removes the workspace. For work that cannot be preserved, you resolve that first.
- *"Work finished · workspace kept in <repo>":* a resolved run whose worktree is still there a day later. This card goes to the main chat and names the worktree, the exact blocker, the ledger card and the safe next step. There is one card per blocker: the same blocker never pings again, a new blocker replaces the card, and a cleared one archives it. Archive the card to keep the worktree.

Cleanup never discards anything it has not preserved. The coordinator stays silent on cleanup warnings unless it knows something the card lacks, and it never pushes, commits, discards or removes anything for you.

## Channels

You never need to open herdr to decide anything: every routine decision can be made from the channel. Pane ids appear on messages only as provenance. The panes stay visible in herdr, where you can watch a run or type into it.

**Who may answer.** Only an approver listed in `notify_user_id` (or, for a DM, the DM's user) can tap or reply to a muster prompt, and only in the notify chat. Being allowed to talk to the Hermes agent is not enough. Anyone else's tap is refused and logged, and anyone else's message is never taken as an answer; it reaches the Hermes agent as an ordinary message. If you answer a builder's question by talking to the coordinator, it tells you to answer on the question's own message.

### Telegram

- Questions use Hermes's own clarify prompt. Tap an option, or tap Other and type, or reply to the message with text. For several choices, type the numbers (`1,3`). Hermes parses the reply.
- Permission prompts use Hermes's own approval card: the command as code, then Allow once and Deny. A reply to the card denies the prompt and passes your words to the builder. Hermes's `approvals.timeout` applies, and muster denies a prompt itself after 10 minutes at the latest.
- After a gateway restart, the questions still unanswered are shown again. Older copies say "Replaced after a restart", and answers already given are kept.
- Muster registers Telegram callback guards so that its prompts answer only the notify user, plus a `pre_gateway_dispatch` hook that swallows only your replies to its own messages.

### Run topics

Run topics are opt-in (`notify_topics: true`). Each run, whether an issue or an ad-hoc launch, gets one forum topic in the notify group, named `<short title> · #<issue>` or `<short title> · <branch>`; with several repositories configured, `<repo>#<issue>`. The title is cut at a word near 48 characters, and two live runs never share a name: the second gets ` (2)`. Everything about the run happens in its topic: its status message, questions, permission cards, designs, reviews, decision buttons, send-backs and re-reviews. Hermes's kanban notifier pings, and runs the coordinator's turn, in each card's subscription thread, so the coordinator's commentary lands in the topic too. General planning stays in the main chat (General).

- **Before you turn it on:** enable Topics in the group's settings, and make the bot an admin with the Manage Topics and Delete Messages rights. Telegram needs the second right to delete an archived run's topic. Muster never changes either setting.
- **At launch,** the gateway creates the topic with Hermes's `create_handoff_thread` while the launch waits up to 45 seconds. If Topics is off, a right is missing, the gateway is down, or the group cannot be checked after three tries, the run stays in the main chat. You get one message there saying why, and a `topic:` comment on the ledger card. Runs launched while topics were off stay in the main chat.
- **A lost create.** Telegram may create the topic while its reply is lost, and Hermes reports every failed create as "no topic". So muster calls create at most once per destination, and never retries a create that returned nothing. Instead it waits 20 seconds for Telegram's own "topic created" notice. If a notice for a topic the bot made carries the run's name, muster adopts that topic; a notice that names no run, or several, is ignored. Without a notice, the run stays in the main chat, with the usual message saying why. A notice that arrives later closes that topic as unused. A run therefore never has two live topics, though a lost reply with no notice can leave one empty topic open. Whether Telegram delivers this notice to the bot that made the topic has not been verified live. After your first launch with topics on, look in `$HERMES_HOME/plugin-data/muster/logs/gateway.log` for a `topic notice: thread …` line for each notice that arrived.
- **Answers are scoped to the topic.** A tap or reply counts only inside the run's topic, and only from the notify user. Text typed after "Other" goes to the prompt waiting in the topic it was typed in, so two runs' prompts never compete. A muster message found anywhere else (Hermes moves a message to General when its topic is gone) is not answered there: the tap says "Moved", and the request is shown again in its topic.
- **A deleted topic** is detected when a prompt lands outside it. Hermes resends a message whose thread is gone to General, and a tap there on a request's newest prompt marks the topic gone; so does a reopen that finds no thread. Nothing probes a topic, because Telegram has no way to read a topic's state and any call that could stand in for one changes it. Muster then creates a new topic, shows the open prompts there again, says so, and moves every card's subscription. A move counts only when Hermes's subscription list shows the card at the new topic and no longer at the old one. A card that has not moved is retried, backing off to once a minute. Until the deletion is detected, pings land in General.
- **When the run's work is resolved** (its ledger is `done` and the newest pull request from its branch is merged, with none open), its open build, feedback and design requests end. The topic gets "Finished. This topic is now closed." and is closed. Nothing about the worktree holds the close; worktree issues hold only the worktree's removal. A failed close is retried, backing off to once an hour. A pull request that is still open is in review, so the topic stays open.
- **A closed topic stays closed.** A restart, a scan, a replayed hook or a saved request never reopens it. Only a request made after the close, such as a new question or a new review, reopens it, unless the run is archived. A resolved run's topic reopened that way closes again on cleanup's next pass. A build or feedback request whose pull request was merged, closed or moved to another head is never shown again; it ends as stale.
- **Closing a topic by hand** is respected: nothing reopens it except a new request, the run's work goes on, and its older prompts can still be answered in the pane. Reopening a topic by hand resumes it: its open prompts show again, and it stays open until you close it.
- **When the ledger card is archived,** by you or by the board, the run is retired. Its open prompts end with "The run is archived", and its topic is deleted with its history within 30 seconds of the archive (5 minutes for a closed topic). The archived card keeps the pull request, proposals and comments, and `<data dir>/runs/<card>/conversation.json` keeps the topic's name and ids. A retired run is never probed, reopened or given a new topic, so deleting its topic by hand is final too. A delete that Telegram does not confirm (for example, without the Delete Messages right) closes the topic in the meantime and is retried, backing off to once an hour. Only a card that reads `archived` retires a run; a card missing from the board deletes nothing. An archived run's card subscriptions are removed (`hermes kanban notify-unsubscribe`), and a removal that fails is retried.

### Slack

Slack support is covered by the test suite against a fake Slack client. This repository records no live acceptance run in a real Slack workspace, so check it in yours before relying on it.

- **Settings.** Set `notify_platform: slack`, `notify_chat_id: C…` (the channel), `notify_user_id` (one or more member ids `U…`, separated by commas) and `notify_chat_type: group`. A missing or wrong setting is refused by `hermes muster` and logged by the gateway when Slack connects. Slack has no DM fallback.
- **Hermes owns the Slack app.** It reads `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`; muster reads no Slack secret. In the app's settings, turn **Interactivity** on (Socket Mode needs it for buttons), and invite the bot to the channel. Only one workspace is supported: muster posts with Hermes's first bot token.
- **Prompts.** Each question is its own message, with the question, one button per option and a "Your own words" box (press Enter to send). Questions that allow several choices use checkboxes and a Submit button. A permission prompt shows the command verbatim, with Allow once, Deny and a "Deny with a message" box. Hermes's own clarify and approval prompts are not used on Slack, because Hermes runs its own button handlers before any plugin's, so muster could not limit who answers them.
- **Who may answer.** Only an approver, in the configured channel. Anyone else's tap gets "Not authorized", visible only to them, and is logged. Thread replies are never taken as answers; they go to the Hermes agent as usual.
- **Restarts.** A prompt stays answerable across a gateway restart. Nothing is posted again, and answers already given to some questions of a request are kept. If the gateway dies between Slack accepting a post and muster saving its id, that prompt is posted once more, and both copies answer the same request once. An interrupted merge or send-back is read back from GitHub or the pane, never redone without a new tap.
- **Limits.** Hermes's `approvals.timeout` does not apply on Slack; an unanswered permission prompt is denied after 10 minutes by muster's own deadline. A command over 2900 characters, or a question with more options than Slack can show (25, or 10 when several may be picked), cannot be shown whole. You get a card saying so, and you answer in the pane.
- **Threads** (`notify_topics`). The bot posts one parent message per run, and the run talks under it. The parent shows the run's short title in bold, then its state and whether it needs you, then the issue as a link, for example "*Activity status* / Needs your decision: design v2 · since 14:05 · Issue #46". When the work is resolved, the parent says "Finished · PR #<n> merged"; a new request from the run still posts in the thread ("Reopened for a new request"). An archived run's parent says "Archived" and its prompts end. Muster deletes no Slack message. Deleting a parent by hand is unsupported, because later messages may land orphaned.
- **Formatting.** Agent and human text is escaped first, so it cannot ping anyone or create a link it did not mean. It is then converted with Hermes's own Markdown converter, so headings, bold and links render natively. A long card is split into several sections, never cut, and a full review gets its own message, linked from its card.
- **The coordinator in a thread.** Hermes's `thread_require_mention`, `strict_mention` and `allowed_channels` settings can keep your un-mentioned thread replies from reaching the Hermes agent. Check them if it does not answer in a run's thread.
- **Switching platform** leaves the old platform's topics or threads, and their kanban subscriptions, as they are. Open requests of a run whose conversation is on the other platform are posted at the top of the new channel, headed with the run's name.

## Prompts

Every instruction that muster gives an agent or a model is a file in [`prompts/`](prompts/README.md), one file per purpose, filled in when it is used:

- **Builder (issue run):** `launch-envelope.md` wraps `issue-dispatch.md`. That file includes `ask-the-human.md`, `builder-workflow.md`, and the production note (or `no-production-note.md`). `reused-worktree.md` is appended when a launch reuses a checkout that has work in it.
- **Builder (ad-hoc run):** `launch-envelope.md` wraps your brief plus `ad-hoc-run.md`, which includes the same `ask-the-human.md` and `builder-workflow.md`.
- **Builder, later in the run:** `build-revision.md` (with `build-revision-issue.md` or `build-revision-ad-hoc.md`) carries a send-back. `design-reply-approve.md` and `design-reply-revise.md` answer a design approval question. `duplicate-handoff.md` hands over a possible duplicate report.
- **Coordinator:** `escalation.md`, `review-standard.md` and `dispatch.md` are registered whole as the Hermes skills `muster:escalation`, `muster:review-standard` and `muster:dispatch`. `design-review-cue.md` and `cleanup-warning-cue.md` are lines on cards that route a wake to the right skill.
- **Model:** `duplicate-classifier.md` instructs the duplicate check.

`workflow_prompt_file` is the only override: it replaces `builder-workflow.md`. The other prompts are changed by editing their files. [`prompts/README.md`](prompts/README.md) lists each file's consumer, its placeholders and which text is treated as untrusted data.

## Security and disclosure

- Muster shells out to `gh`, `git`, `herdr` and `hermes`. It reads GitHub issues and timelines, the board's sqlite database (read-only), `$HERMES_HOME/.env` for the DM chat id, and the per-repo notes.
- It writes kanban cards, worktrees under `worktrees`, state under `$HERMES_HOME/plugin-data/muster/`, and per-pane agent settings (the hooks) in that data directory. Nothing global in your Claude Code config changes.
- The builder is interactive, runs as you, and is not sandboxed. Claude Code starts with `--permission-mode auto`, so it runs most tools without asking. The brief forbids it to push to the base branch, merge, approve, deploy or force-push, to edit `.github/`, CI, deployment config, secrets, lockfiles or agent-instruction files, and to add dependencies. Issue text never enters the brief and is treated as data.
- Human review of the pull request is the control: nothing merges or deploys without you. Muster merges only when you tap Merge on a recommendation, as a squash pinned to the reviewed head, using your own `gh` login.
- Inside the Hermes gateway, muster scans `<data dir>/decisions/` every 2 seconds, and registers only the selected platform's surfaces. On Telegram these are a callback guard and a `pre_gateway_dispatch` hook that swallows only your replies to its own messages. On Slack it is a handler for muster's own `muster_*` button and input ids, which posts and updates muster's own messages in the configured channel. With `notify_topics`, muster also creates, closes, reopens (only for new work) and, for archived runs, deletes forum topics in the notify group, and it reads Telegram's topic created, closed and reopened notices there. On Slack it posts and updates one thread parent per run and deletes nothing.

## Development

```bash
python3 -m pytest -q
hermes plugins validate .
```

The tests are fully mocked: `tests/fake_herdr.py` stands in for `herdr`, `gh` and `hermes`. Code that touches private Hermes names lives only in `muster/hermes_private.py`; see [`AGENTS.md`](AGENTS.md).

## Repository docs

- [`docs/learnings.md`](docs/learnings.md): pattern → consequence lessons from past work. Builders read it before planning.
- [`docs/review-lenses.md`](docs/review-lenses.md): the review lenses the builder's sub-agent reviewers use in this repository.
- [`docs/decisions/`](docs/decisions/README.md): dated decision records that explain why things are built as they are.
- [`docs/plans/`](docs/plans/README.md): dated design and implementation plans, kept as the record of past work. They are not operating instructions; this README describes current behavior.

## License

MIT. See `LICENSE`.
