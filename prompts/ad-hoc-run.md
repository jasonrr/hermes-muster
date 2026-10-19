## This run: $card

You are in a herdr worktree of $repo on branch `$branch`, cut from origin/$base.
Kanban card $card tracks this run. You never report it yourself: each time you end a turn,
the run checks GitHub. It is finished only when exactly one open (or merged), non-draft pull request
from `$branch` into `$base` has your HEAD as its head and no tracked file has
uncommitted changes. Then the human is told, with the pull request's link.

$ask

The brief above is the human's request. When it already says the approach and the scope, so that a design
would only restate it, it is the approved design: build. When you would have to choose an approach, cut
scope or resolve an open question, write the design and propose it as above.

Never push to $base, merge, deploy or force-push.

## How to work

$workflow
