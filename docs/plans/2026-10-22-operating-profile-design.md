# Operating profile as a UI setting — design

## Problem

Muster used to hardcode `default` as its notifier profile. The Bean cutover (not committed; patch at
`~/.hermes/profiles/bean/backups/muster-before-readiness-961277ee6e87/cutover-core.patch`) switched to
the profile Hermes resolves at runtime. It changed three things in `core.py`: `hook_cmd()` adds `-p <profile>`,
`hermes_home()` uses `get_hermes_home()`, and `subscribe()` uses `current_profile_name()`. The live `launcher.pane_env()` also has a
change the patch missed: panes get the scoped home instead of the gateway's `os.environ` home. Jason wants
the profile visible and settable in the Hermes UI, not hardcoded.

## What Hermes actually offers (inspected, source at ~/.hermes/hermes-agent 9581b44a97)

- **Per-profile plugins.** Plugins load once per profile home (`hermes_cli/plugins.py:1620-1750`), and the
  multiplexed host binds each served profile's home with `_profile_runtime_scope`
  (`gateway/run_adapters.py:1067-1100`, `:1742-1757`). Muster's config is `ctx.get_config`, which reads that profile's config.yaml. Its
  state is `plugin_data_dir`, which lives under that profile's home. Cron scripts get
  `HERMES_HOME=<profile home>` (`tools/environments/local.py:232`).
- **Settings form.** The Desktop Settings → Plugins page renders `plugin.yaml` `config_schema`
  (`hermes_cli/plugins_settings.py`, `apps/desktop/src/app/settings/plugin-settings-form.tsx`). It is
  profile-scoped and saves through `save_plugin_setting`. The field types are string, number, boolean, json,
  secret and enum. **Enum choices are static manifest lists only**: there is no "profile" type and no dynamic
  choices. The TUI and the web dashboard render no plugin settings form.
- **Notifier ownership.** `notifier_profile` decides which gateway delivers and wakes
  (`gateway/kanban_watchers_notifier.py:229-246`). A profile that no gateway serves is silently dead. The
  native default is `current_profile_name("user")` (`hermes_cli/kanban.py:1171`).

So the profile muster runs under is the profile it is enabled in, and that profile's config and state come
with it. A setting cannot make an install in profile A run as profile B without splitting config and state
across two homes. That split is the silent migration the brief forbids.

## Chosen approach (recommended)

A new setting, `operating_profile` (`type: str`, default `""`), in `plugin.yaml` and `config.py`.

- **Empty** means current: the profile Hermes resolves, as in the cutover. This is backward compatible: a
  default-profile install still subscribes as `default` and pins no `-p`, and the Bean install still resolves
  `bean`. Nothing is hardcoded.
- **Set** means pinned. `config.require()` checks that the profile exists (`profile_exists`) and that it is the profile muster
  is running in. A mismatch is a `ConfigError` naming both. No subscription, launch or tick runs as the
  wrong owner, and muster never forks state or starts a second scanner.
- **One resolver, `core.operating_profile()`, feeds every path:**
  - `hook_cmd()`: `-p <profile>` unless it is `default`.
  - `subscribe()`: `--notifier-profile` and the read-back.
  - `hermes_home()`: the scoped `get_hermes_home()`.
  - `pane_env()`: the scoped home, plus the kanban home from `kanban_home()`.
  - The human-facing `hermes muster recover` text: carries `-p` too.
  - Readiness (PR #68): left as it is; it already derives the profile from the same scoped home.
- **It checks; it does not move.** The field confirms the profile that already runs muster. It does not
  move muster to another profile. Saving a different profile name makes muster refuse to act. It does not
  re-home anything, and it rewrites no subscriptions, cards, cursors, decisions, receipts or ACKs. Existing
  runs keep the hooks they launched with.
- **Owner moves are out of scope.** This change gives no procedure for moving muster to another profile.
  Each profile has its own state, so toggling plugins between profiles would neither carry existing runs and
  decisions nor rule out two active owners. Any future move needs its own approved, state-preserving
  handoff. Approving this design does not approve changing the live owner.

Desktop mockup (Settings → profile `bean` → Plugins → muster):

```
 Operating profile                                   [ bean            ]
 The Hermes profile muster runs as. Empty = the profile it is enabled in.
 Checks the profile already running muster; it does not move muster. A different name stops muster.
```

## The scope gap: a real dropdown

A dropdown of profile names needs Hermes to support dynamic choices, e.g. `choices_from: profiles` in
`plugins_settings._field_type` plus the Desktop enum control. That is a change outside this repo. Options:

1. **Text field now (in scope), plus a drafted, unfiled Hermes issue for dynamic profile choices.** When
   upstream lands, muster switches the field to that source in one line.
2. **Text field only.**
3. **Hold the PR until a Hermes change is approved and made** (cross-repo; needs your separate approval).

## Build checks (tests first; each result is reported as observed or schema-only)

1. **Native UI field, observed.** In an isolated `HERMES_HOME` with the plugin installed, call the backend that
   the Desktop `plugins.manage` RPC uses: `plugin_settings_fields`, then `save_plugin_settings`, then read the
   config back. Show the field is listed as a string with its label and description, saves, and persists in
   that profile's `config.yaml`. If the Desktop app itself can be driven against the isolated home, record
   that separately. Otherwise, say that its rendering of a string field rests on Hermes's own
   `plugin-settings.test.tsx`. A plugin.yaml/schema assertion alone is reported as schema-only.
2. **Selection.**
   - Explicit non-default `bean` under a bean home override: `hook_cmd` is `-p bean`, `subscribe` stamps and
     reads back `bean`, the home is bean's, and `pane_env` gets bean's home plus the shared kanban home.
   - Empty selection resolves `default` in a default home, with no `-p` (byte-identical to main).
3. **Invalid and mismatch.**
   - An unknown profile, an invalid name, or a name other than the running profile raises `ConfigError`
     before any kanban, herdr or gh call.
   - This holds for tick, launch, the gateway path and subscribe. Assert that zero commands ran.
4. **Shared-Slack readiness.** The PR #68 multiplex tests still pass with `operating_profile` set. Readiness
   uses the same home the setting checks.
5. **No side effects from a change.** Seed saved runs, decisions, receipts, ACKs and notifier
   subscriptions. Change the setting to a mismatch, then run tick, flush and the gateway. Assert every file
   and subscription is byte-identical, nothing is re-sent, and no second subscription appears.
6. **Full suite and the acceptance gate** in isolated homes, never production. Report the pre-existing
   failure (`test_gateway_up_reads_hermess_runtime_status`, which fails on main today) as pre-existing.

## Reload and rollback

- Hermes reads plugin settings at each muster command. Cron ticks, hooks and CLI calls pick up a saved change on their next run.
- The gateway's chat handlers read the setting when muster's gateway code loads its config. Restart the
  gateway for a reliable switch (the docs already require this after an update). No restart is run by this
  work.
- **Rollback:** clear the field, or remove the `operating_profile` key, to return to current. Reverting the PR restores today's code.

## Cut

- Routing to a profile other than the one muster is enabled in: it splits config and state. Cut.
- A cross-profile lock against two installs on one board: each profile has its own config, so a setting cannot
  coordinate them. Cut, along with any owner-move procedure (see above).
- A UI of our own: forbidden, and the native form covers it.

## Open question for Jason

Text field or dropdown: option 1, 2 or 3? (Recommended: 1.) This is Jason's call. Design approval
does not change the live owner.
