# Why this design

Muster reflects a preference: keep engineering knowledge close to the code, and put a clear human
interface around its execution. This is a design opinion, not evidence that it outperforms every other
software factory.

## The folder is the agent

A designer or reviewer needs to understand the code they are working on. Repository instructions such
as `AGENTS.md` and `CLAUDE.md`, project history, review guidance, tests and linters give that work its
context. They should not have to be recreated as a separate factory-owned team configuration.

Improvements committed to the repository travel with its checkouts. A person running a coding harness
directly can benefit from the same instructions and tooling without adopting Muster. The aim is to
make the factory replaceable without losing the project's engineering knowledge.

This does not rule out separate designers, implementers or reviewers. Fresh contexts and different
permissions can be useful. The distinction is where their working practices belong, not whether roles
should exist.

## Useful defaults today, more delegation over time

Many repositories do not have a complete engineering process written down. Muster therefore supplies
a default workflow: design approval, implementation planning, test-first development and independent
reviews. The builder also uses repository guidance, including project-specific review lenses when
present. Today's design is a hybrid, not a repository-only process.

The intended direction is to make it easy for a repository to take ownership of more of that workflow
through its own instructions or configuration. There is no general repository-level delegation setting
today. Muster's existing `workflow_prompt_file` setting replaces the builder workflow for the configured
installation; it is not automatic discovery of a process in each repository.

Muster's core instructions should increasingly focus on the human-facing layer: when to surface a
question, how to explain what is happening, what decision is needed and what each choice will do.
Plain language and understandable state matter more here than elaborate agent personas.

## Why herdr: subscription access and visible work

Muster uses herdr to run ordinary, interactive coding-harness sessions. Those sessions can use a coding
subscription instead of separately metered model API calls. The value depends on the plan, workload
and usage limits; neither savings nor unlimited access are guaranteed. The Hermes coordinator and
other services have their own usage costs.

The terminal is also a direct view of the work. You can watch the agent, answer it or take over in its
pane rather than depending entirely on a factory dashboard or summary. The coding session remains
accessible alongside Muster's orchestration.

Muster currently supports Claude Code only. Other or multiple harnesses are a possible extension of
this approach, not a supported capability today.

## GitHub identity is a control, not a sandbox

There are separate identity boundaries:

- **Starting issue work:** Muster checks who applied the approving label. Only the configured human,
  or a bot explicitly allowed under an automatic-approval rule, can authorize that route.
- **Builder access:** by default the builder uses your `gh` login. Set `gh_config_dir` to give it a
  separate GitHub identity. That identity's repository access and GitHub's applicable permission and
  branch rules constrain operations made with those credentials. You choose how restrictive to make it.
- **Merging:** Muster's merge path uses your own `gh` login after your explicit decision, not the
  builder's configured bot login.

A restricted builder identity is a useful access control, but it does not isolate the local agent.
The builder runs as your operating-system user and is not sandboxed; other accessible credentials or
local resources remain relevant. Prompt instructions are not permission enforcement, and repository
instructions alone cannot guarantee that every checkout follows a process. Use appropriate GitHub
rules, credential restrictions and execution isolation where those guarantees are required.

## Chat is the interface, not the invention

Telegram and Slack make it convenient to direct a run without opening its terminal. Human approvals
and questions also exist in Hermes and coding-agent SDKs; they are not unique to Muster. The aim is a
coherent experience around the whole run: clear progress, questions at the right time, reviewed results
and explicit decisions.

For operation rather than rationale, see [setup](setup.md), [channels](channels.md) and
[reference](reference.md).
