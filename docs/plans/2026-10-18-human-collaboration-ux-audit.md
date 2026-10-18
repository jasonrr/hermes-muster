<!-- Versioned copy of Jason's approved UX audit (source: ~/muster-human-collaboration-ux.md, copied 2026-10-18). Sections marked "Verified"/"Confirmed" describe Muster as installed at 3703f29; "Proposed" sections and message examples are design, not shipped behavior. PR #47 (activity status) merged to main after this audit as 07c7422; it is not deployed. -->

# Muster: collaborate with a human

## Recommendation

Design around the human's next decision, not the system's internal events. Give each task/version one authoritative decision surface. Keep the default message brief; make the full evidence available without requiring it to understand or authorize the action.

Status: UX audit and proposed feature brief. No implementation, dispatch, publication, configuration change, or deployment performed.

## Evidence and scope

- Read recent conversation history covering approval handoffs, stale questions, activity visibility, send-back acknowledgements, and cleanup.
- Inspected installed Muster at `3703f29527fffe14ac4ceda170d158642cf2141b`, including channel renderers and decision execution. Installed run/escalation skills have local additions; those are not all committed behavior.
- Read the entire available Slack thread for issue #46: 25 messages including parent, `has_more=false`. Source: https://radicalcandorllc.slack.com/archives/C0CA6P9TVV2/p1792282669801959
- Confirmed raw Markdown headings/bold markers in actual Block Kit section text, duplicate task identity, and reviews reaching the 3,000-character section cap. The renderer drops option descriptions first and then truncates the question body. This can hide decision-relevant caveats, not merely make messages ugly.
- The same thread also contains memory-recall announcements, skill reads, terminal-command previews, and a home-channel setup notice. These are Hermes-generated noise in the Muster experience; changing Muster templates alone will not remove them. Review their host settings separately rather than rebuilding the host.
- PR #47's activity-status source was inspected separately. It is not present in the installed revision audited here; its proposed visibility is not credited as shipped.
- Confirmed Slack's official `mrkdwn` rules: https://docs.slack.dev/messaging/formatting-message-text/
- This is a source/history audit, not a new live rendering or interaction test. No fresh prompts were posted or tapped.

## The core interaction

For each plan version:

1. **Reviewing:** “Plan v2 ready. Ella is reviewing; no action needed.”
2. **Your decision:** replace that presentation with recommendation, short reason, material caveat, and precise controls.
3. **Applying:** remove/disable obsolete controls; show what was recorded and what is still happening.
4. **Outcome:** confirm delivery or verified result. If confirmation is missing, say so and give one safe next step.

The builder's approval request and coordinator's recommendation may remain separate internal records. They must not look like two independent decisions to the human. Reuse existing messages and request correlation where feasible; do not introduce a new orchestration framework just to unify presentation.

One authoritative surface means one per actual decision, not one giant mutable message for the entire task. Unrelated questions remain independently answerable. Partial answers must survive. A genuine new decision can produce a new card.

Current code creates a design recommendation separately and supersedes other design recommendations, not the original approval question. The handoff also has limitations: no original question yet, an already-ended question, and multi-question approval requests. The design must account for those paths rather than simply deleting an old prompt. Do not silently remove an existing direct-approval capability: if it is retained before review, label it “Approve without Ella's review” as a deliberate exception.

## Message contract

Every actionable message answers:

- **Which task?** Short recognizable title; issue/PR link as secondary identity.
- **What is happening?** Human state, not board/storage state.
- **Does it need me?** Explicit decision or “No action needed.”
- **What happens if I choose?** Concrete authorization and destination.
- **What matters to this decision?** One reason plus essential risks, uncertainty, and deployment consequences.

Default target: roughly 40–90 words for ordinary decision summaries, with short bullets and one thought per sentence. This is a design target, not a truncation algorithm. Commands, material risks, and authorized feedback must not be cut to hit a budget. Move supporting analysis to accessible full detail. Never hide a blocker behind “Details.”

