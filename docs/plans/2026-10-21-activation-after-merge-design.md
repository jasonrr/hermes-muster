# Design: activation after merge (approved Proposal v2 22e08bef3ead, 2026-10-21)

**Approved** by the human on 2026-10-21 as Proposal v2, with the recommended choice: the coordinator runs activation steps only on an explicit chat request.

**What changed since v1** (your revisions):
1. `cleanup.resolve` now runs the same idempotent handoff operation for PRs merged outside the Merge button. It keeps the matching reviewed handoff. When nothing matches, it says "activation unknown", never "nothing to activate".
2. All wording reflects the evidence: "activation not verified", never "not active yet". The status line is explicitly historical, and the Activate card is the current record. There are tests for an automatic deploy and for verified completion.

## Problem

Muster treats a merge as the end of a run. After the Merge tap, the decision message says "✅ Merged … Muster does not deploy." When cleanup sees the merge, the run's status line says "✅ Finished · PR #n merged". Nothing says what you still have to do before the change is live, so you have to ask every time. Some of those steps are outside the PR, such as adding a setting to `~/.hermes/config.yaml`, copying a cron script or restarting the gateway.

## What exists, and where the gap is

I read the prompts, `decisions.py`, `progress.py`, `cleanup.py`, `adhoc.py`, `intake.py`, docs/setup.md and docs/reference.md, the production notes in your `notes_dir`, the local install at `~/.hermes/plugins/muster`, and the `hermes plugins` and `hermes gateway` CLIs. For a contrasting example I read jasonrr/jrr.dev's README.

| Need | What exists | Gap |
|---|---|---|
| What a merge triggers, per repo | **Production notes** (`<notes_dir>/<owner>__<repo>.md`). The four RC notes say where production runs and how to read it. | No note for hermes-muster. The notes say nothing about installing or activating a change. **Ad-hoc briefs never include the note.** Only issue briefs do (`intake.brief` `$production`; `adhoc.footer` has none). This run's brief had none. |
| Change-specific steps | The PR body is the builder's evidence (workflow step 11). | Nothing asks for the steps after a merge. Changes outside the PR have no place. |
| Checking those steps | The coordinator's review must include a "Deploy on merge: …" line (`decisions.summary_of` checks for "deploy"). | It is one line. There are no steps, no owner, no verification and no rollback, and no check against the diff (for example, a new setting with no config step). |
| Showing it after the merge | `decisions.merged()` text and `progress.state` "finished" | Both present merged as done. |
| Tracking it | The Hermes kanban board. Cleanup-warning cards are already blocked `needs_input` cards that tell you what is yours to do (`cleanup.py:622`). | Nothing records that a merged change is not active yet. |

**Build or reuse:** every part reuses something that exists. Repository-level knowledge stays in the production notes and the repo's own docs. The change-specific steps go in a section of the PR body. Checking them is part of the coordinator's existing review. They reach you through the existing merge message, status line and card pattern. Hermes and herdr have no deploy feature, and the brief rules out a deployment engine. The only new code is a small step at the merge that creates an activation card and changes two status texts. A generic deploy tool such as a GitHub Environments workflow or Ansible would not cover config outside the repo or a gateway restart, and it would be a framework to maintain.

## Approach

**1. Prepare: the builder writes it and runs nothing.** Both briefs get a "Production and activation" section: the repo's note (or the "no note" line) followed by a new prompt, `prompts/activation-handoff.md`. That prompt requires a `## Activation` section in the PR body:
- **Merge triggers:** what merging does in this repo, citing the note or the repo doc, or "unknown: no note says".
- **Steps:** in order, the exact commands and every change outside the PR. A config change is shown as the exact YAML to add. Secrets are named, never given values. Include migrations and restarts.
- **Who:** for each step, who runs it and what approval it still needs.
- **Verify live:** a command, its expected output and what it proves. Each step is marked *tested* or *untested*.
- **Rollback.**
- Or **"Nothing to activate: <why>"** (for example, a docs-only or tests-only change).

The builder never runs a step. This is in the non-overridable briefs, not in `builder-workflow.md`, so it still applies when `workflow_prompt_file` replaces the workflow.

**2. Check: the coordinator verifies it before recommending.** `escalation.md` and `review-standard.md` tell the coordinator to compare the section with the diff, the note and the repo docs. The section is incomplete if a new setting, cron script, hook, migration or dependency has no step, or if a gateway-loaded plugin change has no restart. A missing or wrong section means send-back. `hermes muster recommend` gets a required `--activation <file>`: the checked handoff, or `none: <why>`. It is capped at 1500 characters and refused, never cut, when longer, as feedback already is. It is stored on the build request. The summary's "Deploy on merge" line becomes two lines, and the check in code becomes "the summary says what a merge triggers and what is left to activate":
- "Merge triggers: …"
- "After merge: …"

