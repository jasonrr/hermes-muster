# Review lenses

## Lenses
- correctness: attack logic, edge cases, restart/replay and race paths; p1 = wrong behavior a real run hits.
- invariants: AGENTS.md contract — private Hermes names only in muster/hermes_private.py (each with Hermes file:line + upstream issue comment + a loud test); Hermes surfaces, hermes kanban, herdr CLI, gh, stdlib before own code; no stated requirement cut. p1 = private name outside hermes_private.py, or a reimplemented Hermes feature.
- channel-safety: Telegram/Slack decisions and cleanup — only approvers act, agent/human text escaped, a tap never acts on another version, restarts/replays post nothing new, merge re-checks PR ownership, cleanup preserves work in a verified bundle before removal. p1 = non-approver can act, lost work, or a duplicate post.
- simplicity: over-engineering and machinery failing the CLAUDE.md cut test; prefer deletion and native features. Capped at p2.
