> **Historical record (2026-10-18).** Not operating instructions: later work may have changed what this describes. Current behavior is in the [README](../../README.md).

# Design and plan: undoable worktree cleanup (card t_b8907f9d)

Status: Proposal v1 01f6de62ee5f approved 2026-10-18 (D1a, D2a, D3a), and implementation on this branch approved. Built as
planned; the manifest names the git ids `ids` (head, index, work, untracked) and drops the `clone` field.

## 1. The problem, as it stands today

`muster/cleanup.py` removes a quiet, owned coding worktree only when it is **merged** (HEAD is the newest
merged PR head), **clean** (nothing in `git status` except verified scaffolding copies) and **pushed**.
If any of those fails, the worktree stays and the human gets a card: "Unsaved work", or after a day
"Worktree kept after finished work". Finished runs pile up behind blockers that nobody needs to look at.

What I checked live (read-only) on 2026-10-18:
- `radicalcandorwebsite/muster-112` (card t_d58384a8, still blocked). It has only `.agents/...`
  scaffolding untracked, and `rev-list HEAD --not --remotes` is 0, so every commit is pushed. The
  merged head `663c4c45280e` is no longer in the local object store. The single blocker is
  `HEAD 6462b66fa471 is not the merged head`.
- `radicalcandorai/.claude/worktrees/edge-route-sync-guard` still has ` M frontend/package-lock.json`.
  Cleanup has no ownership record for it (a Claude Code worktree), so it is out of scope (§7).
- Clone sizes: hermes-muster 4.4M, radicalcandorwebsite 201M, radicalcandorai 212M, which sets the
  bundle cost (§6).

## 2. Agreed requirements (from the brief, unchanged)

1. Finished, inactive, owned work is removed after its recoverable state has been preserved outside the
   repository and the preservation verified. Dirtiness, unpushed commits and HEAD ≠ merged head stop
   blocking on their own.
2. What gets preserved: staged and unstaged tracked changes (binary included), untracked **non-ignored**
   files, local history as a self-contained Git bundle, and a manifest with the original path, branch,
   HEAD and one exact restore command.
3. What is excluded: everything git ignores (`.env`, local config, build output, `node_modules`). These
   files are never read or copied.
4. Still protected: active agents and processes, open PRs, and unfinished reviews or approvals. If
   preservation fails or cannot be verified, the worktree is not removed.
5. Minimal machinery. Retention is an explicit policy choice. Nothing is deleted on expiry unless you
   authorise it.

## 3. Approach: git's own stash format plus `git bundle`

All of this is native git, with no new dependency and no format of our own. I proved it in a
throwaway repo (scratchpad, not this repo). The repo had a local-only commit; staged and unstaged edits
to the same file; a binary file that was both staged and then edited; a tracked deletion; a rename; a
tracked symlink and a new untracked symlink; untracked files in a new directory; and an ignored `.env`
and `node_modules/`. I preserved it, removed the worktree, **deleted both the clone and the origin**,
and restored. `git status`, `git diff --cached --binary`, `git diff --binary`, file hashes and symlink
targets all came back identical. `.env` and `node_modules` were not in the restore.

Capture (inside the worktree; it writes objects but changes no file, index or ref):
- `I` = `git write-tree` of the real index, committed with parent HEAD. This is the staged state.
- `W` = the tracked working state: copy the index to a temp file, `GIT_INDEX_FILE=<tmp> git add -u`,
  then `write-tree`. This covers modifications, deletions and mode changes.
- `U` = untracked non-ignored files: `git ls-files --others --exclude-standard -z` written to a file,
  then `GIT_INDEX_FILE=<empty tmp> git add --pathspec-from-file=<f> --pathspec-file-nul`, `write-tree`,
  committed with no parent. If the list is empty, `U` is the empty tree. I checked that the I and W trees
  match what `git stash create` makes byte for byte, and that the real index is left untouched.
- `WIP` = `git commit-tree <W tree> -p HEAD -p I -p U`. This is exactly the commit shape
  `git stash push -u` makes, so the restore is plain `git stash apply --index`. It never touches the
  shared stash stack, which is also why I don't use `git stash push`.
