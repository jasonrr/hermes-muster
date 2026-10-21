# Plan: activation after merge

The design is [2026-10-21-activation-after-merge-design.md](2026-10-21-activation-after-merge-design.md), approved by the human as Proposal v2. This plan carries it out. Read docs/learnings.md before starting. Two entries matter here:
- put `--` before free text in `kanban block`, unless the text has a fixed prefix;
- run the tests in a `.venv`.

**Setup:** `uv venv .venv && uv pip install --python .venv/bin/python pytest pyyaml ruff`

**Tests:** `.venv/bin/python -m pytest -q`

**Lint:** `.venv/bin/ruff check muster tests` takes no config and already reports 136 findings on main. A task must not raise the finding count of any file it changes. Compare `.venv/bin/ruff check -q <file> | wc -l` with `git show main:<file> | .venv/bin/ruff check -q --stdin-filename <file> - | wc -l`.

**Baseline on main:**
- 2 tests fail before this work, both caused by commit 1296583, which reworded the attribution rule without regenerating:
  - `test_core.py::test_brief_keeps_forbidden_list` (needle "attribution trailer");
  - `test_prompts.py::test_prompts_match_pre_refactor_output` (golden files).
- Task 1 fixes both, because it regenerates the same goldens anyway.

## Task 1: the builder writes the handoff, in both briefs

- **Files:**
  - `prompts/activation-handoff.md` (new)
  - `prompts/issue-dispatch.md`
  - `prompts/ad-hoc-run.md`
  - `prompts/README.md`
  - `muster/intake.py` (`production_note`)
  - `muster/adhoc.py` (`footer`)
  - `tests/test_core.py`
  - `tests/golden/*.txt`
- **Nearest pattern:** `intake.production_note` and the `$production` placeholder in `issue-dispatch.md`, and `adhoc.footer` filling `ad-hoc-run.md`.
- **Test first.** Add tests to `tests/test_core.py`, next to `test_the_brief_carries_the_production_note_or_says_there_is_none`:
  - `adhoc.footer({...})` contains the repository's note text when one exists. It contains "No production note for this repository" when none exists. In both cases it contains "## Activation" and "Never run a step yourself".
  - `intake.brief(...)` contains "## Production and activation" and "## Activation".
  - Fix the stale needle in `test_brief_keeps_forbidden_list`: "attribution trailer" becomes "AI co-author trailers".
- **Change:**
  - **`prompts/activation-handoff.md`:** the builder's rule.
    - The PR body ends with a `## Activation` section containing Merge triggers, Steps, Who, Verify live (each step and check marked tested or untested) and Rollback.
    - Changes outside the PR (config outside the repo, scripts to copy, migrations, restarts) are steps. Config is given as exact text. Secrets are named, never given values.
    - Or "Nothing to activate: <why>" only when there is nothing to do or check.
    - "Never run a step yourself."
    - Wording as in the design, section 1.
  - **`intake.production_note(repo)`** returns `<note or no-note line>` + blank line + `config.prompt("activation-handoff")`.
  - **`issue-dispatch.md`:** `## Production` becomes `## Production and activation`.
  - **`ad-hoc-run.md`:** add `## Production and activation\n\n$production` *before* `## How to work`. `test_the_workflow_override_replaces_the_bundled_workflow_in_the_brief` requires the footer to end with How to work.
  - **`adhoc.footer`:** pass `production=intake.production_note(run["repo"])`. Import `intake` in adhoc. intake imports only config, core and launcher, so there is no cycle.
  - **`prompts/README.md`:**
    - add an inventory row for `activation-handoff.md`;
    - `$production` in the `ad-hoc-run.md` row;
    - the `issue-dispatch.md` row's `$production` text.
  - Regenerate: `MUSTER_REGOLD=1 .venv/bin/python -m pytest -q tests/test_prompts.py`. Then read `git diff tests/golden`. The only changes may be:
    - the attribution wording from 1296583;
    - the new section heading and handoff text;
    - the ad-hoc footer's new section.
