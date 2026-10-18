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
- Separate code quality from merge readiness: a documentation conflict can block merging without invalidating a good fix.

## Verification proportional to change

- Pin the artifact version: PR base/head or proposal version/hash. Verify checkout identity and cleanliness before treating local results as evidence. Do not switch, stash, reset or clean someone else's worktree.
- Inspect requirements, diff and relevant callers. Run tests that establish changed behavior; broaden where interactions or acceptance criteria warrant it. Match lint to project configuration.
- For a documentation-only revision/conflict resolution, inspect the resolved text, preserve both intended additions and verify executable code is unchanged. Do not rerun code tests solely because documentation changed. If reconciliation also introduces untested executable changes, verify those changes proportionately.
- For non-trivial code, obtain independent review when available, then verify findings yourself. A reviewer suggestion or keyword match is not proof of a defect.
- Keep independently executed evidence separate from author claims, baseline failures, interrupted runs and unperformed live checks. No reported CI is neither failing CI nor passing CI.
- Keep merge, installation and live activation separate. GitHub proves merge state, not what is installed or executing locally. Use installed revision/runtime evidence for deployment claims. Missing production notes mean uncertainty, not proof of no deployment.

## Human decision summary

Write for the decision, not for an audit log. Lead with a recognizable task, recommendation and one concrete reason. State the actual blocker or material caveat and what the human's next action authorizes.

- Use plain language: translate Git state labels and implementation details into consequences.
- For revisions, show every instruction being authorized. Do not make the human reconstruct feedback from analysis or imply a docs conflict is a failed code review.
- Keep hashes, long test totals, routine provenance and non-decision-changing caveats in supporting detail. Include a risk or uncertainty in the summary when it changes the decision; never conceal it to fit a length budget.
- Do not require use-case/rationale lists, severity sections, praise sections or deployment boilerplate on every card. Omit empty or repetitive sections. Honor any concise deployment line required by the actual approval interface without repeating it in prose.
- Author a short summary deliberately; do not truncate the full review. A new renderer cannot simplify verbose text supplied to it. Use the interface's supported summary field.

Example, documentation conflict:

> The fix passed review, but two documentation additions conflict. Keep both and verify no code changed; no test rerun is needed for that resolution. Recommended: send those instructions to the builder, then check the resolved revision before merging.

Example, actual code blocker:

> Send back: reopening a request can replay an old approval onto a new question. Bind recovery to the original request and add a regression test. This prevents an earlier approval from overriding a later human decision.

## Supporting review

Keep enough detail to audit the judgment: exact version, findings with evidence, independent checks, gaps, accepted trade-offs and relevant merge/deployment effects. Use only sections that help. The full review must not contradict its summary or feedback. A recommendation to merge is not a claim of having merged.

## Authorization and delivery

- Reviewing authorizes investigation and preparation, not publishing reviews/comments, approving, merging, deploying, committing, dispatching builders or changing board state. Use the task's authorized action path; Muster decisions require the human's version-bound tap.
- The recommend command's printed id means the recommendation was recorded and the human gets it as a message. If the command refused, say why; never edit request files to repair it.
- On automated wakes, avoid duplicating a delivered actionable notice. On a direct human message, answer briefly even if a decision card was also posted; do not return a silence marker. Answer questions about pending decisions without burying them in unsolicited status updates.

## Final check

Can the human identify what is ready, what is blocked, why, and what their action will do without decoding hashes or reading the pane? Are the summary, full review, feedback, verification scope and authorization path consistent?
