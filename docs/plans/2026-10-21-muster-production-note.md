# Draft production note: jasonrr/hermes-muster

This draft comes from docs/setup.md and an inspection of the local clone. It has not been validated live. To use it, copy it to `<notes_dir>/jasonrr__hermes-muster.md`; on this machine that is `~/.hermes-engineering/repos/`. Writing it there is your call: Muster's builders only read notes.

---

# jasonrr/hermes-muster

## Platforms

- Jason's Mac. Muster is a Hermes plugin loaded by the Hermes gateway, a launchd service. Three Hermes cron jobs (`muster-tick`, `muster-flush`, `muster-cleanup`) run copies of `scripts/*.sh` from `$HERMES_HOME/scripts/`.
- The installed plugin is a git clone of this repository at `~/.hermes/plugins/muster`.

## What a merge triggers

Nothing. No CI and no deploy workflow. A merge to `main` changes GitHub only; the installed clone keeps running its old revision until it is updated.

## How a merged change is activated

The human runs these, or asks their Hermes agent to. Merging authorizes none of them.

1. **Acceptance gate** against the checkout to install. Install only on `PASS`:
   `HERMES_AGENT_DIR=~/.hermes/hermes-agent ~/.cache/muster-acceptance/venv/bin/python tests/acceptance/gate.py`
2. **Update the plugin:** `hermes plugins update muster`.
3. **If `scripts/*.sh` changed:** `cp ~/.hermes/plugins/muster/scripts/*.sh "${HERMES_HOME:-$HOME/.hermes}/scripts/"`.
4. **If the change adds or renames a setting:** edit `plugins.entries.muster.settings` in `$HERMES_HOME/config.yaml` using the exact text the pull request's Activation section gives.
5. **If the change touches anything the gateway loads:** `hermes gateway restart`. The gateway loads `__init__.py`, `muster/`, prompts registered as skills and channel handlers, so most changes need it.

## Verify it is live

- `git -C ~/.hermes/plugins/muster log -1 --format='%h %s'` shows the merge commit or a later one.
- `hermes gateway status` shows the gateway started after the update.
- `hermes muster tick --dry-run` exits 0.

## Rollback

`git -C ~/.hermes/plugins/muster checkout <previous sha>`, then `hermes gateway restart`. Revert any `config.yaml` or script change made in steps 3–4.

## What the builder must not touch

`~/.hermes` (its config, scripts, plugins and kanban board), the running gateway, and any cron job.