- **Verify:** `.venv/bin/python -m pytest -q tests/test_core.py tests/test_prompts.py tests/test_launch.py`
- **Exists because:** ad-hoc briefs never carry the production note (`adhoc.footer` has no `$production`), and no brief asks for the steps after a merge (workflow step 11 lists only review evidence).

## Task 2: the coordinator records a checked handoff

- **Files:**
  - `muster/cli.py`
  - `muster/decisions.py` (`recommend`, `summary_of`, `fresh`)
  - `tests/test_decisions.py`
  - `tests/test_cards.py`
  - `tests/golden/cards/*`
- **Nearest pattern:** `--feedback` in `cli.setup` and `decisions.recommend`. Its file is read by `read_text`, capped by `FEEDBACK_MAX` and refused with a reason, never cut.
- **Test first** (`tests/test_decisions.py`):
  - `rec_args` gains `activation="none: tests only."`, written to `activation.md`, as `args.activation`.
  - New tests:
    - The request stores `activation` verbatim.
    - An activation over 1500 characters is refused, with "activation" and "1500" on stderr, and no request.
    - The parametrized summary-refusal test refuses "Right and tested." with the reason `merge triggers`. The accepted summary is "Right and tested.\nMerge triggers: nothing.\nAfter merge: nothing to activate.".
    - `fresh(req)` keeps `activation`.
  - Update `test_a_summary_is_the_card_and_the_review_stays_whole` and `test_cards.py`'s `SEND_BACK` and `MERGE` to the two new lines.
- **Change:**
  - `cli.py`: `p.add_argument("--activation", required=True, help="file with the checked activation handoff, or 'none: <why>'")` on `recommend`.
  - `decisions.recommend`: `activation = read_text(args.activation, "activation")`. If it is longer than `FEEDBACK_MAX`, raise `ValueError(f"the activation is {n} characters, over {FEEDBACK_MAX}: shorten it; the PR's own section can hold the rest")`. Pass `activation=activation` to `create`.
  - `summary_of`, kind `build`: require both "merge triggers" and "after merge" in `text.lower()`. The error is `'the summary must say what a merge triggers and what is left after it ("Merge triggers: …" and "After merge: …")'`. Update the docstring.
  - `fresh`: add `"activation"` to the copied keys.
  - Regenerate the card goldens the way `test_cards.py` does (`MUSTER_REGOLD=1`). Read the diff: only the summary lines may change.
- **Verify:** `.venv/bin/python -m pytest -q tests/test_decisions.py tests/test_cards.py tests/test_cli.py`
- **Exists because:** the only merge consequence recorded today is the "deploy" substring check (`decisions.summary_of`). Nothing carries the checked steps through to the merge.

## Task 3: one idempotent handoff at every merge Muster makes

- **Files:**
  - `muster/decisions.py` (new `handoff` and `pr_number`; `merged`, `_merge`, `_recover`)
  - `tests/test_decisions.py`
  - `tests/test_cards.py` goldens
- **Nearest pattern:** the cleanup-warning card (`cleanup.py`, around line 622):
  - create with an `--idempotency-key`;
  - if the status is `ready`, `core.subscribe(card, ledger)`, then `kanban block --kind needs_input`, then `events.expect(card, "blocked", …)`.

  `decisions.rereview` already creates cards from `decisions`.