**3. Surface: one handoff operation, for every route to a merge.** One function, `decisions.handoff(ledger, pr_url, head)`, can safely run more than once. Every merge Muster learns of calls it:
- the Merge tap (`_merge`);
- a merge found after a restart (`_recover`);
- **`cleanup.resolve`**, which already finds PRs merged outside the Merge button (on GitHub or by hand). It calls `handoff` *before* it marks open requests stale and closes the run.

`handoff` picks the handoff text:
- **A reviewed handoff that matches:** the build request (any status) whose `pr` and `head` equal the merged PR's URL and `head.sha`. Its stored `--activation` is used as is.
- **No match:** for example, a GitHub merge of an unreviewed head, or a request from before this change. Muster shows **"Activation unknown"**, never "nothing to activate". The text points to the PR's own `## Activation` section, marked as the builder's unreviewed claim, and asks Ella to check it.
- **A matching handoff of `none: <why>`:** no card. This is the only route to "nothing to activate".

For every other result, `handoff` creates one **Activate card** using the cleanup-warning pattern:
- the idempotency key `muster-activate:<ledger>`, so a replay, a restart or a repeated cleanup pass never makes a second card;
- a subscription in the run's thread;
- then blocked `needs_input`, with the handoff as the block text, so the ping *is* the handoff.

It writes an `activation` activity file (the card id and the result) for the status line.

**Wording says what is known, not more.** A merge proves only the merge. The text never says "not active yet", because a merge-triggered deploy may already be live. It says **"activation not verified"** until someone verifies it, or "activation unknown".
- The merge outcome: "✅ Merged into main, as reviewed. Activation not verified: its steps and check are on the Activate card."
- With `none`, the outcome: "… Nothing to activate: <why>."

**4. Track: the card is the current record, and the status line is history.** The final status line is never read again (`gateway` `S.final`). So it does not claim the current state. It states the merge and points to the card:
- "✅ PR #66 merged 14:02 · activation not verified at merge; current state on its Activate card"
- "… activation unknown; current state on its Activate card"
- "✅ Finished · PR #66 merged · nothing to activate"

Nothing polls. The card's own completion ping gives the current state.
- `hermes kanban list --board muster` shows everything merged but not verified.
- When you say it's done, or ask the coordinator to do it, the coordinator runs the read-only **Verify** step. It comments the command and output on the card, then completes the card with one of two summaries:
  - "✅ Active · verified: …"
  - "Reported done by you · not verified: <why>"
- Your word and verified evidence are never merged into one claim. This is the only card the coordinator may complete, and only on your word.

**Authority.** The Merge tap authorizes only the merge. Activation runs when you run it, or when you explicitly ask the coordinator in chat ("install the muster update"). That request authorizes only the steps on that card. The coordinator shows any config change as a diff before writing it, and never activates on a wake. This follows the same rule as "Message a running agent" in `dispatch.md`.

## Messages (Slack, realistic data)

Build card summary (the last two lines are new):
```
🟡 PR #66 · Activation after merge
Ella recommends merging it.
Tests pass (412) and the handoff matches the diff: one new prompt, no new setting.
Merge triggers: nothing. Muster runs from the clone in ~/.hermes/plugins/muster.
After merge: you update the plugin and restart the gateway (3 steps, on the Activate card).
```
After the Merge tap (the decision message is edited):
```
✅ Merged into main, as reviewed. Activation not verified: its steps and check are on the Activate card.
```
The Activate card ping, in the run's thread:
```
Activate · Activation after merge (PR #66)
Merged; activation not verified. Merge triggers nothing here. Yours to run, or ask Ella to:
1. Acceptance gate (docs/setup.md): HERMES_AGENT_DIR=~/.hermes/hermes-agent ~/.cache/muster-acceptance/venv/bin/python tests/acceptance/gate.py · install only on PASS
2. hermes plugins update muster
3. hermes gateway restart · registers the new handlers
Verify: git -C ~/.hermes/plugins/muster log -1 --format=%h shows the merge commit, and hermes muster tick --dry-run exits 0 (untested)
Rollback: git -C ~/.hermes/plugins/muster checkout <previous sha>, then hermes gateway restart
```
Status line, final and historical: `✅ PR #66 merged 14:02 · activation not verified at merge; current state on its Activate card`. After verification, the card's completion pings `✅ Active · verified: plugin at 3f2a9c1, tick --dry-run exit 0`. The status line does not contradict it.