Use human terms: “Plan ready,” “Ella reviewing,” “Needs your decision,” “Feedback delivered,” “Revision pending,” “PR merged.” Avoid “ledger,” raw card IDs, hashes, pane IDs, internal hook names, and routine defensive disclaimers in the primary view. Keep exact identifiers available in details and keep the underlying version binding unchanged.

## Surface-by-surface changes

### 1. Main-channel dispatch announcements and thread parents

Treat the channel's top-level task list as a first-class UX surface, not merely a container for better thread replies. These announcements are what a human scans to find and distinguish concurrent work.

Verified actual Muster Pilot parent messages:

- `muster · hermes-muster#46 Show confirmed build start and lightweight agent activity status · t_dff6c76e   ⏳ running`
- `muster · hermes-muster#42 Questions resolved in herdr reappear when their Telegram topic is reopened · t_903749e7   archived`

The first repeats the product name inside a machine-oriented identity string, exposes an internal Kanban ID, uses a long issue title as interface copy, and ends with a vague state. Neither gives a clear first-glance answer to whether the human needs to act. The internal ID is useful for correlation, not for the normal human view.

Proposed parent presentation, using #46 as a copy example rather than a claim about its present state:

**Agent activity visibility**
Dispatched · waiting for the builder to start
Muster · Issue #46

Make “Issue #46” a native link; use the recognizable repository/product label once as secondary context. Preserve the full source issue title in the linked issue rather than mechanically repeating it. Only change to “Started” after confirmed receipt, then update the same parent to “Needs your decision,” “Ella reviewing,” or the verified final state. The parent must not retain “running” while the thread is waiting for the human. No extra routine channel posts for each state change.

Acceptance: scan the main channel with five task parents, without opening threads. Identify the work and the tasks needing attention. No raw `t_…` IDs, duplicated prefixes, diagnostic paths, or hashes in the default view; details retain exact identifiers. Include both automatically dispatched issue runs and ad-hoc runs, and long/repetitive source titles. Verify parent state against the authoritative task/decision evidence rather than merely mirroring a board status.

Source: `muster/channels/slack.py:424–425,480–529`, `muster/conversation.py::name`; live messages https://radicalcandorllc.slack.com/archives/C0CA6P9TVV2/p1792282669801959 and https://radicalcandorllc.slack.com/archives/C0CA6P9TVV2/p1792276310156519.

### 1a. Launch and confirmed start

Separate “Dispatch requested,” “Brief received,” and observed activity. Do not call dispatch success confirmed agent work. Start failures must say “Not started” or “Start uncertain,” followed by one safe next step. Keep resend warnings where delivery is uncertain. Internal IDs and recovery commands belong in operator detail unless needed now.

### 2. Running, quiet, and stopped

Keep one status presentation, edited rather than reposted. Show last observed activity, not a reassuring spinner. Silence is not proof of failure. A turn ending is not task completion. Outstanding questions outrank unrelated worker activity. Integrate with the existing #46/#47 work rather than creating a competing progress system.

### 3. Questions and choices

Lead with the question. Describe only meaningful differences between choices. Mark the recommendation without repeating the full option prose. Make free-text behavior explicit: current Slack ordinary thread replies go to the coordinator, while per-question inputs answer the builder. Do not imply that replying anywhere delivers an answer. For multiple questions show which remain unanswered.

### 4. Plan review and approval

Show review in progress before asking the human to decide. When ready, present one authoritative plan/version decision with the review and controls together. Retire or clearly redirect the redundant approval presentation without inventing an answer. If the human answered first, acknowledge that outcome; do not issue a second apparent approval request.

### 5. Build completion and review

“PR opened — Ella is reviewing. No action needed.” is different from “Ready for your decision.” A completed Kanban card is not the same as a completed task. Stop emitting internal “done” cues while review or authorization is pending.

### 6. Recommendations

Replace the mandatory visible stack of use cases, rationale, evidence, gaps, trade-offs, and deployment sections with a decision summary. Preserve those useful analyses in full detail. Combine overlapping behavior and rationale into a single concrete explanation. For a revision, state the failing scenario and consequence, not a miniature code review.

### 7. Send-back and revision cycle