- Two temporary refs, `refs/muster-preserve/{head,wip}`, exist only so `git bundle create` has refs to
  name (it drops a raw sha; I checked). They are deleted right after, in a `finally` block, and cleanup's
  global lock means no two passes ever share them.
- `git bundle create repo.bundle refs/muster-preserve/head refs/muster-preserve/wip` with no `--not`
  exclusion. That makes the bundle self-contained: full history up to HEAD and WIP.

Verify (before anything is removed):
- Run `git init` on a fresh temp dir, then `git fetch <bundle> 'refs/muster-preserve/*:refs/muster-preserve/*'`.
  The fetch checks connectivity, so every object must be present.
- The fetched `head`, `wip`, `wip^1..^3` trees must equal the HEAD, W, I and U trees just computed.
  Object ids are content hashes, so equal ids mean byte-exact.
- Write `manifest.json` and the bundle into `backups/cleanup/.tmp-<ts>-<name>/` (mode 0700), then
  `os.replace` the whole directory to `backups/cleanup/<ts>-<name>/`. A directory with a manifest is
  therefore always complete.
- Recheck: compute HEAD, I, W and U again. If anything differs from the manifest, the worktree stays.
  The backup also stays; it is harmless.

Remove: `herdr worktree remove --workspace <id> --force`, under the run's launch lock as today. The branch
stays, as it does now. This is a **change to a stated invariant** ("never with --force"), and it is only
for this path. Two alternatives were worse:
- `git reset --hard && git clean -fd` followed by a non-force remove also has a window between the
  recheck and the reset, and if it fails half-way it leaves a wiped checkout behind.
- Removing without `--force` is impossible: git refuses unclean worktrees.
Ignored files do not block a plain remove, which I confirmed; they are simply deleted with the directory,
as they are today.

Restore. This is the one supported operation; the manifest's `restore` holds it shell-quoted with real
paths:
```
mkdir <dest> && git -C <dest> init -q \
 && git -C <dest> fetch -q <bundle> 'refs/muster-preserve/*:refs/muster-preserve/*' \
 && git -C <dest> switch -q -c <branch> refs/muster-preserve/head \
 && git -C <dest> stash apply --index refs/muster-preserve/wip
```
`<dest>` defaults to the original worktree path. `mkdir` fails if anything already exists there, which
handles the destination conflict: you pick another `<dest>`. (`git init` alone happily reuses a
non-empty directory; I checked.) The result is a standalone repo that needs neither the clone nor
GitHub. To turn it back into a herdr worktree, or to add a remote, you do that by hand. That is a note
in the manifest, not a second supported operation.

Manifest fields: `repo`, `clone`, `worktree`, `branch`, `workspace`, `head`, `wip`, `trees`
(`head`/`index`/`work`/`untracked`), `bundle_sha256`, `bundle_bytes`, the `untracked` list (paths only),
`excluded: "files git ignores (.env, build output, dependencies) were not preserved"`, and `restore`.

## 4. Cases preservation cannot capture faithfully: these still block

Each of these is caught in the check, so the human gets the existing "Unsaved work" card with a
`cannot preserve: …` reason. No removal is attempted.
- **Unmerged index entries** (a conflict in progress: `UU/AA/DD/AU/UA/DU/UD` in porcelain).
  `write-tree` refuses them anyway.
- **Intent-to-add entries** (`git add -N`, porcelain ` A`). Their content is not in the index.
- **An embedded repository or untracked directory** (an `ls-files --others` entry ending in `/`).
  `git add` would record only a gitlink and lose its contents.
- **Submodules**: a worktree with `.gitmodules` and any change. Submodule internals are not captured.
  I am not building a generic backup product for this.

## 5. Which blockers go, which stay

