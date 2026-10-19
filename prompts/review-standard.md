---
name: review-standard
description: How to judge, verify and write up a build or design review for the human: smallest complete fix, evidence kept apart from claims, a summary written for the decision. Load it with `muster:escalation`.
---

# Review standard

The standard for review judgment, verification and human-facing writing, for designs, builds and re-reviews. `muster:escalation` owns the commands, card fields and approval mechanics; this file owns the judgment and the writing.

## Judgment

- Judge the requested behavior against original requirements, affected callers and real failure/recovery paths. A proposal does not define its own acceptance criteria.
- Recommend the smallest complete fix. Block on demonstrated incorrect behavior, essential authorization/security/data-loss risks, or unmet acceptance criteria. Explain the failing scenario and consequence.
- Keep optional hardening, style and speculative resilience non-blocking. A visible, recoverable limitation is not automatically a release blocker.
- Separate code quality from merge readiness: a documentation-only conflict can block merging without invalidating a good fix. Resolve it by inspecting the resolved text; do not rerun code tests for it or call it a failed code review.

## Verification proportional to change

- Pin the artifact version: PR base/head or proposal version/hash. Verify checkout identity and cleanliness before treating local results as evidence. Do not switch, stash, reset or clean someone else's worktree.
- Inspect requirements, diff and relevant callers. Run tests that establish changed behavior; broaden where interactions or acceptance criteria warrant it. Match lint to project configuration.
- For non-trivial code, obtain independent review when available, then verify findings yourself. A reviewer suggestion or keyword match is not proof of a defect.
- Keep independently executed evidence separate from author claims, baseline failures, interrupted runs and unperformed live checks. No reported CI is neither failing CI nor passing CI.
- Keep merge, installation and live activation separate. GitHub proves merge state, not what is installed or executing locally. Use installed revision/runtime evidence for deployment claims. Missing production notes mean uncertainty, not proof of no deployment.

## Human decision summary

Write for the decision, not for an audit log. Lead with a recognizable task, recommendation and one concrete reason. State the actual blocker or material caveat and what the human's next action authorizes.

- Use plain language: translate Git state labels and implementation details into consequences.
- For revisions, show every instruction being authorized. Do not make the human reconstruct feedback from analysis.
- Keep hashes, long test totals, routine provenance and non-decision-changing caveats in supporting detail. Include a risk or uncertainty in the summary when it changes the decision; never conceal it to fit a length budget.
- Do not require use-case/rationale lists, severity sections, praise sections or deployment boilerplate on every card. Omit empty or repetitive sections. The card's "Deploy on merge" line is the one place for the deploy consequence; do not repeat it in prose.
- Author the short summary deliberately; never truncate the full review to make one.

Every text the human reads (the summary, the feedback or revisions, the full review) follows one standard:

- Lead with what changes, why it matters and what the tap authorizes. The full review opens with your plain
  recommendation ("Send it back: …", "Approve: …"), not with a version label.
- Name which part of the software does which job ("intake reads issues and starts runs"), never an abstraction
  such as "the ownership split meets the dispatch brief".
- No commit hashes, ledger or card ids, internal task states or references the human cannot open. The version pin
  goes only in the command's `--head` or `--proposal`.
- Don't restate the brief, the plan, the card or each other: the card already shows the subject, your
  recommendation and the quoted feedback.
- When the proposal has a preview or examples section (messages, screens, output), the summary's first line points
  to it, e.g. "👀 Preview the proposed messages: section 1 of the plan, just above this card".

Example, documentation conflict:

> The fix passed review, but two documentation additions conflict. Keep both and verify no code changed; no test rerun is needed for that resolution. Recommended: send those instructions to the builder, then check the resolved revision before merging.

Example, actual code blocker:

> Send back: reopening a request can replay an old approval onto a new question. Bind recovery to the original request and add a regression test. This prevents an earlier approval from overriding a later human decision.

## Supporting review

Keep enough detail to audit the judgment: exact version, findings with evidence, independent checks, gaps, accepted trade-offs and relevant merge/deployment effects. Use only sections that help. The full review must not contradict its summary or feedback. A recommendation to merge is not a claim of having merged.

## Final check

Can the human identify what is ready, what is blocked, why, and what their action will do without decoding hashes or reading the pane? Are the summary, full review, feedback, verification scope and authorization path consistent?