- **Test first** (`tests/test_decisions.py`):
  - **Fakes:**
    - `World.run` gains a kanban fake shaped like `tests/test_cleanup.py` `World.run`: `create` keyed by `--idempotency-key`, plus `show` and `block`. It records cards in `self.cards`.
    - The `world` fixture monkeypatches `core.subscribe` to record `(card, ledger)`.
    - `test_merge_revalidates…` filters `world.calls` to `gh` calls before it compares `["view","merge","view"]`.
  - **New tests:**
    - **Merge tap with steps** (`build(..., activation="Merge triggers: nothing.\nSteps: …")`): the outcome is `"✅ Merged\nMerged into main, as reviewed. Activation not verified: its steps and check are on the Activate card."`. There is exactly one card: blocked, block text starting "Merged; activation not verified." and containing the handoff, idempotency key `muster-activate:t_1`, subscribed with ledger `t_1`. The `activation` activity file starts with `steps `.
    - **A matching `none: docs only.`:** no card, the outcome ends "Nothing to activate: docs only.", and the activity file reads `none`.
    - **A request without `activation`** (from before this change): the outcome says "Activation unknown"; the card says "Activation unknown" and links `https://github.com/acme/app/pull/9`.
    - **Replay:** calling `handoff` twice, then `_recover` on a landed merge, leaves one card and blocks it once.
    - **Automatic deployment:** an activation of "Merge triggers a deploy (deploy.yml).\nVerify live: gh run list …". Neither the outcome nor the card text contains "not active". Both contain "activation not verified" (case-insensitive).
    - **A kanban failure during the handoff:** the merge still ends `done` with the steps outcome, and the failure is logged.
  - **Updated tests:** `test_recover_a_merge_that_landed` compares with `decisions.merged(req, "unknown")`, since its request has no `activation`.
- **Change:**
  - `pr_number(url)`: `int(m[1])` for `re.search(r"/pull/(\d+)", url or "")`, else None.
  - **`activation_for(ledger, number, head)`** returns `(kind, text)`:
    - **Match:** the newest build request of the ledger (any status) with `pr_number(r["pr"]) == number`, `r["head"] == head` and an `activation`.
    - **`none`:** a match whose text matches `^none\b` (case-insensitive) gives `("none", <the text after "none", stripped of ":" and spaces>)`.
    - **`steps`:** any other match gives `("steps", "Merged; activation not verified.\n" + text)`.
    - **`unknown`:** no match gives `("unknown", f"Activation unknown: no reviewed handoff matches the merged commit. The builder's unreviewed Activation section is in {url}. Ask {who()} to check it.")`, where `url = f"https://github.com/{repo}/pull/{number}"`. The function takes `repo` too.
  - **`handoff(ledger, repo, number, head)`** returns the kind and runs `activation_for`.
    - For `none`: `progress.touch(ledger, "activation", "none")`.
    - Otherwise, the title is `f"Activate · {short(run)} ({progress.pr_name(url)})"`, where `run` is the newest build request's `run`, else `{"title": "", "repo": repo}`.
    - Then `core.kanban("create", "--body", f"Activation handoff. ledger {ledger} | {url} | head {head}\n{text}\n{core.PROVENANCE}", "--idempotency-key", f"muster-activate:{ledger}", "--created-by", core.CREATED_BY, "--json", "--", title)`.
    - If the status is `ready`: `core.subscribe(id, ledger)`, then `core.kanban("block", "--kind", "needs_input", id, text)`. The text always starts with "Merged;" or "Activation unknown", so it cannot be read as an option. Then `events.expect(id, "blocked", "activation handoff")`.
    - Finally `progress.touch(ledger, "activation", f"{kind} {id}")`.
  - **`activate(req, repo)`:** `kind = activation_for(...)[0]`, then `handoff(...)` inside `try/except (OSError, ValueError, KeyError, core.CommandError)` that logs to `decisions` and returns `kind`. `cleanup.resolve` runs the handoff again before it closes the run.
  - **`merged(req, kind)`:** `"✅ Merged\nMerged into {base}, as reviewed. "` followed by:

    | kind | text |
    |---|---|
    | `steps` | "Activation not verified: its steps and check are on the Activate card." |
    | `unknown` | "Activation unknown: no reviewed handoff matches this merge; see the Activate card." |
    | `none` | "Nothing to activate: {why}." (no doubled period) |
  - **`_merge`:** on MERGED, `transition(..., outcome=merged(req, activate(req, run["repo"])))`.
  - **`_recover`:** on MERGED at the head, the same, with `run_of(req["ledger"])["repo"]`, or `(req.get("run") or {}).get("repo")` when that raises.
  - Regenerate the card goldens and read the `pr-merged` diff.