They go, but only when the run is otherwise finished: every PR from the branch is merged into its base
from this repo, none is open, an issue run has its `Closes #n` line, **or** the ledger is `archived`
(the issue #34 path).
- Uncommitted or untracked non-ignored files. These are preserved.
- Local commits not on any remote. They are in the bundle, and the branch also stays.
- HEAD ≠ merged head. Removal no longer needs it. muster-112 is the live example: it would be removed
  with a bundle.

They stay, for the reasons given:
- Any pane busy, a background job, or an agent not idle/done (`quiet`, unchanged). That would be active
  work.
- An open PR from the branch (`under_way`, unchanged). Work is under review.
- No merged PR and the ledger not archived. Not finished; it gets the "Unsaved work" card as today.
- An open or answered decision request, an undelivered outbox event, or a launch/recover holding the
  lock (`owner_free` plus `ledger_lock`). That is unfinished review or approval. **New:** `owner_free`
  now also runs before every *preserved* removal, not only on the archived and retire paths. The clean
  path is unchanged.
- The cases in §4, and any failed or unverifiable preservation.
- Owner-record mismatch, a reused workspace id, unreadable evidence (unchanged).

The quiet window, the fingerprint and the recheck under the launch lock are all unchanged. HEAD and
status are already in the fingerprint, so any edit restarts the 30 minutes.

## 6. Retention and disk (decision D1)

Each preserved removal writes a **full** bundle, roughly the size of the clone's `.git`: about 200 MB
for the website or RCAI, about 4 MB for muster. The bundle is written only when a blocker is waived. A
clean, pushed, head-matching worktree takes today's path and gets no bundle. Each removal logs
`bundle_bytes`.
- D1a (recommended): keep every bundle, never delete one automatically, and log each size and the
  directory total. You prune by hand.
- D1b: automatic expiry after N days. This deletes recovery data, so it needs your explicit yes and a
  value for N.
- D1c: a thin bundle (`--not --remotes`, so it holds only local-only objects, kilobytes). It is not
  self-contained: restoring needs GitHub to still hold the base. That contradicts requirement 2, so I
  list it only because it would cut the cost.

## 7. Orphans, existing leftovers without an ownership record (decision D2)

Owned-run policy does not change for these: cleanup never claims or removes a worktree it has no record
for. Examples are `radicalcandorai/.claude/worktrees/edge-route-sync-guard` (`package-lock.json`
modified, not assumed disposable) and the other `.claude/worktrees/*` and `rc-data-tools-*` checkouts.
- D2a (recommended): nothing automatic. The plan lists them in the PR description, and you decide case
  by case. You can push, discard, or remove by hand.
- D2b: a `hermes muster preserve <path>` command. It runs the same capture and verify on any worktree
  and prints the manifest, without removing anything. It costs about 15 lines and one test, and reuses
  `preserve()`. I only build it if you want it.

## 8. One genuine ambiguity on secrets (decision D3)

Ignored env files are excluded by construction: `--exclude-standard`, and the files are never read. A
secret-shaped file that git does **not** ignore (say an untracked `.env.local` in a repo whose
`.gitignore` misses it), or a tracked `.env` that has been modified, is "untracked non-ignored" or a
"tracked change". Requirement 2 says to preserve those.
- D3a (recommended): preserve it as the requirement says. The backup directory is 0700. The manifest
  lists untracked paths so you can see it.
- D3b: block removal instead (the "Unsaved work" card) whenever an untracked or changed path's basename
  matches `.env*`. That puts the decision in front of you, but it is one more rule.

## 9. Files and tasks (implementation happens only after you approve)

**Task 1: `preserve()` and its verification.** File: `muster/cleanup.py`. Add `preserve(link, clone)`,
about 60 lines, next to `clear()`, following the same backup layout and manifest style, plus a
`preservable(status, path)` check for §4. git calls go through `core.run`, which already takes `env=`
for `GIT_INDEX_FILE`. The commit identity is passed with `-c user.name=muster -c user.email=muster@localhost`.
On any exception the temp dir and temp refs are removed and `NotReady("preservation failed: …")` is
raised.

**Task 2: gate wiring.** File: `muster/cleanup.py`.
- `coding()` splits its problems into *waivable* (rest, unpushed) and *hard* (§4).
- `merged_gate()` gains a flag that skips only its HEAD-equality check.
- When the run is finished (merged gate passes, or ledger archived) and only waivable problems remain,
  there is no warning, and `copies` is replaced by a `preserve` marker. `owner_free(ledger)` runs when
  there is a ledger.
- `coding_target`'s `clear` becomes "preserve when marked, else clear the scaffolding copies".
- `remove()` adds `--force` only when a backup was made.
- The docstring is updated.

**Task 3: tests.** File: `tests/test_cleanup.py`. This extends `real_checkouts` with a bare origin, the
way the existing real-git tests already run real `git worktree remove` inside the fake herdr. Everything
runs in `tmp_path`; nothing live. The tests:
1. A local commit whose remote ref was deleted (`git push origin :branch`), so no remote ref holds it.
   The tree is removed, then restored after the clone and origin are deleted. `git log` includes the
   commit.
2. Staged and unstaged edits to the same file. After restore, `diff --cached --binary` and
   `diff --binary` are byte-identical to before.
3. A binary file, both staged and modified. The restored bytes match.
4. Untracked non-ignored files in a new directory, plus a new symlink. They are restored with the same
   targets.
5. A tracked deletion, a rename and a tracked symlink. `status --porcelain` is identical after restore.
6. An ignored `.env` and `node_modules/`. Neither is in the bundle (`git -C restored ls-tree -r`
   checks every commit) nor in the manifest, and the worktree is still removed.
7. Backup failure: the bundle command fails (via `fail_on`), or the backups dir is unwritable. The
   worktree stays, no manifest dir is left, the temp refs are gone, and the next pass retries.
8. Verification mismatch: the bundle is corrupted after writing (monkeypatched verify step). It is
   kept.
9. Interrupted cleanup: a kill after `os.replace` and before the remove leaves the backup and an intact
   worktree. The next pass preserves again and removes. A kill between the temp refs and the bundle
   leaves refs that the next pass overwrites, and `.tmp-*` directories are ignored.
10. A change after the backup but before the recheck (a file is written between the two). It is kept.
11. Each §4 case keeps the worktree and posts "cannot preserve" on the card. The parametrised cases are
    unmerged, intent-to-add, embedded repo, and submodule.
12. An existing destination: running the restore command twice fails at `mkdir` and the first restore
    is untouched.
13. HEAD ≠ merged head with everything pushed (the muster-112 shape). It is removed with a bundle.
14. Still kept: agent working, open PR, open approval `question` request, undelivered outbox event, no
    merged PR with the ledger not archived.
15. Today's clean path is unchanged: no bundle, no `--force`. The scaffolding-only test passes as is.
16. `--dry-run` writes no backup and removes nothing.

The parametrised `test_resolved_work_closes_its_topic_and_keeps_a_blocked_worktree` expects `dirty` and
`unpushed` to keep the worktree, so those two cases move to "removed after preservation".

**Task 4: docs and skill text.** Files: `README.md` (the cleanup paragraphs at lines 13, 221, 227, 229),
`skills/escalation/SKILL.md` (the "Unsaved work" card is now only for unpreservable or unfinished work,
plus how to restore from a manifest), and `docs/learnings.md` (one entry: git's stash shape plus a
bundle is the backup format).

Verify: `.venv/bin/python -m pytest -q` (the full suite) and `python3 -m py_compile muster/cleanup.py`.
Then a manual `hermes muster cleanup --dry-run`, which I expect to report "would preserve and remove"
for muster-112. I will not do a live destructive run; the first real removal happens on cron once you
merge.

## 10. Non-goals

There is no generic backup tool, no submodule or nested-repo capture, no restore CLI, no expiry, no
adoption of orphan worktrees, and no change to analysis workspaces or to the scaffolding-copy path.

## 11. Material limits

- A write that lands between the final recheck and `herdr worktree remove --force` is lost. The window
  is milliseconds, after 30 quiet minutes and with a recheck under the launch lock.
- A pass killed after the backup and before the remove leaves a duplicate bundle on the next pass. It
  costs disk, not data.
- Bundles can be large (§6).

## Decision needed

Approve this design with D1a, D2a and D3a, or name your choice for D1, D2 or D3. Approving the plan is
not approval to merge. Implementation happens on this branch as a PR for your review.
