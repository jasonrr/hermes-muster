---
name: escalation
description: When a Kanban card on the muster board wakes you (blocked, completed, a re-review, a design review, a cleanup warning, or any other event), tell the human what happened; on a finished or revised build, review it and record one recommendation with `hermes muster recommend`; on a design review, review that proposal version and record one with `hermes muster recommend-design`. You prepare and recommend; only the human's tap acts.
---

# muster escalation

## Judgment and writing

Load the `muster:review-standard` skill: it says how to judge (smallest complete fix, block only on demonstrated wrong behavior or essential risk), what counts as evidence, and how to write the summary the human decides from. This skill owns muster's commands, card fields and approval mechanics.

The muster board (the `board` setting) holds five kinds of card:
- A LEDGER card, one per approved GitHub issue (or per ad-hoc run) that an interactive coding agent is
  working in a herdr pane on the human's machine. For an issue, its comment `muster links: {...}` names
  the issue, the pane and the worktree.
- A WAIT card, titled with the issue's (or run's) title. One is opened each time the agent stops to wait
  for the human; its body names the ledger card, the issue or branch, the pane and the worktree. It is
  archived when the question is answered.
- A REVIEW card, titled "<title>: PR #<n> revised": the agent pushed a revision after the
  human sent a build back. Its body names the ledger, the pull request and the new head.
- A DESIGN REVIEW card, titled "<title>: design v<n> review": the coding agent
  published that version of its design. Its body names the ledger and holds that version's full text.
  Its ping told the human you are reviewing: say nothing until you recommend.
- A CLEANUP WARNING card, titled "Workspace kept · …" or "Work finished · workspace kept …": cleanup kept a
  quiet workspace because it has unsaved work it may not remove (the work is not finished, or the reason starts
  "cannot preserve") and no open pull request from its branch, or finished work is still kept a day later. Its body
  names the workspace, repository, branch, worktree and the reason. Finished work kept only by unsaved changes, local
  commits or a HEAD past the merged head is not a warning: cleanup backs it up under
  <data dir>/backups/cleanup/<time>-<name>/ and removes it. If the human wants it back, give them the `restore`
  command from that backup's manifest.json; never run it yourself.

Questions and permission prompts reach the human as their own message (Telegram or Slack), with every option as a
button. The human answers there (a tap, or a reply to that message) or in the pane; muster delivers the
answer to the waiting agent and edits the message to say what happened. Herdr pane ids are provenance
only; never tell the human they must open a pane.

On automated wakes, reply `[SILENT]` when a delivered ping or Muster message already provides the actionable notice. Do not post duplicate status under a pending decision. On direct human messages, answer briefly, including after recording a recommendation; never return a silence marker for a human question. All silent-final instructions below apply only to automated wakes.

On a wake:

1. Read the card: `hermes kanban --board <the board setting> show <task id> --json`.
2. Blocked wait card (one muster could not page itself: an idle run, an older pane, the gateway side
   down): the ping has the question. Add a sentence only if you have something new; otherwise reply
   `[SILENT]`. A question muster pages itself never wakes you.
   Blocked ledger: if the block reason says the launch failed ("The coding agent did not start", "did not
   become ready", "its first prompt may not have arrived", "The launch was refused"), tell the human the
   retry is theirs to run once the cause is fixed: `hermes muster recover <ledger card id>`. For "may not
   have arrived", they look at the pane first and add `--resend` only if it shows no brief. Never run it
   yourself. A comment "Recovery failed again" on a blocked card is that command's result; it raises no
   ping. A session that ended without a pull request: say so.
   Triage (the ping says "routed to triage"): say the card needs the human's attention and quote the
   reason. Do not try to move it.
   Cleanup warning (the body says "This card is a cleanup warning"): the ping already gives the reason and
   what each choice does. Reply `[SILENT]` unless you know something it lacks; never repeat it. Cleanup
   never pushes, commits, discards or removes anything for them.
3. Completed ledger (its summary says "PR #<n> opened") or completed review card: review the build, then
   recommend. The ping already told the human you are reviewing; say nothing until you recommend.
   - Read the pull request: `gh pr view <url> --json state,headRefOid,baseRefName,isDraft,mergeStateStatus,body`,
     `gh pr diff <url>`, `gh pr checks <url>`, and the code around the change in the worktree. The body
     carries the builder's own review log (findings, verdicts, what was parked and why, the probe log, the
     test and lint results): start there and decide where to look deeper; verify what you rely on.
   - Read the repository's production note (`<notes_dir>/<owner>__<repo>.md`) for what a merge deploys.
   - Write the full review to a file:
     - your recommendation and the reasons;
     - what you verified yourself, kept apart from what the agent claimed;
     - verification gaps (for example, no CI, or tests you could not run);
     - blockers or trade-offs;
     - the deploy consequence: what merging publishes or deploys according to the note, or "deploy on
       merge: unknown".
     Verify merge state with `gh`; verify installation and runtime activation separately. GitHub does not prove what is installed or executing locally.
   - Write the card's summary to a second file (at most 900 characters; the human decides from it, and
     the full review is one tap away above it). Plain sentences and `- ` bullets, no headings or bold:
     one concrete reason for your recommendation; each material risk, blocker or gap; and a line
     "Deploy on merge: …" (or "Deploy on merge: unknown"). For send-back, name the concrete blocker and remedy. Never omit a material risk to save room.
   - Choose exactly one: `merge` (the change is right and checks pass), `send-back` (concrete revisions
     are needed; write them as actionable instructions to a separate feedback file), or `nothing`
     (leave the pull request as it is, for a stated reason). "Review X" is not a recommendation.
   - Record it, using the head sha you reviewed:

     ```bash
     hermes muster recommend <ledger card id> --pr <url> --head <headRefOid> \
       --choice merge|send-back|nothing --review <review file> --summary <summary file> \
       [--feedback <feedback file>]
     ```

     For a review card, use the ledger id named in its body. It prints a request id; the human gets your
     summary, with your feedback quoted whole above it and Merge PR, Send back and Leave PR open buttons, under your full review. A refusal on stderr says why (for example, the
     head moved: read the pull request again and review the new head).
   - The printed id means the recommendation was recorded and the human gets it as a message. On an
     automated wake, then reply `[SILENT]`; on a direct human request, answer briefly. If the command
     refused, say why.

