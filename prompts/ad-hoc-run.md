## This run: $card

You are in a herdr worktree of $repo on branch `$branch`, cut from origin/$base.
Kanban card $card tracks this run. You never report it yourself: each time you end a turn,
the run checks GitHub. It is finished only when exactly one open (or merged), non-draft pull request
from `$branch` into `$base` has your HEAD as its head and no tracked file has
uncommitted changes. Then the human is told, with the pull request's link.

When you need a decision or a fact you cannot read, ask the human with the AskUserQuestion tool and
wait: that pings them. A question in plain text does not. Before you ask the human to approve a design
or plan, write all of it to a file (approach, scope and non-goals, safety boundaries, trade-offs, the
test plan, the decision you need; no secrets) and run
`$hermes muster hook --card $card propose <file>`, again after every revision.
Then ask with AskUserQuestion, giving the approval question the header `Approval`: without a saved
proposal that question is refused. The human reviews from the cards, not your pane. Never push to $base, merge, deploy
or force-push.
