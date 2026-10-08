---
name: run
description: When the human asks you to send coding work to a herdr pane (not a labeled issue), launch it with `hermes muster launch` so the run reports itself; open analysis workspaces with `hermes muster open`; and when an ad-hoc run card wakes you, tell the human what happened and, on a finished build, recommend one action.
---

# muster run

## Dispatch

Only when the human asks for coding work in a herdr pane.

1. Write the brief to a file. Say what to build and how to check it. Do not add "call back when done",
   "run hermes kanban complete" or any other callback line: the run reports itself.
2. Run, with a new branch name each time (the branch must be `feat|fix|chore|deps/<name>`):

   ```bash
   hermes muster launch --cwd <repo checkout> \
     --branch <feat|fix|chore|deps>/<name> --title "<short title>" --brief <file>
   ```

   Optional: `--base <branch>` (default `main`), `--model <model>` (default: the `agent_model` setting).
3. Exit 0 prints the run as JSON; tell the human the `card` id and the pane. Exit 1 prints why on stderr.
   If it names a failed step, the card is blocked; tell the human the step and the details (a failed
   subscribe pings no one, so your message is their only notice). The retry is theirs:
   `hermes muster recover <card>`. Never run it, and never launch the same work again on a new branch to
   get around it. If it says "already has run", pick a new branch name.

Never open a coding pane by hand (`herdr worktree create`, `herdr agent start`): that run has no card,
so nobody hears when it ends.

## Analysis workspace (no pull request)

When the human asks for research or analysis in a herdr pane (not coding), open the workspace with:

```bash
hermes muster open --cwd <dir> --label "<short label>"
```

It prints the record as JSON (`workspace`, `pane`, `terminal`). Start the agent in that `pane`. The record
lets cleanup close the workspace after 30 minutes with every pane quiet; no file is deleted. Never open
one with `herdr workspace create` yourself: without the record it stays open forever.

## Wake

A card whose body says "ad-hoc run", or a wait card whose body names one as its ledger:

1. Read it: `hermes kanban --board <the board setting> show <card id> --json` (a wait card's body names
   its ledger).
2. The ping already gave the title, the whole reason and the pane, or the pull request link. Do not repeat
   it. Add, in one or two sentences, only what it lacks:
   - Completed (ledger or a review card): review the build and record one recommendation with
     `hermes muster recommend`, exactly as the `muster:escalation` skill says. Never say merged, deployed
     or live unless `gh` shows it.
   - Wait card blocked by a question or permission prompt: it already went out with its options; add only
     new context, else reply `[SILENT]`. Idle 10 minutes without a finished pull request: say so and why (the reason is in the
     block text). The human answers by replying to the question's message, or in the pane.
   - Ledger blocked "session ended": the agent's session ended without a finished pull request.
   - Ledger blocked by a failed launch ("The coding agent did not start", "did not become ready", "its
     first prompt may not have arrived", "The launch was refused"): the human fixes the cause, then runs
     `hermes muster recover <card>`. For "may not have arrived", they look at the pane first and add
     `--resend` only if it shows no brief.
   - A card titled "Unsaved work in <repo>": see the `muster:escalation` skill.
3. At most one message per wake. Nothing new to say, or a muster decision is waiting on the human: reply
   exactly `[SILENT]` (Hermes sends nothing), and after `hermes muster recommend` always.

You must not type into or mirror the pane, start, stop or prompt any agent, move any card, comment on,
merge, approve or deploy anything, or send revision instructions yourself: merge and send-back happen only
when the human taps a button on your recommendation. Only the human decides.
