# Plan: the builder owns dependency setup (t_38d558f3)

Approved design: Proposal v2 (ca693dea45eb) on t_38d558f3, option "R1 + prompt". This repo builds only the
builder's setup instructions, for both routes. R1 is a separate radicalcandorai run that Jason authorizes:
its tracked post-checkout hook starts `setup-worktree.sh` in the background, with the SessionStart hook's log
and lock. It is listed in the pull request's Activation section, not built here.

Why: #518's 308 s `herdr worktree create` was 300 s of one hung `npm audit` request inside the synchronous
post-checkout bootstrap. Its timeline is in the proposal. Muster keeps native herdr creation and never skips
a hook, so it cannot move that install itself. What Muster owns is the brief: with setup happening in the
background (R1, or the global SessionStart hook), the builder must find, wait for, verify and report on
setup itself.

Test command (no venv in a fresh worktree; reuse the main clone's):
`PY=/Users/jasonrosoff/Code/hermes-muster/.venv/bin/python`, then `PYTHONDONTWRITEBYTECODE=1 $PY -m pytest -q -p no:cacheprovider <tests>`.

## Task 1: the setup section in both briefs

Files: `prompts/dependency-setup.md` (new), `prompts/issue-dispatch.md`, `prompts/ad-hoc-run.md`,
`muster/intake.py` (`brief`), `muster/adhoc.py` (`footer`), `tests/test_prompts.py`, `tests/golden/*.txt`
(regenerated), `tests/test_launch.py`.

Nearest pattern: `ask-the-human.md`. It is one file that both templates include through a placeholder,
`$ask`, filled in `intake.brief` (intake.py:101) and `adhoc.footer` (adhoc.py:190). The new section is
`$setup` = `config.prompt("dependency-setup")`, in the same two calls. It goes inside the brief, not in
`launcher.ensure`, because the issue route saves the brief as its audit copy
(`git_dir / core.BRIEF_FILE`), and `test_core.py:302` requires that copy to equal what was delivered.

Placement: the `## Dependency setup` header is in each template; the prompt file is the body only, with no
blank line at its ends, since `config.prompt` strips them. In `issue-dispatch.md` it goes after the
`## Asking the human` section (`$ask`) and before `## How to work`. In `ad-hoc-run.md` it goes after the
"Never push" and attribution lines and before `## Production and activation`, so those lines stay outside it.

Test first, in `tests/test_prompts.py`: `test_both_briefs_carry_the_dependency_setup_section(sandbox)`. Use
`sandbox`, the conftest fixture the other tests use, so `notes_dir` is not the real one. Both
`intake.brief("o/r", 1, True)` and `adhoc.footer({"card": "t_1", "repo": "o/r", "branch": "feat/a",
"base": "main"})` contain `"## Dependency setup\n\n" + config.prompt("dependency-setup") + "\n\n"`, placed as
above (assert the index order of the neighbouring headings). The section text contains, exactly:
"never start a second run", "setup trouble, not a finding about your task" and "Ask the human with your ask
tool". Watch the test fail on the missing prompt file.

Test first, in `tests/test_launch.py`: `test_the_launch_runs_no_setup_command_before_the_brief`, through the
existing `ensure(world, rec)` helper (not a full tick). Over the list entries of `world.calls` (skip the
`{"saved": ...}` dicts): every argv[0] is `herdr`, `git` or `gh`. Every herdr verb (`c[1:3]`) is a worktree,
tab, agent or pane call; every git verb (after `-C <path>`) is fetch, rev-parse, rev-list, update-ref or
status. Do not substring-scan the argv: paths and the brief contain "setup". Exactly one `agent prompt`, after
`agent start`. Also add `"dependency-setup"` to the parametrize list of
`test_a_missing_prompt_file_fails_the_launch_before_any_agent_starts`.

Route coverage: in `tests/test_core.py`, the issue launch test (around line 302) asserts that the decoded
brief contains `"## Dependency setup"`. In `tests/test_runs.py`,
`test_launch_brief_is_the_callers_text_plus_the_run_footer` asserts that the saved `brief.md` contains it.
Both routes deliver through the real `launcher.ensure`.

Code: write the prompt (text below), then `git add prompts/dependency-setup.md` straight away. The
tracked-prompts test reads `git ls-files`. Add the header and `$setup` to both templates, and pass
`setup=config.prompt("dependency-setup")` in `intake.brief` and `adhoc.footer`. Regenerate goldens in the
same commit:
`MUSTER_REGOLD=1 PYTHONDONTWRITEBYTECODE=1 $PY -m pytest -q -p no:cacheprovider tests/test_prompts.py`.
Then `git diff tests/golden`: only `issue-bug-human-bot`, `issue-feature-auto-nonote`,
`issue-feature-human-nobot` and `ad-hoc-footer` change, by the new section alone.

Already-launched runs: `ensure` fixes `rec["text"]` once, and the existing resend tests in test_launch.py
already pin that a recover sends the saved bytes. No new test; run them.

Prompt text (`prompts/dependency-setup.md`, no `$`):

(The Task 1 review changed four things: a lock with no live process is Failed, Never run re-checks once,
only files that were clean before setup are restored, and the herdr allowlist checks exact verb pairs. The
shipped file is the source of truth.)

> Muster does not install dependencies for you. When you start, this checkout's setup may be finished, still
> running in the background (a post-checkout or session-start hook may have started it), or not run at all.
> Before the first test, build or command that needs dependencies, and only if your task needs one:
>
> 1. Find the repository's setup and whatever runs it: its agent instructions, README, contributing guide and
>    setup scripts, its post-checkout hook (`git rev-parse --git-path hooks/post-checkout`), and any
>    SessionStart hook in your Claude Code settings. Together they name the setup command, its completion
>    marker, and the lock and log of a run in the background. Use only the commands they define. Never
>    invent an install command, and never install what your task does not need.
> 2. Judge its state, and never start a second run of it:
>    - **Done:** its completion marker exists, or its documented check passes. Use it.
>    - **Running:** its lock, or a live process of its script, exists. Wait for it to end: `sleep 30` between
>      short checks, then judge again. If its log shows no new output for 15 minutes, stop waiting and ask
>      the human.
>    - **Failed:** it ended without its marker, or its log shows an error. Read the end of the log, then run
>      the setup once yourself, in the foreground.
>    - **Never run:** run it once, in the foreground.
> 3. Confirm it succeeded (exit status 0, and the marker if there is one) before you rely on it.
> 4. Setup must leave no tracked change: if it rewrote a tracked file such as a lockfile, restore that file
>    with `git checkout -- <file>`.
>
> If setup fails, that is setup trouble, not a finding about your task. Do not report it as a product bug,
> and do not work around it with an install of your own. Ask the human with your ask tool, giving the
> command, its exit status and the end of its log.

Verify: `PYTHONDONTWRITEBYTECODE=1 $PY -m pytest -q -p no:cacheprovider` (the whole suite; ~5 min)

Exists because: neither brief says how to find, wait for, verify or report repository setup. Checked:
`grep -ri 'setup\|bootstrap\|depend' prompts/` finds only "Add no new dependency".

## Task 2: docs

Files: `prompts/README.md` (inventory row for `dependency-setup.md`, plus `$setup` in the
`issue-dispatch.md` and `ad-hoc-run.md` rows), `docs/reference.md` (~line 288, the list of builder prompt
files), `docs/learnings.md` (a `## agent-owned-setup (2026-10-22)` bullet).

Nearest pattern: the `ask-the-human.md` row and its mentions.

No test: docs only. In reference.md, both the "Issue builder" and "Ad-hoc builder" bullets name the new
file. In prompts/README.md, the new row's inputs column says "none". The learnings bullet records the #518
finding: a 308 s create was one hung `npm audit` request, timed from npm's debug-log birth and mtime.

Verify: `$PY -m pytest -q -p no:cacheprovider tests/test_prompts.py`, then
`grep -n dependency-setup prompts/README.md docs/reference.md`.

Exists because: prompts/README.md says it is the inventory of every prompt, and reference.md lists the
builder's prompt files.

## Not in this plan
- R1 (radicalcandorai hook): a separate run, listed in Activation with its brief.
- Any change to `launcher.take_worktree`, herdr creation or git hooks (v1, withdrawn).
- The 60-second retry.
