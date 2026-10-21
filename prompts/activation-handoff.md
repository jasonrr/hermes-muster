A merge is not activation. Someone still has to make the change live, so your pull request body carries an
`## Activation` section that tells them how. Put it immediately before the final `Closes #n` line (which stays
last), or at the end of the body when there is none. Muster carries it to the human after the merge.

- **Merge triggers:** what merging does in this repository, citing the note above or the repository's own docs
  (a deploy workflow, an install or update guide), or "unknown: nothing says".
- **Steps:** in order, every command and every change outside this pull request: settings in a config file outside
  the repository, scripts to copy, migrations, restarts. Give a config change as the exact text to add and where.
  Name a secret; never write its value.
- **Who:** who runs each step, and what approval it still needs. Merging authorizes none of them.
- **Verify live:** a command, its expected output, and what it proves. Mark each step and check tested (you ran it
  here) or untested.
- **Rollback:** how to undo the activation.

When the merge alone is the whole change and there is nothing to run or check (documentation or tests only), write
"Nothing to activate:" and why instead.

Never run a step yourself: no install, deploy, restart, migration, or write to configuration outside this worktree.
