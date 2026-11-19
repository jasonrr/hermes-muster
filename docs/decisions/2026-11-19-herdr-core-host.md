# herdr-core as an optional terminal host

## Problem

Muster runs builders in herdr, the TUI server, by default, and in Orca as an option (#73). The Herdr desktop
app runs its terminals in herdr-core (`hc`). Muster should offer `host: herdr-core` as a third choice next to
those two. Every route must work on it: issue runs, ad-hoc runs, analysis workspaces, holds and decisions, and
cleanup. Issue #79 has the draft plan, the hc facts and the lessons from Orca. This record settles the plan's
four open questions.

## Chosen approach

One module, `muster/hosts/herdr_core.py`, with `NAME = "herdr-core"`, added to `hosts.NAMES`. `hosts.get` maps
`-` to `_` when it imports the module. The module speaks the seam's contract, the docstring in
`muster/hosts/__init__.py`, the way `orca.py` does. It adds no new state and no new process. The mapping table
in #79 stands, with these decisions:

- **Identity.** The workspace is the checkout path, and each pane is an hc terminal name. Every terminal muster
  starts carries the label `muster.path=<path>`, so `panes` is `hc ls --label muster.path=<path>`. This works
  with or without a window. Every terminal is started with `--no-parent --outlive-parent`, so muster's runs
  don't end when the shell or gateway that launched them ends. A name that is already live means reuse.
- **Visibility (Q1).** When muster needs a space and `herdr-app status` fails because no window is open,
  muster runs `herdr-app` (no arguments opens the window) and waits a bounded time for `status` to answer.
  Then it runs `space.new --folder <path>` and labels its terminals `herdr.space=<id>`. If the window never
  comes up, for example because nobody is logged in to the GUI, the run proceeds hidden and the log says so.
  `docs/setup.md` gives the one-liner that surfaces a hidden run: `herdr-app do terminal.keep --terminal
  <name>`, or `hc attach <name>`. The space is set up on the reuse path too, not only on create (Orca lesson 4).
- **Delivery (Q2): type-then-send, with the seam unchanged.**
  - `open_tab` is a shell started with `hc run --bg`.
  - `start_agent` types `env K=V claude <args>` into that shell with `hc send`, then waits until hc reports
    the agent idle.
  - `prompt` is `hc send --wait --timeout 30`:
    - a turn that is working or done, or a check-in on a working agent, counts as delivered;
    - `not_started` raises `CommandError` (unproven);
    - the blocked refusal raises `hosts.Blocked`.
  - Holds and decisions use the same `prompt`.
- **Agent state.** `hc get --json` gives `status.state`, which maps to `agent_status`. `error` passes through,
  so it reads as neither ready nor delivered. `turn` becomes the seq fields. When the foreground program is
  the shell, `agent` returns None.
- **Core restart (Q3).** A kept terminal comes back as a bare shell, so `agent()` returns None. The run then
  takes the existing agent-gone path: an alert, then `recover --resend`. No new recovery code.
- **Quiet.** Uses `foreground.argv` and each muster terminal's status, checked against `QUIET` and `HELPER`.
  If hc exposes a terminal's root pid, quiet reuses `hosts.under`; that is a probe item.
- **Naming (Q4).** The setting value is `herdr-core`. From now on, docs call the existing `herdr` host
  "herdr TUI"; its setting value stays `herdr`.

The build follows #79's task list. Task 1 is the live probe, run with the window open and closed. It pins the
JSON shapes, `send --wait --timeout` on a long turn, whether `space.new` adds its own shell terminal (which
quiet must then allow), whether bare `herdr-app` opens the window when muster is run from cron or the gateway,
and what a core restart does to names and labels. The verify commands are the ones listed in #79.

## Cut and why

- **Auto-surfacing hidden runs on later ticks:** muster opening the window at launch covers the normal case,
  and the docs give the manual one-liner. Add it if hidden runs turn out to be common.
- **Auto `claude --continue` after a core restart:** the existing resend path covers this rare event.
- **One-shot `hc run claude "<brief>"`:** it would change the seam for every host. hc documents
  wait-then-send as supported.
- **hc remote machines, templates and `hc events`:** as listed in #79. Nobody asked for them, muster builds
  its own argv, and cron polling is enough.

## Open questions

None are deferred. Task 1's probe answers the shape questions above, and none of them changes the approach.

## Deviations

- T1 commit a0aaf06 — deviation: none (only the `calls` helper added now; `jump`/`checkout` come with the tasks that use them); reviewed by the controller directly (plan transcription, diff matches) — parked: none
- T2 commit 901cf67 — deviation: none (fake is the plan code verbatim; jump/checkout helpers added); reviewed by the controller directly (byte-identical to the reviewed plan code) — parked: none
- T3 commits 056ab2a, 77cc4a3 — deviation: review fixes beyond the plan: up/window/space also catch OSError (a missing herdr-app or open binary runs hidden, never fails a launch); panes() counts an agent only with a foreground, like agent(); live-checked that a turn stopping at a dialog after typing exits 0 with wait.status blocked (no duplicate-brief risk). Not fixed, ruled: p3 a prompt to an agent that exited between take_agent and prompt reads delivery-unknown (safe; Blocked would mislead); p3 non-JSON hc output raises ValueError (every hc call prints JSON, live check) — parked: none
