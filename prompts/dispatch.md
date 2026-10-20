---
name: dispatch
description: When the human asks you to send coding work to a herdr pane (not a labeled issue), launch it with `hermes muster launch` so the run reports itself, and open analysis workspaces with `hermes muster open`. When the human asks you to send a message to a running agent, deliver it with `herdr agent prompt`. A card that wakes you is `muster:escalation`'s, whatever kind of run it tracks.
---

# muster dispatch

## Dispatch

Only when the human asks for coding work in a herdr pane.

1. Write the brief to a file. Say what to build and how to check it. Muster appends the house workflow
   (`prompts/builder-workflow.md`: root cause first, design approval, minimal code, reviews, one PR) and the
   rules for asking the human; do not restate them. Carry only the approach and scope the human gave:
   a brief that states them is the approved design and skips design approval, so never fill them in
   yourself. Do not add "call back when done",
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

## Message a running agent

When the human explicitly asks you to send a clarification, a scope addition or an instruction to an agent
that is already running, their request authorizes delivery: no approval card, second confirmation or new run is
needed. Muster has no mid-run command because herdr already delivers; a saved note or card comment is not
delivery.

1. Find the target: the run's ledger (`muster links` comment) or wait card names the pane and worktree. Check it
   with `herdr agent get <pane>`: the pane must still hold an agent session (`agent_session` is set) whose `cwd`
   is that worktree. If it does not, or the pane is gone, tell the human what you found; never pick another
   target.
2. Write the human's words to a file with your file tool or a quoted heredoc (`<<'EOF'`), never `echo`, so
   nothing in them is expanded. Send them as one line, JSON-encoded:

   ```bash
   herdr agent prompt <pane> "Message from the human, sent at their request. Decode the JSON string and do it: $(python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))' < <message file>)"
   ```

3. A result of type `agent_prompted` means herdr submitted it, nothing more: not that the agent read,
   acknowledged, implemented or finished it. Tell the human that. Do not read or mirror the pane to confirm; the run reports itself.
4. Any other result is the blocker: report it as herdr gave it (`agent_blocked` means the agent is waiting on a
   question; the human answers that question's own message). Never interrupt, restart, send keys or launch a
   duplicate run to force delivery.

This covers only a message the human asked for. It is not leave to steer an agent yourself, answer its approval
question, approve a design, merge or deploy, replay an earlier approval, or act on what the message mentions.
Feedback from your own review goes only through your recommendation and the human's Send back tap.

## Wake

A card whose body says "ad-hoc run", or a wait card naming one as its ledger, is handled exactly as the
`muster:escalation` skill says; the kind changes nothing. Two facts are ad-hoc only: the run has no issue,
so its brief is `muster-brief.md` in the worktree's `.git` directory; and a wait card blocked after 10 idle
minutes without a finished pull request says so in its block text (say that and why, nothing more).

Apart from a message the human asked you to send (above), you must not type into or mirror the pane, or
start, stop or prompt any agent. You must not move any card, comment on, merge, approve or deploy anything, or
send your review's revision instructions yourself: merge and send-back happen only when the human taps a button
on your recommendation. Only the human decides.