- **Verify:** `.venv/bin/python -m pytest -q tests/test_decisions.py tests/test_cards.py tests/test_gateway.py tests/test_slack.py`
- **Exists because:** `decisions.merged()` says "Muster does not deploy" and nothing more, and nothing records that a merged change still has to be activated.

## Task 4: merges outside the Merge button get the same handoff

- **Files:**
  - `muster/cleanup.py` (`resolve`)
  - `tests/test_cleanup.py`
- **Nearest pattern:** `resolve` already loops over `conversation.every()` and finds the newest merged PR from `pull_requests(link["repo"], link["branch"])`.
- **Test first** (`tests/test_cleanup.py`, beside `test_resolving_ends_the_saved_approvals_but_not_the_agents_question`, using `resolvable(world)`):
  - **A matching reviewed request:** a build request with `pr=f"https://github.com/{REPO}/pull/16"`, `head=world.head_sha`, `activation="Merge triggers: nothing.\nSteps: restart."` and status `open`. After `main([])`, one card exists with key `muster-activate:t_1`, its reason contains "Steps: restart.", and the request is `stale`. So the handoff was read before the request was marked stale.
  - **A head mismatch:** the same request with `head="f"*40`. The card reason starts "Activation unknown". No text contains "nothing to activate".
  - **No request at all:** "Activation unknown".
  - **Repeated passes:** `main([])` twice, plus a `tick`, leave one card blocked once.
  - **After a Merge tap:** `decisions.handoff("t_1", REPO, 16, world.head_sha)` first, then `main([])`, leaves one card.
  - **A dry run:** no card.
  - **A kanban `create` failure:** the run is not resolved (`resolved_at` absent), and the pass reports failure (exit 1).
- **Change:** in `resolve`, after the `if dry:` block and before marking requests stale, call `decisions.handoff(ledger, link["repo"], newest["number"], newest["head"]["sha"])`. Its exceptions are `core.CommandError` and `ValueError`, which are inside `FAILURES`. Check that `FAILURES` contains them.
- **Verify:** `.venv/bin/python -m pytest -q tests/test_cleanup.py`
- **Exists because:** `cleanup.resolve` is the only code that sees a PR merged on GitHub or by hand, and it closes the run with no handoff.

## Task 5: the final status line is history, not a claim of done

- **Files:**
  - `muster/progress.py` (`state`)
  - `muster/channels/telegram.py` (the topic-closing note)
  - `tests/test_progress.py`
  - `tests/test_gateway.py`
  - `tests/test_cards.py` goldens
- **Nearest pattern:** `progress.state`'s `resolved_at` branch and its `read(kind)` helper.
- **Test first** (`tests/test_progress.py`, beside `test_finished_only_once_the_work_resolved`):
  - The activity `activation` = `steps t_9` gives `("finished", "✅ PR #9 merged · activation not verified at merge; current state on its Activate card", True)`.
  - `unknown t_9` gives "… activation unknown; current state on its Activate card".
  - `none` gives "✅ Finished · PR #9 merged · nothing to activate".
  - No activation file (a run resolved before this change) keeps "✅ Finished · PR #9 merged".
  - **Verified completion:** in `steps` state, completing the card changes nothing Muster reads, so the line stays the same historical line. Assert that it contains no "not active" and no bare "Finished".
  - In `test_gateway.py:1176`, the Telegram note expects "Merged. This topic is now closed."
- **Change:**
  - **`progress.state`:** in the `resolved_at` branch, `kind = read("activation").split(" ")[0]`, then branch as in the tests. The key stays `finished`, because Slack's `parent_line` shows the final line only for key `finished`.
  - **`telegram.py:622`:** "Finished. This topic is now closed." becomes "Merged. This topic is now closed."
  - Regenerate the card goldens if the status-line golden changes.
