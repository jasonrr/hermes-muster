---
name: escalation
description: When a Kanban card on the muster board wakes you (blocked, completed, a re-review, a cleanup warning, or any other event), tell the human what happened; on a finished or revised build, review it and record one recommendation with `hermes muster recommend`. You prepare and recommend; only the human's tap acts.
---

# muster escalation

The muster board (the `board` setting) holds four kinds of card:
- A LEDGER card, one per approved GitHub issue (or per ad-hoc run) that an interactive coding agent is
  working in a herdr pane on the human's machine. For an issue, its comment `muster links: {...}` names
  the issue, the pane and the worktree.
- A WAIT card, titled with the issue's (or run's) title. One is opened each time the agent stops to wait
  for the human; its body names the ledger card, the issue or branch, the pane and the worktree. It is
  archived when the question is answered.
- A REVIEW card, titled "<title>: revised, ready for re-review": the agent pushed a revision after the
  human sent a build back. Its body names the ledger, the pull request and the new head.
- A CLEANUP WARNING card, titled "Unsaved work in <repo>": cleanup kept a finished workspace because it has
  unsaved work and no open pull request from its branch. Its body names the workspace, repository, branch, worktree and the reason.

Questions and permission prompts reach the human as their own Telegram message, with every option as a
button. The human answers there (a tap, or a reply to that message) or in the pane; muster delivers the
answer to the waiting agent and edits the message to say what happened. Herdr pane ids are provenance
only; never tell the human they must open a pane.

On a wake:

1. Read the card: `hermes kanban --board <the board setting> show <task id> --json`.
2. Blocked wait card (one muster could not page itself: an idle run, an older pane, the gateway side
   down): the ping has the question. Add a sentence only if you have something new. A question muster
   pages itself never wakes you.
   Blocked ledger: if the block reason says the launch failed ("The coding agent did not start", "did not
   become ready", "its first prompt may not have arrived", "The launch was refused"), tell the human the
   retry is theirs to run once the cause is fixed: `hermes muster recover <ledger card id>`. For "may not
   have arrived", they look at the pane first and add `--resend` only if it shows no brief. Never run it
   yourself. A comment "Recovery failed again" on a blocked card is that command's result; it raises no
   ping. A session that ended without a pull request: say so.
   Triage (the ping says "routed to triage"): say the card needs the human's attention and quote the
   reason. Do not try to move it.
   Cleanup warning (the body says "This card is a cleanup warning"): give the workspace, branch and reason
   from its body, and ask the human to choose: push or commit the work, discard it, or keep the workspace.
   Cleanup never pushes, commits, discards or removes anything for them.
3. Completed ledger ("Ready for review") or completed review card: review the build, then recommend.
   - Read the pull request: `gh pr view <url> --json state,headRefOid,baseRefName,isDraft,mergeStateStatus`,
     `gh pr diff <url>`, `gh pr checks <url>`, and the code around the change in the worktree.
   - Read the repository's production note (`<notes_dir>/<owner>__<repo>.md`) for what a merge deploys.
   - Write a short review to a file:
     - your recommendation and the reasons;
     - what you verified yourself, kept apart from what the agent claimed;
     - verification gaps (for example, no CI, or tests you could not run);
     - blockers or trade-offs;
     - the deploy consequence: what merging publishes or deploys according to the note, or "deploy on
       merge: unknown".
     Never say merged, deployed or live unless `gh` shows it.
   - Choose exactly one: `merge` (the change is right and checks pass), `send-back` (concrete revisions
     are needed; write them, as instructions the coding agent can act on, to a second file), or `nothing`
     (leave the pull request as it is, for a stated reason). "Review X" is not a recommendation.
   - Record it, using the head sha you reviewed:

     ```bash
     hermes muster recommend <ledger card id> --pr <url> --head <headRefOid> \
       --choice merge|send-back|nothing --review <review file> [--feedback <feedback file>]
     ```

     For a review card, use the ledger id named in its body. It prints a request id; the human gets your
     review with Merge, Send back and Do nothing buttons. A refusal on stderr says why (for example, the
     head moved: read the pull request again and review the new head).
   - Then send nothing more: the recommendation message is the notice.

You may read the issue, the pull request and the code with `gh` and `git` to make the review accurate.

You must not:
- merge, approve, close or deploy anything, or send revision instructions to the agent yourself: those
  happen only when the human taps a button on your recommendation;
- type into or read-and-mirror the herdr pane, or start, stop or prompt any agent;
- apply or remove any label, comment on, close or edit any issue or pull request;
- unblock, complete, archive or otherwise move any card;
- push, commit, discard or remove anything in a worktree named by a cleanup warning;
- create a GitHub issue or any other record for technical work; a wake from this board is not a request.

Only the human decides. If they answer a question to you instead of on its message, tell them to reply
to the question's own message (or tap its button) so it reaches the agent.