Build “Send back” currently opens a second confirmation; design send-back can deliver directly. Make the distinction predictable. Where confirmation remains necessary, use “Review feedback…” then “Send feedback to builder.” Show all instructions being authorized, once. After delivery show “Feedback delivered · waiting for revision”; do not keep the old approval actionable. When a new revision arrives, name what changed and that re-review is underway.

### 8. Permissions

Show the exact command, purpose, and scope. “Allow once” does not mean the command completed. Separate permission recorded, delivered, and execution evidence. On expiry say permission was denied and whether anything needs to be retried. Do not label a stopped session “answered” without evidence.

### 9. Recorded, delivered, and applied outcomes

“Approval recorded,” “Approval delivered,” and “Builder resumed” are distinct facts. Likewise feedback sent, revisions completed, merge queued, merge verified, and deployment verified. Use the strongest supported claim and make uncertainty visible. The current design recommendation can report “Approval sent” after recording the original question answer, before bridge delivery; correct this presentation gap.

### 10. Failures, stale controls, and recovery

Explain what did not happen, whether work or authorization is safe, and one next action. Replace internal errors with a short human summary plus inspectable diagnostics. Stale controls should say what replaced them or that the decision already ended, rather than inviting repeated taps. Never automatically replay an uncertain consequential action as a UX fix.

### 11. Work finished versus cleanup

Keep work completion separate from workspace cleanup. One cleanup exception should explain why the workspace was kept and exactly what each proposed action affects. “Archive warning” and “archive run ledger” have different consequences today; never use generic “archive” as the human action. Avoid duplicate raw warning plus coordinator paraphrase. Surface cleanup completion where a human explicitly requested it; keep routine successful housekeeping quiet.

### 12. Thread completion, reopening, and retirement

Use a concise final state and result link. Reopening a conversation must not look like restarting work or granting approval. If work actually resumes, show that transition. Telegram topic closure and Slack parent-state updates differ; do not promise Slack thread closure/deletion. No repeated boilerplate about retained history and separate cleanup unless it explains a current exception.

### 13. Host-generated communication — deferred

Tool traces, skill announcements, recall counts, and unrelated onboarding notices contribute to reading burden, but are outside this feature's initial scope. Jason explicitly prefers improving Muster-owned messages first rather than spending time adapting Hermes Slack/Telegram notices.

Do not monkey-patch Hermes adapters, private notification paths, or host activity notices for this work. An obvious existing supported setting may be noted as an optional separate follow-up; investigating or changing host behavior is not a delivery requirement or a blocker for the Muster improvements.

## Proposed message examples

These are design examples, not implemented controls or claims of current state.

### Plan approval

**Activity status · Plan v2**
**Recommend: approve**

Shows whether the builder started and when it last reported activity. Updates one message instead of adding a stream of notices.

Limit: a quiet tool can still be running.

Approving lets the builder implement this plan. It does not merge or deploy anything.

Controls: **Approve plan** · **Request changes** · **Leave pending**
Secondary: **Full plan and review**

### Send back a build

**Activity status · PR #47**
**Recommend: request two fixes**

- A failed final update can leave a finished task showing “working.”
- Missing start evidence can hide a PR that is ready for review.

Tests pass, but these two behaviors still need correction.

Controls: **Review feedback…** · **Merge PR** · **Leave PR open**
Secondary: **Full review**

Before merging, show the real deployment consequence or that it is unknown. Before sending feedback, show the complete instructions being authorized.

### After feedback delivery

**Feedback delivered · revision pending**

The builder received both fixes. No action needed; Ella will review the revision when it arrives.

Only use “received” when the existing receipt evidence supports it. Otherwise: “Feedback sent; receipt not confirmed.”

### Cleanup exception

**Work finished · workspace kept**

The PR is merged. One untracked file prevents safe cleanup: `uv.lock`.

Recommended next step: discard the generated file and remove this workspace. Keep the branch.

The actual cleanup action still requires authorization; this example does not introduce working cleanup buttons.

## Platform rendering