- **Verify:** `.venv/bin/python -m pytest -q tests/test_progress.py tests/test_gateway.py tests/test_slack.py tests/test_cards.py`
- **Exists because:** `progress.state` says "✅ Finished · PR #n merged" and Telegram posts "Finished." when cleanup sees the merge. Both present merged as done.

## Task 6: the coordinator checks, records and, on request, verifies

- **Files:**
  - `prompts/escalation.md`
  - `prompts/review-standard.md`
- **Nearest pattern:** the cleanup-warning entries in `escalation.md`: the card list entry, plus "reply `[SILENT]` unless you know something it lacks". Also the "Message a running agent" rule in `dispatch.md`, where the human's explicit request is the authority, limited to exactly what they asked.
- **Test first:** none. These are instruction files with no code path. `test_cli` checks that the skills are registered, and `test_prompts` checks that files are tracked. Run both.
- **Change:**
  - **`escalation.md`:**
    - **(a) Card list:** an ACTIVATE card entry, titled "Activate · …". It is a merged change whose activation is not verified. Its block text is the handoff. Its ping is the handoff, so on its wake reply `[SILENT]`.
    - **(b) Step 3:**
      - Read the PR body's `## Activation` with the note and the repository's install and deploy docs.
      - Check it against the diff: every new setting, script or cron entry, hook, migration, dependency or gateway-loaded change has a step; each step names who runs it; Verify and Rollback are present.
      - When the section is missing or wrong, send it back, unless you correct a small gap in your own handoff and say so.
      - Write the handoff file (at most 1500 characters, `none: <why>` only when there is nothing to do or check) and keep what you verified apart from the builder's claims.
      - Add `--activation <file>` to the command.
      - The summary lines "Merge triggers: …" and "After merge: …" replace "Deploy on merge".
    - **(c) New step 5, Activate card:**
      - Act only when the human says it is done or explicitly asks you to carry it out. That request authorizes only that card's steps. Show a config change as a diff before writing it. Never activate on a wake or because a merge happened.
      - Run the card's Verify step (read-only).
      - `hermes kanban --board <board> comment <card> "<command and output>"`, then `complete <card> --summary "✅ Active · verified: <evidence>"`, or `"Reported done by you · not verified: <why>"`.
      - It is the only card you may complete.
    - **(d) "You must not":** add these exceptions:
      - "deploy" → "deploy or activate anything, except as step 5 allows";
      - "unblock, complete…" → "except completing an Activate card as step 5 says".
  - **`review-standard.md`:**
    - In Verification, add the handoff check against the diff and the install docs.
    - In Human decision summary, the "Deploy on merge" sentence becomes: the "Merge triggers" and "After merge" lines are the one place for the merge and activation consequence.
    - Keep "no deployment boilerplate" and adjust it so the two lines are the exception.
- **Verify:** `.venv/bin/python -m pytest -q tests/test_cli.py tests/test_prompts.py`, then `grep -n "Deploy on merge" prompts/` prints nothing.
- **Exists because:** `escalation.md` asks only for "Deploy on merge" and forbids the coordinator to move any card, so it can neither check a handoff nor record verified activation.

## Task 7: docs, the drafted muster note, learnings

- **Files:**
  - `README.md`
  - `docs/reference.md`
  - `docs/setup.md`
  - `docs/channels.md`
  - `docs/architecture.md`
  - `docs/plans/2026-10-21-muster-production-note.md` (new)
  - `docs/learnings.md`
