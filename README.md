# Muster

Muster is a Hermes plugin that turns your coding-agent
subscription into a small software factory you run from chat. You approve work in GitHub, or ask for it
in Telegram or Slack. Muster starts a coding agent for it in a visible herdr pane on your machine, sends
you its questions and designs, has your Hermes agent review the result, and merges only when you tap
Merge.

## Why use it

- **Use the subscription you already pay for.** Each run is an ordinary interactive Claude Code session,
  so it draws on your Claude plan rather than billing you per API call. How much work that covers depends
  on your plan, your workload and its usage limits.
- **See the work.** Every run is a real terminal pane in herdr. Watch it or type
  into it at any time.
- **Decide from your phone.** Questions, permission prompts, design approvals and merges arrive as
  messages with buttons. Nothing merges without you.
- **No leftovers.** Finished worktrees are removed, and unsaved work is backed up first.

Muster supports **Claude Code** only. Because each run is a plain herdr pane, other coding agents, or
several side by side, could be added later; they are not supported today.

## How a run goes

1. **You start it:** label a GitHub issue (`agent-ready` by default), or ask your Hermes agent for the work.
2. **Muster launches it:** a new git worktree and herdr pane, Claude Code with a brief, and a card on
   Muster's Hermes kanban board.
3. **The builder asks you:** questions and permission prompts arrive in your chat. For a feature, it
   proposes a design and waits for your approval before writing code.
4. **It opens one pull request.** Your Hermes agent reviews it and recommends merge or send-back.
5. **You decide:** Merge, send feedback to the builder, or leave it open.
6. **Muster cleans up** the worktree after the merge.

To install and configure Muster, see [setup](docs/setup.md).

## Start work

### From a GitHub issue

Use this for work you have already written up. Apply the approval label (`agent-ready` by default) to an issue in a configured
repository. Only the GitHub account you configured as the approver can start work this way; a label from
anyone else is refused.

Within a minute Muster starts a builder on branch `muster/<issue number>` (the prefix is configurable). It comments "Picked up by
muster." on the issue, asks you what it needs in chat, and opens one pull request that closes the issue.
Issues labeled `bug` are worked as bugs (reproduce, root cause, failing test, fix); the rest as features
(design first).

A monitoring bot, such as an error tracker, can also start work on its own issues:
see [automatic approvals](docs/setup.md#automatic-approvals).

### Ask for it in chat (ad-hoc run)

Use an ad-hoc run when you want a coding agent to do something now, without first writing and labeling a
GitHub issue. Tell your Hermes agent what you want and in which repository:

> Start a muster run in ~/Code/app: add a `--json` flag to `app export`. Keep the text output as it is.

Your Hermes agent writes your request into a brief and launches the run. A new branch (`feat/…`,
`fix/…`, `chore/…` or `deps/…`), worktree and pane open, and it tells you the run's card id. Questions
and permission prompts arrive in the same chat as for issue runs.

If your request already states the approach and scope, as above, the builder builds straight away.
Otherwise it proposes a design for you to approve first. You review one pull request into the
repository's base branch, exactly as for an issue run.

Command and flags: [`hermes muster launch`](docs/reference.md#ad-hoc-runs).

### Research without a pull request

Ask your Hermes agent for research or analysis in a herdr pane, and it opens an analysis workspace. No
card tracks it, and cleanup closes it once it has been quiet for 30 minutes, without deleting any file.
See [analysis workspaces](docs/reference.md#analysis-workspaces).

## What you decide

Every decision is a message with buttons in your chat; you can also answer in the pane.

- **Questions** from the builder: tap an option or answer in your own words.
- **Permission prompts:** Allow once or Deny. Nothing is allowed for a whole session, and an unanswered
  prompt is denied.
- **Designs:** your Hermes agent reviews each version first. Approve it, send revisions, or leave it
  pending. No code is written for a feature before you approve.
- **Pull requests:** your Hermes agent reviews the build and says what a merge deploys. Merge, send
  feedback to the same builder, or leave it open. A merge is a squash of exactly the reviewed commit, made
  with your own `gh` login.

Each run also has a status message, edited in place, that shows whether it is working, quiet or waiting
for you. See [channels](docs/channels.md) for how this looks on Telegram and Slack, including one forum
topic or thread per run.

## Cleanup

Muster removes a worktree once its pull request is merged and its panes have been quiet for 30 minutes.
The branch is kept. Uncommitted or unpushed work is backed up first, and a worktree whose state cannot be
backed up faithfully is kept, with a card telling you why. See [cleanup](docs/reference.md#cleanup).

If a launch fails, the run's card says why; fix the cause and run `hermes muster recover <card>`. See
[recovery](docs/reference.md#recovery).

## Safety

- **You are the control.** Nothing merges or deploys without your tap, and your Hermes agent only
  recommends.
- **The builder is not sandboxed.** It runs as you, in Claude Code's auto permission mode, so it runs
  most tools without asking. Its brief forbids pushing to the base branch, merging, deploying,
  force-pushing, editing CI, deployment config, secrets or lockfiles, and adding dependencies.
- **Issue text is data.** It never enters the builder's instructions.
- **Only your listed approvers** can answer Muster's prompts, and only in the configured chat.
- **On error, nothing is allowed.** A permission prompt Muster cannot handle falls back to the pane's own
  dialog.

Full list of what Muster reads, writes and registers: [security and disclosure](docs/reference.md#security-and-disclosure).

## Documentation

- [Setup](docs/setup.md): requirements, install, every setting.
- [Channels](docs/channels.md): Telegram, Slack, run topics, the status message.
- [Reference](docs/reference.md): commands, run details, cards, recovery, cleanup, prompts, security.

## Development

```bash
python3 -m pytest -q
hermes plugins validate .
```

The tests are fully mocked: `tests/fake_herdr.py` stands in for `herdr`, `gh` and `hermes`. Code that
touches private Hermes names lives only in `muster/hermes_private.py`; see [`AGENTS.md`](AGENTS.md).

## Repository docs

- [`docs/learnings.md`](docs/learnings.md): lessons from past work. Builders read it before planning.
- [`docs/review-lenses.md`](docs/review-lenses.md): the lenses this repository's reviewers use.
- [`docs/plans/`](docs/plans/README.md) and [`docs/decisions/`](docs/decisions/README.md): historical
  designs and decisions, not descriptions of current behavior.

## License

MIT. See `LICENSE`.