- Use a small explicit presentation contract and separate native renderers, not a broad new UI framework or a regex-only universal Markdown converter.
- Slack currently passes escaped arbitrary text to `mrkdwn`; escaping protects against injected mentions but does not translate Markdown headings or `**bold**`. Render authored structure natively with Slack blocks or correct Slack syntax.
- Preserve escaping for agent/user strings. Native links should come from validated structured URLs; do not undo escaping globally to make links work.
- Keep exact commands in the existing literal/preformatted treatment. Never modify command or authorized feedback text to improve typography.
- Provide meaningful top-level fallback text for notifications and accessibility, not the entire long review or only “permission.”
- Use readable paragraph spacing and restrained emphasis. Do not rely on emoji or color alone to distinguish state.
- Keep Telegram native behavior; platform differences should not change authorization semantics.
- Reuse existing versioned proposals, review records, and ledger detail as the source for optional detail. Verify a human-openable link/action before claiming full detail is accessible; a raw local path or card ID is not sufficient.

## Suggested implementation order

1. **Message ownership and lifecycle:** map each current request and message producer; define the authoritative decision presentation and handling of review-before-question, question-before-review, already-answered, and multi-question cases.
2. **Concise review generation and native formatting:** update coordinator guidance and channel templates together; retain full detail and consequential content. Replace truncation-as-summary with deliberately authored summaries.
3. **Status gaps and receipts:** make review pending, feedback delivery, revision pending, and action uncertainty explicit using existing evidence. Integrate the existing activity-status feature.
4. **Muster-owned noise and cleanup:** reconcile notification versus coordinator ownership within Muster. Keep permissions and cleanup consequences explicit. Hermes-generated notices are deferred; no host monkey-patches or host-notice dependency.

Do not add a dashboard, reminder engine, general transport framework, or new recovery subsystem for this feature. Start with the smallest complete workflow and the existing request/channel primitives.

## Acceptance walkthrough

Use a fixture gallery before coding, then renderer tests and a separately authorized bounded live check on Slack and Telegram.

- Put five active tasks side by side: from each message's first lines, identify task, state, whether it needs the human, and the next action.
- Review arrives before/after the builder's question: exactly one authoritative approval surface. No double approval or tap-again loop as the ordinary path.
- Answer before the review finishes: preserve the answer and do not ask again.
- Two real questions: answering one does not erase the other or conflate their controls.
- Send back: complete authorized instructions visible; confirmation predictable; delivered versus unconfirmed receipt explicit; revision re-review visible.
- Permission: exact command unchanged; one-time scope explicit; no claimed execution without evidence.
- Long review with a material deployment caveat: caveat stays visible, full review remains accessible, no silent cut at Slack's section limit.
- Native Slack rendering: no literal Markdown headings, double-star artifacts, broken links, or unexpected pings; readable mobile layout and fallback text. Telegram also retains usable formatting and controls.
- Quiet tools, session end, merge queued, merge confirmed, and cleanup blocked each communicate the correct distinct state.
- Restart/stale callbacks preserve human decisions; cosmetic message consolidation grants no extra authority.
- No duplicate Muster-owned notice obscures the pending human decision. Existing Hermes tool/activity notices are outside this feature's acceptance criteria and do not block delivery.

## Primary source pointers

Paths below are relative to the installed plugin inspected, except the progress reference.

- `muster/channels/shared.py:52–110`: title, shared render, truncation, and Slack escaping.
- `muster/channels/slack.py:99–153,199–209,239–241,315–368,424–425,511–529`: blocks, outcomes, reply semantics, acknowledgements, parent and finish text.
- `muster/channels/telegram.py:251–268,302–349`: native outcomes/questions and permission behavior.
- `muster/decisions.py:316–361,453–526,529–566`: design recommendation, approval handoff, build feedback confirmation, verified merge outcomes.
- `muster/cleanup.py:583–607,611–653`: delayed exceptions and removal outcome.
- `skills/escalation/SKILL.md:18–30,54–57,74–122`: visible review structure, silence rules, and recommendation workflow.
- PR47 worktree `muster/progress.py:119–160`: proposed evidence-based activity states; not installed at audit time.