- **Nearest pattern:** the existing "Production notes" section in `docs/setup.md`, and the "Build review and merge" and "Cards" sections in `docs/reference.md`.
- **Test first:** none (docs).
- **Change:**
  - **README:**
    - "What you decide": the Pull requests bullet says the review states what a merge triggers and what is left. Add an "Activation" bullet: the Activate card, your authority, verified vs reported.
    - "How a run goes": a step between merge and cleanup.
  - **reference.md:**
    - an "Activation after merge" subsection: the handoff, the three routes, the texts, the card lifecycle, and the coordinator's role on request;
    - Cards: an Activate card bullet;
    - the "never … moves a card" sentence gains the exception.
  - **setup.md, Production notes:** the note should also say what a merge triggers, the install or update procedure, verification and rollback.
  - **channels.md:**
    - the Telegram close note text;
    - the Slack parent final line;
    - the status table's final row.
  - **architecture.md:** step 6 mentions the activation handoff (`decisions.handoff`, also from `cleanup.resolve`). The module map's `decisions` row gains "the activation handoff".
  - **Muster note draft:** what merge triggers (nothing; the clone lives at `~/.hermes/plugins/muster`), the gate, `hermes plugins update muster`, copying `scripts/*.sh` when they changed, `config.yaml` settings when added, `hermes gateway restart`, verify, rollback. Head it "Drafted from docs/setup.md and the local clone; not validated live. Copy to `<notes_dir>/jasonrr__hermes-muster.md` if you want it."
  - **learnings.md:** add a `## activation-after-merge (2026-10-21)` bullet: a prompt edit committed without `MUSTER_REGOLD` left main with 2 failing tests → regenerate goldens in the same commit as any prompt edit.
- **Verify:** `.venv/bin/python -m pytest -q` (full suite) and `~/.local/bin/hermes plugins validate .`
- **Exists because:** the user docs describe a merge as the end ("Finished"), and setup says notes cover only where production runs.

## Order and review

Run the tasks in order 1 → 7. After each task, a fresh cheap sub-agent reviews that task's diff against its section here and `docs/review-lenses.md`. Fix its p1s before the next task. Before the PR, a final review covers the live tree.

## Amendments from the plan attack

A fresh sub-agent attacked this plan twice ("where would you guess" and "trace one failure per task"). Every finding is folded in below. **Where an amendment and a task above disagree, the amendment wins.**

### Baseline and Task 1

- **Golden diff allow-list.** Also allow the `launch-envelope` first-line change from c459877. That commit is the other edit committed without regenerating the goldens.
- **The new prompt file.** `git add prompts/activation-handoff.md` before Verify (`test_every_prompt_the_code_names_ships_in_git…`). Write a literal `$` as `$$`. Avoid any word containing "tally", because `test_brief_has_workflow_and_no_tally` checks for it.
- **Placement in the PR body.** The `## Activation` section goes immediately before the final `Closes #n` line, which stays last. In an ad-hoc PR, with no Closes line, it goes at the end. The prompt says exactly this.

### Task 2

- **`--activation` help text:** "file with the checked activation handoff; its text is 'none: <why>' when there is nothing to activate".
- **Where it is read and checked:** with the other file reads, before the dedupe early return, so a bad file is refused even then. On the dedupe branch, the open request keeps its text, as today ("the new text was ignored"). That behavior is unchanged and visible on stderr.
- **The two-line check covers the card's text either way.** It applies to the text the card shows: the summary, or the review when there is no summary. A short review without the lines is refused.
- **More tests to update:**
  - `test_decisions.py` at line 1335 (its summary needs the two lines);
  - `test_cards.py:113`, where the `kept` tuple's "Deploy on merge" becomes "Merge triggers";
  - `tests/acceptance/test_lifecycle.py` at about line 282: add `--activation` and the two lines. Running it is opt-in (`MUSTER_ACCEPTANCE=1`) and is not part of this plan's verify. Say so in the PR.

### Task 3

- **Signatures:**
  - `activation_for(ledger, repo, number, head)` returns `(kind, text, url)`, where `url = f"https://github.com/{repo}/pull/{number}"`.
  - `activate(req, repo)` returns `(kind, text)`.
  - `merged(req, kind, text)`. For `none`, `why` is the text after `none:`, with any trailing "." removed. The outcome is "Nothing to activate: {why}.".