A merge-triggered deploy (illustrative, radicalcandorai's deploy workflow per its note) produces a card that leads with "Merge triggers a deploy (bot-service-deploy.yml). Activation not verified." Its steps are only to verify: `gh run list --workflow bot-service-deploy.yml`, then `gcloud run revisions list` shows the new revision serving.

A PR merged on GitHub, outside the Merge button, with no matching review, produces a card that reads "Activation unknown: no reviewed handoff matches the merged commit. The builder's unreviewed Activation section: <PR link>. Ask Ella to check it."

## Examples

- **hermes-muster.** Read from docs/setup.md and the local clone, not validated live. A merge triggers nothing. The plugin is a git clone at `~/.hermes/plugins/muster`. Activating takes the gate, `hermes plugins update muster`, then `hermes gateway restart`. Copy `scripts/*.sh` only when they changed. Add a settings change to `config.yaml` only when the PR adds one. That repo has no production note, so I will draft one at `docs/plans/…-muster-production-note.md` for you to copy into `notes_dir`. Writing into `notes_dir` is your call.
- **jasonrr/jrr.dev (illustrative).** Read from its README. It is not configured in Muster, and none of this was run. "No branch or CI job automatically deploys production." The steps are preflight, a target build, D1 migration status and check, then `wrangler deploy`, staging before production. Rollback cannot undo a database migration. The same section format carries a very different procedure. That is why the steps come from the repo and the PR, not from Muster.

## Changes

- **Prompts:**
  - new `prompts/activation-handoff.md`
  - `issue-dispatch.md` and `ad-hoc-run.md` get the section
  - `escalation.md` and `review-standard.md` get the check, the `--activation` flag, the activation-card rules and the [SILENT] rule on its wake
  - `prompts/README.md` inventory
- **Code:**
  - `intake.production_note`, reused by `adhoc.footer`
  - `cli.py` and `decisions.recommend`: `--activation`, stored on the request, and the summary check
  - `decisions.handoff` (new), called from `_merge`, from `_recover` and from `cleanup.resolve` before it marks requests stale
  - `merged()`: the outcome text
  - `progress.state`: the historical final text, from the `activation` activity file
- **Docs:** README, reference.md and setup.md ("Production notes" gains an Activation part).
- **Responsibilities:**
  - The builder prepares the handoff.
  - The coordinator checks it and, only on your request, runs or verifies it.
  - Muster carries and tracks it.
  - You authorize each consequential step.

## Acceptance tests (mocked, as today)

1. An ad-hoc brief contains the note and the activation rule. An issue brief still does.
2. `recommend` without `--activation`, with one over the cap, or with a summary missing the "Merge triggers"/"After merge" lines is refused with a reason.
3. A Merge tap with steps produces the "Activation not verified" outcome and one Activate card (blocked `needs_input`, subscribed in the run's thread, body = the handoff). A replay or `_recover` makes no second card.
4. A merge with a matching `none` makes no card, and the outcome says "Nothing to activate: …".
5. **A GitHub/manual merge:** `cleanup.resolve` on a merged PR with no Merge tap.
   - When a build request matches its URL and head, the card carries that reviewed handoff, read before the request is marked stale.
   - With no match (a different head, or no request), the card says "Activation unknown" and never "nothing to activate".
   - Running `resolve` twice, or after a Merge tap, leaves exactly one card.
6. **Automatic deployment:** a handoff whose merge triggers a deploy gives a card and texts that say "activation not verified". No text says "not active".
7. **Verified completion:** after the Activate card is completed "✅ Active · verified …", the run's status line contains no "not active" claim. It is the historical "merged … current state on its Activate card" line.
8. `progress.state` after resolution never reads "Finished" alone for a run that has a card. "nothing to activate" appears only with a matching `none`.
9. A request recorded before this change (no `activation` field) still merges, takes the "Activation unknown" path and gets a card.

## Cut

- **A deploy engine, per-repo command runners, Muster running activation itself, and button taps that run steps.** These are the framework the brief rules out. Your chat request to the coordinator already covers execution.
- **Automatic activation detection**, such as polling installed revisions. It is repo-specific. The Verify step on demand covers it.
- **An "activated" state in Muster's own files, and refreshing the status line from the card.** Either would need the final status reopened and the card polled. The status line is history, and the card is the current record.
- **Writing the muster production note into `notes_dir`.** That is live config, so it is drafted for you instead.
