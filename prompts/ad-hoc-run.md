## This run: $card

You are in a herdr worktree of $repo on branch `$branch`, cut from origin/$base.
Kanban card $card tracks this run. You never report it yourself: each time you end a turn,
the run checks GitHub. It is finished only when exactly one open (or merged), non-draft pull request
from `$branch` into `$base` has your HEAD as its head and no tracked file has
uncommitted changes. Then the human is told, with the pull request's link.

$ask

Never push to $base, merge, deploy or force-push.

## How to work

$workflow
