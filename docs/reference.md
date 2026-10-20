# Muster reference

Details behind the [README](../README.md): commands, the rules each step follows, cards, recovery,
cleanup, prompts and what Muster touches on your machine. For installation see [setup](setup.md); for
chat behavior see [channels](channels.md).

- [Commands](#commands)
- [Starting work](#starting-work): [issue runs](#issue-runs), [ad-hoc runs](#ad-hoc-runs),
  [analysis workspaces](#analysis-workspaces), [duplicate reports](#duplicate-reports)
- [During a run](#during-a-run): [how the builder works](#how-the-builder-works),
  [questions and permissions](#questions-and-permission-prompts), [design approval](#design-approval),
  [build review and merge](#build-review-and-merge)
- [Cards](#cards) · [Recovery](#recovery) · [Cleanup](#cleanup) · [Prompts](#prompts) ·
  [Security and disclosure](#security-and-disclosure)

## Commands

You run few of these by hand. Cron runs `tick`, `cleanup` and `flush`; your Hermes agent runs `launch`,
`open`, `recommend` and `recommend-design`; the builder and its hooks run `hook`. `recover` is yours.

| Command | Run by | What it does |
|---|---|---|
| `hermes muster tick [--dry-run]` | cron, every minute | Starts newly approved issues. `--dry-run` checks the setup and launches nothing. |
| `hermes muster launch --cwd <dir> --branch <type>/<name> --title "<title>" --brief <file> [--base <branch>] [--model <model>]` | Hermes agent | Starts an [ad-hoc run](#ad-hoc-runs). |
| `hermes muster open --cwd <dir> --label "<label>"` | Hermes agent | Opens an [analysis workspace](#analysis-workspaces). |
| `hermes muster recover <card> [--resend] [--adopt]` | you | Retries a failed launch. See [recovery](#recovery). |
| `hermes muster cleanup [--dry-run]` | cron, every 5 minutes | Removes finished workspaces. `--dry-run` reports what it would do. |
| `hermes muster flush` | cron, every minute | Delivers hook events a pane could not deliver itself. |
| `hermes muster recommend <ledger> --pr <url> --head <sha> --choice merge\|send-back\|nothing --review <file> [--summary <file>] [--feedback <file>]` | Hermes agent | Records the build review you decide on. |
| `hermes muster recommend-design <ledger> --proposal v<n>:<sha> --choice approve\|revise\|judgment --review <file> [--summary <file>] [--feedback <file>]` | Hermes agent | Records the design review you decide on. |
| `hermes muster hook <event> [--card <card>]` | builder and its hooks | Events: `notification`, `prompt`, `stop`, `session-end`, `permission` (hooks); `done <PR url>`, `propose <file>`, `duplicate <n> same\|different` (builder). |

To read any card: `hermes kanban --board <board> show <card> --json`.

## Starting work

### Issue runs

1. The approver applies `label` (default `agent-ready`) to an issue in a configured repository.
2. Within a minute, `tick` checks the issue's timeline: the newest event for the label must come from
   `approver_login` with `approver_id`. A label applied or re-applied by anyone else refuses the issue.
   Each approving label event makes one ledger card, so re-applying the label later starts a new run.
3. Muster creates a herdr worktree on branch `<branch_prefix><issue number>`, cut from the base branch,
   starts Claude Code in it, and submits the brief as its first prompt. Issue text never enters the brief:
   the builder reads the issue with `gh` and treats it as data.
4. The builder comments "Picked up by muster." on the issue and works it.
5. It opens one pull request whose body ends with `Closes #<n>`, then runs `hermes muster hook done <PR url>`.
   Muster accepts it only if `gh` names the run's branch as the pull request's head. The ledger card is
   completed, and your Hermes agent [reviews the build](#build-review-and-merge).

Issues opened by a bot listed in `auto_approvers` can be approved by that bot; see
[setup](setup.md#automatic-approvals). A bot re-applying `label` starts a new run, as yours does;
re-applying only its own label does not. When no entry matches, only you can approve.

### Ad-hoc runs

Your Hermes agent's `muster:dispatch` skill writes your request to a brief file and runs
`hermes muster launch`. The details:

- **Branch:** must match `(feat|fix|chore|deps)/<name>`, with a name of lowercase letters, digits, `.`,
  `_` and `-`. Each branch can be used for one run only.
- **Base:** `--base`, else the repository's `@base` in `repos`, else origin's default branch.
- **Model:** `--model`, else `agent_model`.
- **Output:** the run as JSON (exit 0), or the reason on stderr (exit 1). A failed step blocks the card;
  the retry is yours with `recover`.
- **Brief:** your brief, then a footer naming the card, branch and base, the same "ask the human" and
  "how to work" text issue runs get, and the rule never to push to the base, merge, deploy or force-push.
  A brief that already states the approach and scope counts as the approved design.
- **Completion:** the builder does not report it. Each time its turn ends, Muster checks GitHub. The run
  is finished when exactly one open or merged, non-draft pull request goes from the branch into the base,
  its head is the worktree's HEAD, and no tracked file has uncommitted changes. If the builder sits idle
  for 10 minutes without that, a wait card tells you.

### Analysis workspaces

For research that ends in no pull request, your Hermes agent runs
`hermes muster open --cwd <dir> --label "<short label>"`. It opens a herdr workspace that Muster records
and prints `workspace`, `pane` and `terminal` as JSON. No card tracks it. Cleanup closes it after every
pane has been quiet for 30 minutes; closing deletes no file.

### Duplicate reports

Error trackers often file several issues for one incident. Before an automatic approval launches, Muster
makes one model call through Hermes (auxiliary task `muster_dedupe`) comparing the issue with the
repository's live issue runs. The model can only name one of those runs, or none.

- **No duplicate, or any failure** (no model, timeout, unusable answer): the issue launches normally. A
  failure is noted on its card ("dedupe check failed").
- **Possible duplicate:** the issue's card is blocked with "Possible duplicate of …; not dispatched" and
  gets no worktree. Once the live run's builder is idle with no question open, Muster hands it the
  question. The builder decides:
  - **Same incident:** it comments on the report, adds `Closes #<n>` to its own pull request, and runs
    `hermes muster hook duplicate <n> same`. When that pull request merges, GitHub closes the report and
    the held card is archived.
  - **Different incident:** it runs `hermes muster hook duplicate <n> different`, and the next tick
    launches the report after checking its approval again.
- **You can decide instead:** `hermes muster recover <held card>` launches the report; closing the issue
  drops it. A reopened issue is not dispatched again on its old approval.
- **Notices:** the block pings you once, and at most one more card follows if the live builder is gone or
  nothing was answered within 24 hours.

Issues you labeled yourself are never checked.

## During a run

### How the builder works

Every brief includes the house workflow, [`prompts/builder-workflow.md`](../prompts/builder-workflow.md),
unless `workflow_prompt_file` replaces it:

- **Bugs:** reproduce, find the root cause, write a failing test, fix at the source. An architectural or
  multi-file fix is handled like a feature.
- **Features:** read the code, then write a short design: problem, approach, what was cut and why, open
  questions. **You approve the design** before any code is written.
- **Plan:** after approval, the builder writes a step-by-step plan to `docs/plans/YYYY-MM-DD-<slug>.md`
  and a fresh sub-agent attacks it until it holds. You are not asked to approve the plan.
- **Build:** tests first. A sub-agent reviews each task, and a fresh one reviews the whole branch, using
  the repository's `docs/review-lenses.md` if it has one.
- **Pull request:** one. Its body lists every review finding with severity, verdict and outcome, the
  probe log, the test and lint commands run, and any deviation from the plan.
- Trivial one-file changes skip the design and the plan.

### Questions and permission prompts

The builder asks with Claude Code's AskUserQuestion tool, because a plain-text question pings no one.
Each pane runs Claude Code hooks that call `hermes muster hook`. When a dialog waits for you, Muster opens
a wait card and records a decision request under `<data dir>/decisions/`, and the gateway shows it to you.
Answer in chat or in the pane; the first answer wins.

- **Permission prompts** are decided once each; there is no "allow for the session". An unanswered prompt
  is denied and the builder is told why. A subagent's prompt works the same way and names the subagent;
  while Muster waits, the pane cannot answer it.
- **Fail closed:** if anything goes wrong, such as the gateway being down, the hook makes no decision and
  the pane's own dialog decides. Muster never allows anything on error.
- **When Muster cannot deliver a question itself,** the wait card pings you the ordinary Hermes way.
- Every request keeps an audit trail of its status changes with times and results.

`<data dir>` is `$HERMES_HOME/plugin-data/muster`.

### Design approval

1. The builder writes its whole design to a file and runs `hermes muster hook propose <file>` (an ad-hoc
   builder adds `--card <card>`). Each distinct text becomes the next version, `Proposal v<n> <sha>`,
   saved as a ledger comment and as `<data dir>/runs/<ledger>/proposals/v<n>.md`. Earlier versions are
   kept. A proposal is capped at 48 KB, and GitHub tokens in it are redacted.
2. The builder asks with AskUserQuestion, with the header `Approval`. Muster refuses that question, and
   tells the builder why, if no version has been saved, the saved version was already put to you, or
   another wait card is open.
3. Each version wakes your Hermes agent to review it. You get its review, the full design, and a card with
   **Approve design v<n>**, **Send revisions to builder** and **Leave pending**. Until the review
   arrives, the builder's question says your agent is reviewing; you can still approve without its review.
4. One decision is made per version. A tap made before the builder asks is kept and given to it when it
   asks about that version; a newer version cancels it. If you answered the builder's question first,
   that answer stands. A tap never acts on another version.

Your Hermes agent cannot answer the builder's question itself.

### Build review and merge

When the pull request is ready, the run's status message says "<coordinator> is reviewing PR #<n> · nothing
to decide yet" (no separate ping). Your Hermes agent reads the diff, the surrounding code, the checks, the builder's review log and
the production note, and records one recommendation: merge, send back, or nothing. You get its full
review, then a decision card whose summary (at most 900 characters) states what a merge deploys.

- **Leave PR open** records your choice.
- **Merge** first re-checks that the pull request is still the run's own (same branch, base and
  repository, not a fork or a draft), that its head is the reviewed commit, and that GitHub reports it
  mergeable (`mergeStateStatus` CLEAN, HAS_HOOKS or UNSTABLE). Required checks are GitHub's call, so
  without branch protection a red check does not block it. Muster then runs
  `gh pr merge --squash --match-head-commit <sha>` with **your own** `gh` login, not the bot's, and reads
  back MERGED before saying it merged. A merge never implies a deploy.
- **Review feedback…** shows the proposed revision instructions. Send them, add your own words, or start
  your words with `replace:` to replace them. They go to the same builder in the same pane and count as
  sent only once the pane confirms they arrived; a busy or absent builder gets nothing, and you get
  **Retry**.

**Re-review.** After a send-back, the builder pushes a revision (an issue builder runs `hook done` again).
Once the pull request is clean and non-draft at a new head, the status message says your agent is
reviewing it again, and you get a new recommendation. Buttons on older recommendations answer "stale".

## Cards

Each run is tracked on Muster's Hermes kanban board.

- **Ledger card:** the record of one approval or ad-hoc run. Blocked when the launch fails or the session
  ends without a pull request; completed when the pull request is done. An issue run's
  `muster links: {...}` comment names the issue, pane and worktree.
- **Wait card:** opened each time the builder waits for you, archived once you answer.
- **Review cards:** wake your Hermes agent, one per design version and one per revised pull request head.
- **Cleanup warnings:** see [cleanup](#cleanup).

Cards that should reach you are subscribed `notify+wake`: the gateway pings you, then gives your Hermes
agent a turn. Review starts are subscribed `wake` (a turn, no ping), since the status message already says the
review started: the review cards, and the ledger once it completes with an open pull request. A ledger that
blocks is switched back to `notify+wake` first, so a block always pings. Your Hermes agent never types into a pane on its own, answers the builder, moves a card or
merges; it tells you what happened and recommends. The one exception is a message you ask it to send to a
running agent, which it delivers with `herdr agent prompt`. Its instructions are the skills `muster:escalation`,
`muster:review-standard` and `muster:dispatch`.

## Recovery

If a launch failed ("The coding agent did not start", "did not become ready", "its first prompt may not
have arrived", "The launch was refused"), fix the cause and run `hermes muster recover <ledger card>`.

- **"May not have arrived":** look at the pane first. Add `--resend` only if the pane shows no brief.
- **A branch with commits ahead of the base, or a worktree Muster did not make:** add `--adopt` to let the
  builder work on it as it is. A reused checkout's existing work is kept.
- **Never relaunch the same work on a new branch.**

Your Hermes agent never runs `recover`; it tells you when the retry is yours.

## Cleanup

`hermes muster cleanup` runs every 5 minutes and touches only workspaces Muster recorded: issue
worktrees, ad-hoc runs and analysis workspaces. Cleanup never discards anything it has not preserved.

- **Finished work:** a worktree is removed, and its branch kept, once its pull requests are merged and
  every pane has been quiet for 30 minutes (agents idle or done, no job running in a shell).
- **Unsaved work is backed up first:** uncommitted or untracked files, local commits, or a HEAD past the
  merged head. Cleanup writes a git bundle of the history plus a commit of the index, tracked files and
  untracked non-ignored files, verifies it, and saves it under `<data dir>/backups/cleanup/<time>-<name>/`
  with a `manifest.json` holding the `restore` command. Ignored files (`.env`, build output,
  dependencies) are never read or kept. Backups are never deleted automatically.
- **What keeps a worktree:** state a backup cannot capture faithfully (a merge in progress, an
  intent-to-add entry, a nested repository, submodules, index flags hiding edits), an open decision
  request, an undelivered event, or any failed or incomplete check.
- **Work that ended without a merge:** removed under the same checks once its ledger card is archived and
  no pull request from its branch is open.
- **Retiring runs:** a run whose issue was closed as not planned or duplicate, with no open pull request,
  or whose ledger is done and worktree already gone, gets a "retired" comment and its ledger is archived.

**Warnings that ask you:**

- *"Workspace kept · pull request closed in <repo>":* the pull requests closed unmerged while the issue
  stays open. Archive the ledger to end the run and let cleanup remove the workspace (the branch is kept),
  or keep it as the card explains.
- *"Workspace kept · unsaved work in <repo>":* unsaved work and no open pull request. Commit, push and
  open a pull request, or archive the ledger so cleanup backs up the work and removes the workspace. Work
  that cannot be preserved you resolve first.
- *"Work finished · workspace kept in <repo>":* a resolved run's worktree is still there a day later. The
  card names the blocker and the safe next step, and is replaced or archived as the blocker changes.
  Archive it to keep the worktree.

Your Hermes agent never pushes, commits, discards or removes anything for you.

## Prompts

Every instruction Muster gives an agent or a model is a file in [`prompts/`](../prompts/README.md):

- **Issue builder:** `launch-envelope.md` wraps `issue-dispatch.md`, which includes `ask-the-human.md`,
  `builder-workflow.md` and the production note (or `no-production-note.md`). `reused-worktree.md` is
  added when a launch reuses a checkout with work in it.
- **Ad-hoc builder:** `launch-envelope.md` wraps your brief plus `ad-hoc-run.md`, which includes the same
  `ask-the-human.md` and `builder-workflow.md`.
- **Later in a run:** `build-revision.md` (with `build-revision-issue.md` or `build-revision-ad-hoc.md`),
  `design-reply-approve.md`, `design-reply-revise.md` and `duplicate-handoff.md`.
- **Your Hermes agent:** `escalation.md`, `review-standard.md` and `dispatch.md` are the skills
  `muster:escalation`, `muster:review-standard` and `muster:dispatch`. `design-review-cue.md` and
  `cleanup-warning-cue.md` route a card's wake to the right skill.
- **Model:** `duplicate-classifier.md`.

`workflow_prompt_file` is the only override; change the others by editing their files.
[`prompts/README.md`](../prompts/README.md) lists each file's consumer, placeholders and untrusted text.

## Security and disclosure

- **Shells out to** `gh`, `git`, `herdr` and `hermes`. **Reads** GitHub issues and timelines, the board's
  sqlite database (read-only), `$HERMES_HOME/.env` for the DM chat id, and the production notes.
- **Writes** kanban cards, worktrees under `worktrees`, state and per-pane Claude Code hook settings under
  `$HERMES_HOME/plugin-data/muster/`. Your global Claude Code config is not changed.
- **The builder is not sandboxed.** It is interactive, runs as you, and starts with
  `--permission-mode auto`, so it runs most tools without asking. Its brief forbids pushing to the base
  branch, merging, approving, deploying, force-pushing, editing `.github/`, CI, deployment config, secrets,
  lockfiles or agent-instruction files, and adding dependencies. Issue text is treated as data.
- **Your review is the control.** Nothing merges or deploys without you. Muster merges only on your Merge
  tap, as a squash pinned to the reviewed commit, with your own `gh` login.
- **In the gateway,** Muster scans `<data dir>/decisions/` every 2 seconds and registers only the selected
  platform's handlers: on Telegram, callback guards and a `pre_gateway_dispatch` hook that swallows only
  your replies to its own messages; on Slack, handlers for its own `muster_*` buttons and inputs. With
  `notify_topics` it creates, closes, reopens (only for new work) and, for archived runs, deletes
  Telegram forum topics in the notify group, and reads Telegram's topic notices there. On Slack it posts
  and updates one thread parent per run and deletes nothing.
