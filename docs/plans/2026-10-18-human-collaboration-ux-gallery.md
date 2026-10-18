# Human collaboration UX: message gallery and plan (checkpoint for approval)

v2 changes from v1, as Jason asked:
- Section 1's priority: an Approval under review reads "Ella reviewing", with both examples.
- Section 3's timing cases and recorded/delivered outcomes now cover Send revisions as well as
  Approve, with the binding rules spelled out.

Audit: `docs/plans/2026-10-18-human-collaboration-ux-audit.md` (Jason's approved brief, copied whole).
Base: origin/main at 07c7422, which includes PR #47 (one status message per run). PR #47 is merged but
not deployed. Nothing is implemented yet. This document is the gate.

## Decision needed

Approve this gallery and plan (this proposal version) so the build can start. Or send back the specific
messages or rules you want changed. Three sub-choices are folded in below with a recommendation. Each is
marked **Choice**.

## Legend

- **Today**: what Muster sends now, from the source at 07c7422.
- **Proposed**: what this change would send. No proposed control exists until it is built.
- "Ella" stands for the coordinator. See Choice A.
- Slack renders `*bold*`. Telegram renders **bold** through Hermes's MarkdownV2 send. Both are shown once
  where they look alike.

---

## 1. Main channel: five tasks at a glance

Slack thread parents are edited in place and never reposted. On Telegram these are the topic names. A
topic name cannot hold a status line, so the state goes in the run's one status message (PR #47).

**Today** (verified live in #46's thread):

```
muster · hermes-muster#46 Show confirmed build start and lightweight agent activity status · t_dff6c76e   ⏳ running
Launched 14:02 · brief receipt not confirmed yet.
```

**Proposed** (Slack parents, top level of the channel):

> *Agent activity status*
> Needs your decision: plan v2 · <Issue #46>
>
> *Topic reopen asks again*
> Working · last activity 3 min ago · <Issue #42>
>
> *Fix flaky cleanup test*
> Ella reviewing PR #51 · no action needed · <Issue #49>
>
> *Ad-hoc: bump herdr pin*
> Quiet 22 min · long tools can be quiet · branch `muster/bump-herdr`
>
> *Slack thread parents*
> Finished · <PR #44> merged

Rules:

- Line 1 is a short task name: the issue title, with any repo or product prefix dropped, cut at a word
  boundary near 48 characters. The full title stays in the linked issue. No `t_…` id, no `muster ·`
  prefix, no repeated repo name. An ad-hoc run uses its title, or else its branch.
- Line 2 is the state, then whether it needs you, then a native link. The link is built from the
  validated repo and number (`<https://github.com/…/issues/46|Issue #46>`), never from agent text.
- The state reads in this order. The first match wins:
  1. Needs your decision. Any open request the human must answer: a question or permission (other
     than an Approval still under review), or Ella's plan or build card.
  2. Ella reviewing, no action needed. A plan version or PR is waiting for her recommendation. That
     includes the builder's open `Approval` question for a version with no design card yet. That
     question alone never yields "Needs your decision". Early approval is still offered on its message,
     as the labelled exception (section 3).
  3. Feedback delivered, revision pending.

  Both cases side by side. Each gets its own regression test:

  > *Agent activity status*
  > Ella reviewing plan v2 · no action needed · <Issue #46>

  (only the builder's Approval question for v2 is open, with no review yet)

  > *Agent activity status*
  > Needs your decision: 1 question · <Issue #46>

  (the same pending review, plus an unanswered question or permission)
  4. PR #47's existing states, reworded: Dispatched, waiting for the builder · Started · Working ·
     Quiet N min · Turn ended, the builder is waiting at its prompt · Session ended.
  5. Finished, or Archived.
- Rows 2 and 3 are new. Today "waiting" counts open requests only, so a review in progress reads as
  "Working". Rows 4 and 5 keep PR #47's evidence and only change the words. "Dispatched" stays until
  the brief's receipt is confirmed.
- Telegram topic name: `Agent activity status · #46` (ad-hoc: `· branch`). The ledger id leaves the name. The
  issue number or branch keeps names unique among live runs. A name that matches several runs is
  already ignored when a lost topic is adopted.
- Details (ledger id, pane, worktree) are not dropped. They stay on the Kanban card and in the request
  file.

## 2. Dispatch and start

**Proposed**, run status message (Telegram), or the parent's second line (Slack):

> Dispatched · waiting for the builder to start

then, only after the brief's hash is seen (existing evidence):

> Started 14:03 · working

Start failures stay as they are today: blocked-card pings from the launch, worded by the coordinator.
Only the human words change: "Not started: the agent did not become ready. Next: run
`hermes muster recover` once the cause is fixed." The recovery command stays visible because it is the
next step.

## 3. Plan review pending, then one approval surface

The builder asks its `Approval` question. Ella's design review for that version comes later, or earlier.
Today both appear as separate cards with buttons: the builder's question (Approve / …) and Ella's review
(Approve / Send back / Do nothing). Tapping Approve on Ella's card before the builder asks fails with
"tap again on the new message".

**Proposed: review pending.** This is the builder's question message, shown while no review exists yet:

> *Plan v2 ready* · Agent activity status
> Ella is reviewing. No action needed.
>
> To decide without her review, answer the builder's question:
> [Approve without Ella's review] [Request changes] … (the builder's own options)
> Your own words: (input on Slack / Other on Telegram)

- The builder's options and answer text are unchanged. Only the lead text changes. On Slack the first
  button reads "… without Ella's review" when its label is an approve. Telegram's clarify buttons are
  Hermes's: they keep their labels, and the lead sentence carries the warning.

**Proposed: your decision.** Ella's card is now the one authoritative surface:

> *Plan v2 · Agent activity status*
> *Ella recommends: approve*
>
> One status message per run, edited in place. It shows confirmed start and the last activity.
> • Limit: a quiet tool can still be running.
>
> Approve lets the builder implement plan v2. Nothing is merged or deployed.
> [Approve plan v2 (recommended)] [Send revisions to builder] [Leave pending]
> Full plan and review ↑ (link to the detail message)

At the same moment, the builder's question message is edited to:

> *Plan v2 ready* · decide on Ella's review below.

Its buttons are gone. The request stays open, so a reply or a pane answer still counts. First answer
wins (existing `answer_part` guard).

### The four timing cases (smallest complete approach)

Every rule below applies to **both** of the card's sending actions: **Approve plan vN** and **Send
revisions to builder**. Each keeps its own words. A revision is never called an approval anywhere: not
on the card, not in an outcome, not in the reply sent to the builder. The exact text of the reply
("Approve: the human approved vN." or "Revise vN before building. Revisions from the human: …", with
any typed additions) is fixed when you tap. It is stored in `intent.reply` on the design request and
bound to that version, and it is delivered word for word.

| Case | Today | Proposed: Approve | Proposed: Send revisions |
|---|---|---|---|
| Review before question | Tap fails: "tap again on the new message" | The tap is recorded on the design request (`executing`, waiting). When the builder's question for **the same version** appears, the scan answers it once. Card: "Approval of v2 recorded · the builder hasn't asked yet; it gets this when it does." | Same path, with the exact authorized revisions kept. Card: "Revisions for v2 recorded · the builder hasn't asked yet; they go when it does." |
| Newer version before the builder asks | (not handled) | The pending delivery is cancelled, and nothing is sent. "Plan v3 replaced v2. Your approval of v2 was not sent." | "Plan v3 replaced v2. Your revisions for v2 were not sent." |
| Question before review | Two live cards | The question shows "Ella reviewing" (above). When the review arrives, the question message is edited to point at it and loses its buttons. | Same. |
| Answered before review | Review card appears; its tap fails | `recommend-design` refuses: "already answered (Approve)". The skill posts the review as a plain note, with no buttons: "You approved v2 before my review finished. I'd still change X: …" Your answer stands. Nothing asks again. | Same refusal, and the same note. Your original answer, whatever it was, is never re-sent or overridden. |
| Bundled questions (approval + others) | Ella's tap fails: "answer it on its own message" | Ella's tap answers only the `Approval` question, through the same per-question `answer_part` the channels use. The other questions keep their own messages and controls. Card: "Approval recorded · awaiting your other answer above." | "Revisions recorded · awaiting your other answer above." |

Binding, unchanged where it exists today and kept in every new path:
- **Exact-request recovery binding.** `intent.question` is written before the answer is attempted. A
  recovery (restart, or a re-run of the scan) acts only on that question id. It never picks another
  question, even one for the same version.
- **First answer wins.** The approval part goes through `answer_part`, under the same lock as a direct
  tap.
- **No retargeting.** A human answer that lands in between can be a direct tap on the builder's
  question, a typed reply or a pane answer. When one does, Ella's card fails with "You already answered
  this on the builder's question: <answer>. Nothing else was sent." The accepted decision is never
  moved to a different question or version.

No new orchestration. The changes are one scan check and the existing answer paths:
- `decisions._answer_approval` keeps its version binding and answers a part, not the whole request.
- The gateway scan applies a waiting design answer to a newly open question for the same pin, and
  cancels it when a newer version exists.
- `handle()` skips the approval slot while an open design request covers it.

### Recorded vs delivered (both actions)

Ella's design request stays `executing` until the builder's question ends. Only bridge evidence changes
"recorded" to "delivered": the question ends `done` with the hook's "Sent to Claude".

| Moment | Approve | Send revisions |
|---|---|---|
| After the tap | ✓ Approval of v2 recorded · delivering to the builder | ✓ Revisions for v2 recorded · delivering to the builder |
| Other bundled answers missing | Approval recorded · awaiting your other answer above | Revisions recorded · awaiting your other answer above |
| Bridge evidence | ✓ Approval delivered to the builder | ✓ Revisions delivered to the builder · revision pending |
| The question ends any other way | Not delivered: the builder stopped waiting. Nothing was approved. | Not delivered: the builder stopped waiting. The revisions were not sent. |

Today the card says "Approval sent to the agent" or "Revisions sent to the agent" as soon as the answer
is recorded, before the pane picks it up.

## 4. Independent questions

> *Question 1 of 2* · Agent activity status
> Which status should a quiet tool show?
> [Quiet N min (recommended)] [Working]
> Your own words: …

> *Question 2 of 2* · Agent activity status
> Keep the 5-minute refresh?
> [Yes] [No]

After you answer one: "✓ Answered: Quiet N min · 1 question still open". The other card is untouched.
- **Restart fix:** today a Telegram reboot re-presents every question and clears the answers already
  given (`partial={}`). Proposed: it re-presents only unanswered questions and keeps the parts. Slack
  already keeps them.
- Free text, Slack: "Type here to answer the builder. A thread reply goes to Ella, not the builder."
  Today it says only "type your own answer below".

## 5. Build: PR review pending, then the recommendation

**Today**, the completion ping: `Ready for review: <url>; review it with the muster:escalation skill`.
This internal cue reaches the human.

**Proposed** completion ping (card summary):

> PR #47 opened · Ella is reviewing. No action needed.

Then Ella's card:

> *PR #47 · Agent activity status*
> *Ella recommends: send back two fixes*
>
> • A failed final update can leave a finished task showing "working".
> • Missing start evidence can hide a PR that is ready for review.
>
> Tests pass, but these two behaviors need fixing first.
> Merge squashes into main. Deploy on merge: none (per the repo's production note).
> [Review feedback… (recommended)] [Merge PR] [Leave PR open]
> Full review ↑

- "Review feedback…" ends in "…" because it opens a confirmation. That is today's second step, kept.
- The deploy consequence is always on the card: the note's answer, or "Deploy on merge: unknown".

## 6. Send back, delivery, revision

Confirmation card (today: "Send as written" / "Don't send"):

> *Send this to the builder?* · PR #47
> 1. Make a failed final status edit retry; don't mark it final.
> 2. Let a verified review outrank a missing receipt.
> [Send feedback to builder] [Don't send]
> Your own words: add to it, or start with `replace:`

The full authorized text is shown once, verbatim, and never shortened. Today's 1,500-character rule
for plan revisions stays. A build's feedback card already shows its whole text.

Then:

> ✓ Feedback delivered · revision pending. No action needed.

or, when the hook's hash is not seen (existing `_retry`):

> Feedback sent; receipt not confirmed. A new "Send?" card follows. Nothing was sent twice.

Revision arrives (existing re-review card):

> PR #47 updated (2 new commits) · Ella is re-reviewing. No action needed.

The old build card is already staled by `rereview`. Proposed wording: "Replaced: PR moved to a1b2c3d".

Plan revisions on Ella's design card ("Send revisions to builder") are delivered directly, as today:
the label names the action, and the card shows the full revisions. The rule for both is the same: a
button that sends says "Send … to builder", and a button that opens a confirmation ends in "…".

## 7. Permission

**Proposed** (Slack. On Telegram, Hermes's approval card keeps its layout and we write only its lead text):

> *Permission needed* · Agent activity status
> Run the test suite in the worktree.
> ```uv run pytest -q```
> Allow once lets the builder run this one command. Muster can't confirm it ran.
> [Allow once] [Deny]   Deny with a message: …

Outcomes:
- "Allowed once · sent to the builder. Not confirmed run."
- "Denied · sent to the builder."
- On expiry: "Expired: denied after 10 min. The builder may ask again."
- When the builder stops waiting: "The builder stopped waiting. Nothing was allowed."

The command is still shown verbatim (Slack `rich_text_preformatted`, Telegram Hermes's code block). The
existing rule stays: a command too long to show sends you to the pane.

## 8. Final outcome

Build card outcomes, already verified against GitHub:
- "✓ Merged a1b2c3d (squash). Not deployed by Muster."
- "Merge queued · not merged yet."
- "Merge not confirmed: GitHub reads OPEN. Check the PR before tapping again."

Thread close: the parent becomes `Finished · <PR #44> merged`. The Slack finish note ("Work finished:
this thread is done. History kept; the worktree is cleaned up separately.") is dropped, because the
parent says it. On Telegram the topic closes as today.

## 9. Cleanup exception

**Today**: Hermes's ping shows the card ("Unsaved work in hermes-muster: Cleanup kept a workspace … Open
Herdr workspace w_…"), and then Ella paraphrases it.

**Proposed** card summary, so the ping reads whole and Ella stays `[SILENT]`:

> *Work finished · workspace kept* · Agent activity status
> PR #47 is merged. One untracked file blocks safe cleanup: `uv.lock`.
> • Commit or push it: cleanup then removes the workspace by itself.
> • Discard it in the workspace: cleanup then removes the workspace. The branch is kept.
> • Keep the workspace: archive this warning card. That only silences this warning.

"Archive this warning card" and "archive the run's ledger" are never shortened to "archive". The
ledger action (closed-PR case) says: "Archive the run's ledger: retires the run, deletes its Telegram
topic, and cleanup removes the workspace. The branch is kept." No cleanup buttons are added.

## 10. Stale and ended controls

A tap on an ended card gets a reply only you see (Slack ephemeral, or Telegram's callback answer):
- "Already answered: Approve plan v2." The answer is unchanged.
- "Replaced by the newer message below."
- "This decision ended: the builder stopped waiting. Nothing was sent."
- "Not shown: PR #47 moved to a1b2c3d. Ella's new review follows."
- Telegram after a restart (today: "Superseded: see the newer message"): "Replaced after a restart · use
  the newer message below."

Restart: decisions live in the request files. Nothing in this change re-sends an effect, and none of the
consolidated messages grants authority. A tap still binds to its own request id and version.

## Slack vs Telegram

| | Slack | Telegram |
|---|---|---|
| Task list | Channel thread parents, edited | Topic names, plus one status message per topic |
| Formatting | Muster's Block Kit sections; agent text escaped, then Hermes's `format_message` (Markdown → mrkdwn, broadcast mentions escaped) | Hermes's send/edit (MarkdownV2) |
| Long full review | Detail message posted before the card, split into ≤3,000-char sections on paragraph breaks; the card links to it by permalink | Detail message before the card; Hermes splits over 4,096 |
| Free text | Per-question input answers the builder; a thread reply goes to Ella | Reply to the message, or Other |
| Thread end | Parent edited to Finished | Topic closed |

---

## Implementation plan (after approval)

Four slices. Each is test-first, tested, and committed on its own. Every slice reuses PR #47's
`progress`, the existing request store and the channel modules. There is no new module, framework,
state machine or dependency.

1. **Copy, parents and formatting.**
   - `conversation.name` gets a short name with no ledger id.
   - `slack.parent_line` and `progress.state` get the human states and native issue/PR links.
   - `shared.render` gets Markdown lead lines: Slack runs them through `T.adapter.format_message` after
     `slack_text` escaping; Telegram already formats.
   - `recommend` and `recommend-design` take an authored `--summary` file, capped at about 900
     characters. They refuse an over-long summary rather than cut it, and fall back to the review when
     no summary is given.
   - The full review goes to a detail message posted just before the card. Slack links it by
     `chat_getPermalink`.
   - The escalation skill is rewritten around the summary contract: recommendation, one reason,
     material caveats, deploy consequence.
2. **One surface per decision.**
   - In `decisions`: `_answer_approval` answers the `Approval` part, through the same lock-guarded
     `answer_part`, for Approve and Send revisions alike. A waiting design answer (its exact
     `intent.reply`) is applied to a new question for the same pin. A newer version cancels it. The
     `intent.question` recovery binding and first-answer-wins are kept, and nothing is retargeted
     after a human answer that lands in between.
   - In `gateway.handle`: the covered approval slot is skipped, and the question message is edited to
     point at the review.
   - `recommend-design` refuses for a version already answered.
   - Telegram re-presents only unanswered questions.
3. **Status truth.**
   - The design request stays `executing` until its question ends, so the card says recorded, then
     delivered.
   - Two new progress rows, "Ella reviewing" and "Feedback delivered · revision pending", are read from
     existing request and review evidence.
   - Outcome strings for permissions, feedback and merge are reworded.
4. **Noise and cleanup.**
   - Completion and re-review summaries become human copy, and the skill keys on card kind, not the
     cue text.
   - Cleanup card text is rewritten as in section 9, and the skill keeps Ella `[SILENT]` on it.
   - The Slack finish note is dropped.
   - Stale-tap wording is reworded.

**Tests.**
- Renderer fixtures for every gallery message, in Slack blocks and Telegram text.
- Slack payloads are checked against Block Kit limits. The section/markdown schema rules are taken from
  Slack's docs and Hermes's own `sanitize_blocks`, not just a mock that accepts any send.
- The four timing cases, plus newer-version cancel, each for both Approve and Send revisions.
- An intervening human answer: no retarget.
- Recovery acts only on `intent.question`.
- Parent state: an Approval under review alone reads "Ella reviewing". Add an unrelated question or
  permission and it reads "Needs your decision".
- A bundled question set answered partially, across a restart.
- Stale taps.
- A long review with a deploy caveat near the limit: the caveat stays on the card, and the full text
  lands in the detail message.
- No `**` or `#` leaks into Slack text.
- Five parents scan cleanly.
- The full suite runs before the PR.

## Scope, safety, non-goals

- No Hermes-generated notices (tool traces, memory/skill notes, onboarding). No adapter patches, no
  private paths.
- No dashboard, reminder engine, transport framework or recovery subsystem.
- Version binding, `answer_part` first-answer-wins, per-request authorization and escaping are
  unchanged. Commands and authorized feedback are shown verbatim and never cut.
- No live Slack/Telegram posts, gateway restart, install or merge. The PR is opened unmerged. The live
  walkthrough needs your separate approval.

## Choices

- **A. Coordinator name.** The name "Ella" lives only in `~/.hermes/SOUL.md`, as free text, not as a
  setting. *Recommend:* one optional setting, `coordinator_name`, defaulting to "the reviewer"; you set
  it to Ella. The alternative is fixed neutral copy ("Review in progress").
- **B. Direct approval before review.** *Recommend:* keep it, labelled "… without Ella's review" (Slack)
  or with the lead sentence (Telegram). The alternative hides the builder's buttons until Ella's review
  lands. That is simpler, but you could not approve early.
- **C. Full detail location.** *Recommend:* a detail message in the run's thread or topic, posted just
  before the card. The alternative is a Slack modal: cleaner, but it adds a view handler and does
  nothing for Telegram.

## Trade-offs and limits

- The detail message adds one message per decision in the thread. Runs without topics put it in the main
  channel.
- Short names cut long titles at a word boundary, by rule, not by a model. A cut name can still read
  oddly; the full title is one tap away.
- "Delivered" still means the hook printed the answer, not that Claude applied it (decision-channels
  rule). The copy says "delivered to the builder", never "applied".
- Hermes's own Slack/Telegram noise remains until you take it up separately. If you want, the optional
  follow-up is Hermes's existing `display.tool_progress` setting.

---

## As built (approved v2; deviations recorded during the build)

- **Coordinator name.** Choice A was approved as a setting. `coordinator_name` defaults to "Coordinator", not
  "the reviewer", because the copy reads "Coordinator is reviewing". Set it to "Ella".
- **Summary contract.** `--summary` is refused, never cut, over 900 characters. A build summary must contain
  the word "deploy" (the deploy consequence). Without `--summary` the card shows the whole review, as before.
- **Detail message.** It is posted when the request has a summary, or when the review (or a send-back)
  runs over 3,000 characters. A plan's detail message carries the review and the plan's full text. Slack
  links to it by `chat.getPermalink`. If that call fails, the card has no link and the detail stays just
  above the card.
- **Slack text.** Text is escaped, then converted by Hermes's `adapter.format_message`. Without that method
  (an older Hermes), Muster converts only its own `**bold**`. Long text is split into sections at
  paragraph, line or space breaks, never cut.
- **Telegram.** Cards are plain text, because the clarify path HTML-escapes its text. Only the detail
  message gets Hermes's Markdown formatting.
- **Topic names.** Topic names are `<short title> · #<issue>` (or `· <branch>`, or `repo#n` when several
  repos are configured). A second live run with the same name gets ` (2)`.
- **"Delivered".** Plan approvals and revisions say "delivered" on the bridge's hook evidence. They carry
  no ✓, because the decision-channels rule keeps ✓ for outcomes Muster verified itself (merge read-back,
  feedback prompt hash).
- **Telegram close note.** It is now "Finished. This topic is now closed." It makes no merge claim, because
  the close can follow cleanup of an archived run.
- **Not built (deferred, as agreed).** Hermes tool traces, memory and skill notices, onboarding notices.
  The optional host setting for those is Hermes's existing `display.tool_progress`.
- **Card order (from the branch review).** On build and plan cards, the action's consequence and the whole
  revisions now come before the summary or review. The gallery showed them after it. A card longer than
  its platform's limit can then cut only the review, and the review is always whole in the detail message.
- **Slack links in agent text.** A Markdown link in agent text is never turned into a Slack link. Hermes's
  converter would make `[x](!here)` a ping. Muster's own links (issue, detail) are built separately.
- **A missing detail never hides a decision.** If the detail post fails, the card still goes out, and the
  failure is logged. A detail longer than one Slack message's 50 blocks spans several messages.
- **Cleanup copy follows PR #48 (undoable cleanup).** Finished work with unsaved changes is now backed up and
  removed, so it no longer gets a card. The "unsaved work" card covers only work that is not finished, or
  that cleanup cannot back up. For unfinished work it offers two choices. Commit, push and open a pull request,
  and nothing is removed. Or archive the run's ledger card, and cleanup backs the work up and removes the
  workspace. For work cleanup cannot back up, it asks the human to resolve that first. Section 9's
  "Commit or push … cleanup then removes it" no longer holds.
- **An early plan tap is reconciled against ended questions first (re-review cycle 1).** A tap made before
  the builder asked ends in either of two cases. In the first, the human answered that version's question,
  here or in the pane; their answer stands. In the second, the builder asked after the tap and stopped
  without this reply, and the tap ends as not delivered. This holds even if the question started and ended
  between two gateway scans. A later question for the same version never gets the old tap.