- **Matching:**
  - Use `.get("pr")` and `.get("head")`, because requests without them exist in tests.
  - Require the colon: `^none:`, case-insensitive. A handoff starting "None of the steps…" is steps, not none.
  - Prefer a matching request with `recommended == "merge"`. Otherwise use the newest match, and prefix the text with "{who} recommended {choice}, not merge, for this commit; check before activating."
- **Card title:** the fallback run is `{"title": "", "repo": repo, "card": ledger}`, read with `.get("run")`.
- **Idempotency key:** `muster-activate:{ledger}:{head}`. The tap, `_recover` and `resolve` all pass the merged head, so they find the same card. A later merge of another head gets its own card.
- **One process at a time:** create, subscribe and block run under `decisions.locked(f"activate-{ledger}")`, so the gateway (tap) and cron (`resolve`) never both subscribe and block.
- **Where the ping goes:** `core.subscribe(id, ledger if <the run's conversation is open and not resolved> else None)`, as `cleanup.escalate` does for resolved runs. A closed topic never gets the ping. A run's card subscriptions end when the run is archived (`shared.unsubscribe_cards`). After that, a completion ping may not arrive, and the coordinator's own reply is the notice. Document this limit.
- **Failures:**
  - `activate` catches `Exception` (like `execute`) and logs it. The reason: `core.subscribe` raises `core.LaunchError` and `notify_target` raises `config.ConfigError`, and neither is in the earlier list.
  - On a failure it returns kind `pending`. `merged` then says "Activation not verified: Muster could not open the Activate card yet; cleanup retries it. Steps: the PR's Activation section, {url}."
- **Tests:**
  - Call `progress.touch("t_1", "launched", …)` first, so the `activation` activity file can be written.
  - Update `test_decisions.py:392`. Its `build()` request has no activation, so the outcome is the `unknown` one.
  - Filter `world.calls` to `gh` calls at line 392 and in the readback tests near lines 453 and 462.
  - Add a test that a LaunchError from subscribe still leaves the merge `done` with the `pending` outcome.

### Task 4

- **Failures:** around the handoff call only, catch `Exception`, then `say` it, mark the pass failed and `continue`. The run is not resolved, and the next pass retries. A broken subscription never aborts the rest of the cleanup pass.
- **The cron log line:** append `; activation {kind}` to the resolve `say` line.
- **Test fake:** a create whose key starts `muster-activate:` gets the id `t_act<n>`, so `overdue_cards` (`t_warn` prefix) ignores it.
- **Existing tests that now see an Activate card:**
  - `test_a_resolved_runs_unsaved_work_warning_goes_to_the_main_chat` (its `world.ledgers`);
  - any equality on `world.cards`, `world.blocks` or `world.subscribed`.

  Give `resolvable()` no request, so the unknown card appears, and adjust those assertions to ignore `t_act` cards. Assert the activation card only in the new tests.
- **A known limit, documented:** `resolve` sees only runs with a conversation reference (`notify_topics` on, as it is on this machine). With topics off, a merge outside the Merge button gets no card, as it gets no resolution today.

### Task 5

- **Slack parent fallback:** `slack.PARENT["done"]` becomes "Merged" and the `close()` docstring says so. `gateway.show_progress`'s `posting and final` branch also saves `key` and `text`, so the parent can show the final line.
- **Telegram tests:** in `test_gateway.py`, the filters at lines 1175 and 1611 that match "Finished" now match "Merged.".

### Task 6

- **Card count:** "five kinds of card" becomes six.
- **The Activate card wake:** it is recognized by the title "Activate · " before the generic blocked-wait rule. The coordinator replies `[SILENT]` on its block wake and on the wake from its own completion.
- **Commands:** use `comment <card> -- "<text>"` and `complete <card> --summary …`.

### Task 7

Add a learnings bullet: a new exception type outside a caller's `except` tuple (`core.LaunchError` from `subscribe`) would fail a merge that landed → wrap a side step after an irreversible action with `except Exception` and a log line.