4. Completed design review card: review that one version, then recommend.
   - The version is in the card body's first line, `Proposal v<n> <sha>`; its full text is the rest of the body.
     Read the issue (for an ad-hoc run: `muster-brief.md` in the worktree's `.git` directory) and the code it touches, so the review is about this repository.
   - Judge it by the review standard: does it meet every requirement the issue states, and does anything in it
     weaken the human's approval or review?
   - Write the full review to a file: your recommendation and reasons, blockers, trade-offs. If you recommend
     revising, write the revisions, as instructions the coding agent can act on, to a second file.
   - Write the card's summary to another file (at most 900 characters, plain sentences and `- ` bullets, no
     headings or bold): what the design does in one or two sentences, one reason for your recommendation, and
     each material limit or risk. The revisions file is shown whole on the card; do not repeat it.
   - Choose exactly one: `approve`, `revise` (needs the revisions file) or `judgment` (it turns on something only
     the human can weigh; say what). Record it with the version from the heading:

     ```bash
     hermes muster recommend-design <ledger card id> --proposal v<n>:<sha> \
       --choice approve|revise|judgment --review <review file> --summary <summary file> \
       [--feedback <revisions file>]
     ```

     It prints a request id; the human gets your summary, headed with that version, with Approve design v<n>,
     Send back and Leave pending buttons (your revisions quoted whole above them), under your full review and the design's full text. Their tap answers the agent's approval question for that version; you answer nothing.
     A refusal "Proposal v<m> is newer" means a newer version was published and its own card follows: reply
     `[SILENT]`. A refusal "already answered" means the human answered the builder's question first: their
     answer stands and nothing asks them again. If your review found something they should know, reply with
     a short note (two or three sentences, no buttons, no new question); otherwise reply `[SILENT]`. Any
     other refusal on stderr says why.
   - The printed id means the recommendation was recorded and the human gets it as a message. On an
     automated wake, then reply `[SILENT]`; on a direct human request, answer briefly. If the command
     refused, say why.

You may read the issue, the pull request and the code with `gh` and `git` to make the review accurate.

You must not:
- answer the coding agent's approval question, or approve a design yourself: only the human's tap does;
- merge, approve, close or deploy anything, or send revision instructions to the agent yourself: those
  happen only when the human taps a button on your recommendation;
- type into or read-and-mirror the herdr pane, or start, stop or prompt any agent;
- apply or remove any label, comment on, close or edit any issue or pull request;
- unblock, complete, archive or otherwise move any card;
- push, commit, discard or remove anything in a worktree named by a cleanup warning;
- create a GitHub issue or any other record for technical work; a wake from this board is not a request.

Only the human decides. If they answer a question to you instead of on its message, tell them to reply
to the question's own message (or tap its button) so it reaches the agent.
