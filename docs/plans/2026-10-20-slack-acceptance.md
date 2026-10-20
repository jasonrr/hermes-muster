# Slack acceptance harness: builder plan

Design: [2026-10-20-slack-acceptance-design.md](2026-10-20-slack-acceptance-design.md). Read it first: §4 is the lifecycle under test, §5 the isolation layers, §5a the preflight, §6 the gates.

**Handoff boundary.** This run wrote only these two documents. A fresh builder in its own worktree implements the plan.
- V1 (Tasks 0–11) may start only after Jason approves the design and the dependency below holds.
- V2 and V3 (Tasks 12–15) also need Jason's completed one-time setup (design §7) and his explicit go-ahead.

## Scope

**In scope:** design §5 and §5a, the unit-level diamond assertions and the gate.

**Non-goals:** everything design §8 cuts. Also:
- no change under `muster/`;
- no change to Hermes;
- no change to production config;
- nothing filed upstream.

## Dependencies

1. **Sibling fix t_5b73f25f merged as #59 (35e6507).**
   - **What #59 fixed:** the diamond, at the Slack seam `channels/slack.py:parent_line`, with `progress.WORKING`. It also added a link from the thread parent to the open review.
   - **What #59 left:** it does **not** retire the notifier's "🔍 is reviewing" reply or the coordinator's tool bubble. Its learnings section ("slack-working-and-review-order") says `cleanup_progress` likely never fires for these wake turns, because `session_ingress.deliver_response` skips the post-delivery callbacks.
   - **What follows:**
     - On `main`, Task 7's assertion A should pass.
     - Assertion B (no stale "is reviewing" reply, no leftover bubble) should **fail**.
     - The control pair is therefore: 007767e fails A and B, and `main` passes A and fails B. A red B is the harness working, not a harness bug. **Never loosen B.**
     - The gate reports it as FAIL, or BLOCKED once B is a strict `xfail` citing that Hermes path, until Jason decides the fix. The options are an upstream Hermes change or an approved fallback or waiver.
2. **A Hermes checkout** at `$HERMES_AGENT_DIR`, default `~/.hermes/hermes-agent`. It is read-only: Task 0 proves nothing is written into it.
3. **The test interpreter** `$PY` = `~/.cache/muster-acceptance/venv/bin/python`, built in Task 0. Hermes's production runtime (`~/.hermes/tools/python-3.14…`, run with `-I`) and the checkout's two venvs lack `pytest` and `aiohttp`.

## Verified facts (2026-10-20, Hermes 3c2f1cf86c)

| Fact | Source |
|---|---|
| `hermetic_env(home, extra)` strips real `SLACK_*`, `HERMES_*` and `*_TOKEN`, then applies `extra`. `PYTHONPATH_PREPEND` goes first on `PYTHONPATH`. It sets `HOME` to the test home | `tests/e2e/core/platforms/_helpers.py:30-32,42-70` |
| `GatewayUnderTest(root, llm_base_url=, config=, env=, ready=)`. `run_cli` uses `sys.executable -m hermes_cli.main` with `stdin=DEVNULL`, so it has no stdin | `_helpers.py:103-165` |
| `Director` routes on the token in `[in:<token>]` in the last user message, else `"none"` | `_helpers.py:33,66-100` |
| `SlackDriver`: `gateway_config()` returns `platforms.slack.{enabled, extra.require_mention}`. `gateway_env()` holds the stand-in tokens, `HERMES_STANDIN_SLACK_API`, `SLACK_ALLOWED_USERS` (`U0000111`, `U0000301-306`) and `PYTHONPATH_PREPEND=_SHIM`. `connected()` means `auth.test` arrived and a socket is open | `tests/e2e/core/platforms/_drv_slack.py` |
| `SlackStandin.block_action(user, channel, message_ts, action_dict)` passes `action_id`, `block_id`, `type` and `value` through. `visible(ch)` returns `Visible(message_id, text, edits, deleted, extra)`, with `thread_ts` and `blocks` in `extra`. `buttons(ch)` returns button elements only. It also provides `ephemerals(ch)` and `calls_of(...)`; `Call.at` is monotonic time | `tests/fakes/platforms/slack_standin.py:347,507-527,539-543`; `_standin.py:26-55,163` |
| `FakeLLMServer`, `ToolCall(name, args)`, `Text(text, chunk_chars=8, delay_per_chunk=0.0)`, `write_hermes_home` | `tests/fakes/fake_llm_provider.py:43-70,420` |
| Plugin discovery is `$HERMES_HOME/plugins/<name>` (symlinks followed) plus `plugins.enabled`. Modules load under a namespace (`hermes_plugins…muster.core`) | `hermes_cli/plugins_discovery.py:209`; `plugins_loader.py:739`; `plugins.py:1654` |
| `hermes kanban boards create <slug>` | `hermes_cli/kanban_boards.py:75` |
| The kanban notifier runs in the gateway by default, starting after 5 s, then every 5 s. The wake text includes the card title | `gateway/kanban_watchers.py:~80`; `kanban_watchers_notifier.py:624-652` |
| Display resolution is `display.platforms.<p>` > `display.<key>` > tier. `cleanup_progress` deletes tracked bubbles after a successful final response | `gateway/display_config.py:3-35`; `gateway/run_turn_progress.py:126,992` |
| The terminal tool passes `HERMES_HOME` into its subprocess | `tools/environments/local.py:439` |
| The Hermes `slack` extra is `slack-bolt`, `slack-sdk` and `aiohttp`. The `dev` group holds pytest | `pyproject.toml` (`[project.optional-dependencies] slack`, `[dependency-groups] dev`) |
| The production `hermes` launcher `exec`s a `python3 -I` script that pops `PYTHONPATH` | `~/.local/bin/hermes` → `~/.hermes/hermes-agent/.hermes/bin/hermes` |
| Muster's `core.run` is the seam for herdr, gh and git. `prepare_env` prepends `$HOME/.local/bin:/opt/homebrew/bin` | `muster/core.py:99,118` |
| Ad-hoc launch requires a GitHub origin | `muster/adhoc.py:237-241` |
| An ad-hoc run completes on `hook stop`; `hook done` returns 1. `verify` runs `git rev-parse HEAD`, `git status --porcelain --untracked-files=no`, and `gh pr list … --json url,state,headRefOid,baseRefName,isDraft` | `muster/adhoc.py:35-82,364-366`; `tests/test_runs.py:1035-1038` |
| `recommend-design <ledger> --proposal v<n>:<sha12> --choice approve\|revise\|judgment --review FILE [--feedback FILE] [--summary FILE]`, where `sha12` is `sha256(text)[:12]` of the proposal | `muster/cli.py:49-58`; `muster/events.py:363` |
| `recommend <ledger> --pr URL --head SHA --choice merge\|send-back\|nothing --review FILE [--feedback FILE]`; it runs `gh pr view … --json url,state,headRefOid,headRefName,baseRefName,isDraft,isCrossRepository` | `muster/cli.py:41-48`; `muster/decisions.py:244-249` |
| Gateway scan period: 2 s | `muster/channels/shared.py:14` |

## Nearest in-repo patterns

| Area | Pattern |
|---|---|
| Stateful CLI fake | `tests/fake_herdr.py:World` (`handle`, `git`, `worktree`) |
| A fake world wired through `core.run` | `tests/test_decisions.py:215` (`world`) |
| Status text per state | `tests/test_progress.py` |
| Block Kit flattening | `tests/test_cards.py:30 flat` |
| Learnings | `docs/learnings.md`: "clear-decision-messages" and "issue-run outbox" |

## Tasks

### Task 0: test interpreter, with the checkout proven untouched
- **Files:** `tests/acceptance/README.md` (commands only).
- **Steps:**
  1. Snapshot the checkout:
     ```
     S=$(mktemp -d)
     git -C "$HERMES_AGENT_DIR" status --ignored --porcelain > $S/before
     touch $S/stamp
     ```
  2. Build the venv:
     ```
     UV_PROJECT_ENVIRONMENT=~/.cache/muster-acceptance/venv uv sync --project "$HERMES_AGENT_DIR" --frozen --no-install-project --group dev --extra slack
     ```
     Then expose Hermes's own modules without installing it:
     ```
     echo "$HERMES_AGENT_DIR" > ~/.cache/muster-acceptance/venv/lib/python*/site-packages/hermes-checkout.pth
     ```
  3. Check the imports:
     ```
     $PY -c "import pytest, aiohttp, slack_bolt, slack_sdk, yaml, hermes_cli"
     ```
  4. Run Hermes's own Slack contract test as a smoke boot:
     ```
     cd "$HERMES_AGENT_DIR" && $PY -m pytest -p no:cacheprovider tests/e2e/core/platforms/test_slack_contract.py -q -x
     ```
     It must pass, or show only that file's `KNOWN` xfails.
  5. Check that nothing changed:
     ```
     git -C "$HERMES_AGENT_DIR" status --ignored --porcelain | diff $S/before -
     find "$HERMES_AGENT_DIR" -newer $S/stamp -not -path '*/.git/*' -print
     ```
     Both must print nothing. **Any output fails the task.**
- **Exit:** if steps 3–4 fail twice for environment reasons, stop and report. Never patch Hermes.
- **Verify:** steps 3–5, with their output pasted into the PR.
- **Exists because:** no interpreter on this machine can run the stand-in, and the checkout is production code.

### Task 1: diamond assertions at the unit level
- **Files:** `tests/test_slack.py`. #59 puts the shortcode in `slack.parent_line`, gated on `progress.WORKING`.
- **Seam:** assert through `slack.parent_line` and through `slack.show` against `tests/fake_slack.py`. Extend #59's tests in `tests/test_slack.py`; never duplicate them.
- **Test first**, written from the spec in design §4, not from the fix's output:
  - `working` starts with `:diamond_spin: ` and keeps `Working · last activity`.
  - `quiet`, `received`, `turn`, `ended`, `reviewing`, `review`, `reviewed`, `revision`, `waiting`, `finished` and `archived` contain no `:diamond_spin:`. Set the state mtimes with `os.utime`, as the existing tests do.
- **Mutation check:** temporarily make `working` plain text, confirm the test fails, then revert. Record the result in the PR.
- **Verify:**
  ```
  uv run --group dev python -m pytest tests/test_slack.py -q
  ```
- **Exists because:** defect 1. #59's own tests cover only some states; this task covers every non-working state.

### Task 2: the shim, the world server, the PATH scripts
- **Files:**
  - `tests/acceptance/shim/sitecustomize.py`
  - `tests/acceptance/world.py`: `AcceptanceWorld(fake_herdr.World)` plus `serve(world, sock_path)`, a thread that reads one JSON argv per Unix-socket connection and answers `{stdout}` or `{error}`.
  - `tests/acceptance/test_shim.py`: ordinary suite, no Hermes needed.
- **Shim behavior:**
  1. If `MUSTER_CHAIN_SITECUSTOMIZE` is set, `runpy.run_path` it first.
  2. Install a `sys.meta_path` finder that wraps the loader of any module whose `Path(__file__).resolve()` equals `Path($MUSTER_UNDER_TEST)/muster/core.py`, resolved. After `exec_module`, set `module.run = shim_run`, then write `markers/<pid>.json` with `{pid, argv, patched: true}`.
  3. `shim_run(argv, env=None)` routes on `basename(argv[0])`:
     - `hermes` goes to the original `run`.
     - `herdr`, `gh` and `git` are sent to `$MUSTER_WORLD_SOCK`. An `{error}` reply is raised as `module.CommandError(error)`. If the socket is unreachable, raise `CommandError("acceptance shim: no world")`.
     - Anything else raises `CommandError("acceptance shim: refused …")`.
  4. Every call and every refusal is appended to `journal.jsonl`, by the world server for forwarded calls and by the shim for refusals.
- **AcceptanceWorld:** a subclass of `World`. It adds only what the scenarios reach, each method citing the muster call site it serves. An unmodelled argv returns `{error: "unmodelled: …"}`, which is journaled and fails the test.
  - `git remote get-url origin` returns `https://github.com/demo/demo.git`.
  - `git rev-parse HEAD` returns the worktree's sha. `git status --porcelain --untracked-files=no` returns empty.
  - `gh pr list` returns the PR `{url, state: OPEN, headRefOid: <current sha>, baseRefName: main, isDraft: false}` once `world.pr` is set.
  - `gh pr view` returns `{…, headRefName: <branch>, isCrossRepository: false, mergeStateStatus: CLEAN}`.
  - `gh pr merge` is recorded.
  - `herdr agent prompt` is recorded with its text and calls `on_submit`.
- **PATH scripts** (written by the rig in Task 4): `$HOME/.local/bin/hermes` is `#!/bin/sh` and exports `PYTHONPATH`, `MUSTER_*` and `HERMES_HOME` literally, then `exec $PY -m hermes_cli.main "$@"`. `herdr` and `gh` there append to `journal.jsonl` and `exit 97`.
- **Test first:**
  - Each of the routes above.
  - No world socket means refusal.
  - `bash` is refused.
  - The journal is appended in order.
  - The chain runs first.
  - **Real-load test:** symlink the repo into a temp dir, load `muster/core.py` through `importlib.util.spec_from_file_location` under a dotted namespace name, as Hermes does, with the finder installed. Assert `run is shim_run` and that a marker was written.
- **Verify:**
  ```
  python3 -m pytest tests/acceptance/test_shim.py -q
  ```
- **Exists because:** `prepare_env` reaches the real `herdr` and `gh`, and the production `hermes` launcher ignores `PYTHONPATH` (`-I`).

### Task 3: the confined kit module and its guard
- **Files:**
  - `tests/acceptance/hermes_kit.py`, the only place that imports Hermes's `tests.*`. Each name carries a comment with its Hermes file:line and the upstream ask.
  - `tests/acceptance/test_kit.py`.
- **Behavior:**
  - Load the kit with `importlib` under the alias `hermes_tests`. Temporarily put `$HERMES_AGENT_DIR` first on `sys.path`, pop any `tests` entry from `sys.modules` before and after, and restore `sys.path`. Muster's own `tests/` (no `__init__`, importlib mode) is never shadowed.
  - If `HERMES_AGENT_DIR` is unset, skip with that reason. If it is set and any name or parameter is missing, **fail** with "Hermes test kit shape changed: <name>".
- **Test first:** assert every name and parameter in the facts table: `GatewayUnderTest`'s `llm_base_url`, `config`, `env` and `ready`; `hermetic_env`'s `extra`; `SlackStandin`'s `block_action`, `visible`, `buttons`, `ephemerals` and `calls_of`; `Text`'s `delay_per_chunk`; `Director.script`; `SlackDriver`'s `gateway_config`, `gateway_env` and `connected`.
- **Verify:**
  ```
  MUSTER_ACCEPTANCE=1 HERMES_AGENT_DIR=~/.hermes/hermes-agent $PY -m pytest tests/acceptance/test_kit.py -q
  ```
- **Exists because:** AGENTS.md requires a loud test for any reliance on a non-public Hermes shape. This is a test-only reading, recorded in design §5.

### Task 4: the rig
- **Files:**
  - `tests/acceptance/conftest.py`: the `rig` fixture.
  - `collect_ignore_glob = ["test_lifecycle.py", "test_preflight.py", "test_kit.py"]` unless `MUSTER_ACCEPTANCE=1`, so the ordinary suite never shows a green run that skipped acceptance.
- **Steps**, in order:
  1. **Root.** `root = tmp_path_factory.mktemp("acc")`, short for AF_UNIX paths. `home = root/"home"`. `clone = root/"clones/demo"` (`git init`; the world serves its remote).
  2. **World.** Create `AcceptanceWorld(root)` and serve it on `root/"world.sock"`. The world's `on_submit` touches the builder's prompt evidence, as `tests/fake_herdr.py` does.
  3. **Plugin.** Symlink `home/.hermes/plugins/muster` to `$MUSTER_UNDER_TEST`, which defaults to the repo root.
  4. **Config** passed to `GatewayUnderTest(config=…)`:
     - `plugins: {enabled: [muster], isolation: in_process, entries: {muster: {settings: {…}}}}`, where `settings` is:
       - `approver_login: tester`, `approver_id: 1`
       - `repos: ["demo/demo=<clone>"]`, `board: muster-acc`
       - `notify_platform: slack`, `notify_chat_id: C0ACCEPT`, `notify_user_id: U0000111`
       - `notify_chat_type: group`, `notify_topics: true`
       - `coordinator_name: Ella`
       - `worktrees: <root/worktrees>`
     - `platforms.slack` from `SlackDriver.gateway_config()`, plus `extra.allowed_channels: [C0ACCEPT]`.
     - `display: {tool_progress: all, tool_progress_grouping: accumulate, interim_assistant_messages: true, cleanup_progress: false, platforms: {slack: {cleanup_progress: true}}}`. Its constant `DISPLAY` is shared with the gate.
  5. **Environment** passed to `env=`:
     - `SlackDriver.gateway_env()`, with `SLACK_ALLOWED_USERS` extended by `U0000222`, so that Muster, not Hermes, rejects the non-approver.
     - `PYTHONPATH_PREPEND=<repo>/tests/acceptance/shim`
     - `MUSTER_CHAIN_SITECUSTOMIZE=<SlackDriver's shim dir>/sitecustomize.py`, read through `hermes_kit`.
     - `MUSTER_UNDER_TEST`, `MUSTER_WORLD_SOCK`, and `MUSTER_JOURNAL`/`MUSTER_MARKERS` (under `root`)
     - `HERMES_KANBAN_HOME=<home/.hermes>`
     - `HTTPS_PROXY=HTTP_PROXY=http://127.0.0.1:9`
     - `PATH=<home/.local/bin>:<os.environ PATH minus ~/.local/bin>`
  6. **PATH scripts.** Write the three from Task 2 into `home/.local/bin`.
  7. **Static preflight** (Task 5) runs here, before any child starts.
  8. **Board.** `gw.run_cli("kanban", "boards", "create", "muster-acc")`, then write `human_notices: true` into `home/.hermes/kanban/boards/muster-acc/board.json` (`docs/setup.md` step 3).
  9. **Start.** Start the stand-in and the `FakeLLMServer(Director())`, then the gateway with `ready=driver.connected`. Then run the dynamic preflight.
  10. **Teardown.** Stop the gateway, the stand-in, the LLM and the world server. Nothing outside `root` is touched.
- **Test first:** `test_rig_boots`. The gateway is alive, the gateway marker exists, the canary marker exists and the journal is empty.
- **Verify:**
  ```
  MUSTER_ACCEPTANCE=1 HERMES_AGENT_DIR=~/.hermes/hermes-agent $PY -m pytest tests/acceptance -q -k rig_boots
  ```
- **Exists because:** design option B. If the plugin fails to load, fix the key from `docs/setup.md` or `docs/channels.md`; never guess.

### Task 5: fail-closed preflight and its negative tests
- **Files:** `tests/acceptance/conftest.py` (`preflight_static`, `preflight_dynamic`) and `tests/acceptance/test_preflight.py`.
- **Static checks** (before start). Each raises `Preflight("<check>: <detail>")`.
  - **paths:** writable state stays under `root`, and the only allowed outside path is the plugin's code.
    - `HERMES_HOME`, `HERMES_KANBAN_HOME`, the `worktrees` setting, every repo path and the data directory (`HERMES_HOME/plugin-data/muster`) must each satisfy `.resolve().is_relative_to(root)`.
    - **The plugin's code** is the only path allowed outside `root`. `home/.hermes/plugins/muster` must be a symlink whose `.resolve()` equals exactly the pinned `Path($MUSTER_UNDER_TEST).resolve()`. Muster writes only into its data directory, never into its plugin directory, so that code path is read-only.
    - Any other symlink, or any other resolved target outside `root`, fails.
  - **channel:** `notify_chat_id in ALLOWED = {"C0ACCEPT"}`.
  - **path-scripts:** `shutil.which(x, path=env["PATH"])` is the script under `home/.local/bin` for `hermes`, `herdr` and `gh`.
  - **tokens:** the env's `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` equal the stand-in's.
  - **egress:** the proxies point at the dead port.
- **Dynamic checks** (after start):
  - **gateway-marker:** the gateway's marker exists.
  - **canary:** `subprocess.run([home/.local/bin/hermes, "muster", "recover", "nosuch"], env=child_env)` leaves a marker and journals nothing.
  - **identity:** `calls_of("auth.test")` shows the response `user_id == "UBOT0001"` and `bot_id == "B0STANDIN"`.
  - **no-real-slack:** the gateway log has no `slack.com` line.
- **Test first:**
  - **Positive test:** `test_preflight_accepts_valid_layout`. The standard rig, with the plugin symlinked to the checkout outside `root`, passes static and dynamic preflight. Assert that the plugin resolves to `MUSTER_UNDER_TEST` and that every writable path lies under `root`.
  - **Negative rigs**, each expecting `Preflight` with a named check:
    1. The shim dir is left off `PYTHONPATH_PREPEND` (gateway-marker).
    2. `worktrees` is set to `Path.home()/"muster-acc-should-not-exist"`, i.e. writable state outside `root` (paths). Assert the path does not exist afterwards.
    3. The plugin symlink points at a different directory than `MUSTER_UNDER_TEST` (paths).
    4. `notify_chat_id: C0NOTALLOW` (channel).
    5. The `hermes` script is removed (path-scripts).

    Each must also show `journal.jsonl` absent or empty and `calls_of("chat.postMessage") == []`.
- **Verify:**
  ```
  MUSTER_ACCEPTANCE=1 HERMES_AGENT_DIR=~/.hermes/hermes-agent $PY -m pytest tests/acceptance/test_preflight.py -q
  ```
- **Exists because:** Jason's v2 review asked for fail-closed checks with negative cases.

### Task 6: timeline artifact
- **Files:** `tests/acceptance/conftest.py`: `timeline(rig)` writes `root/timeline.md`, and a `pytest_runtest_makereport` hook prints its path and its last 60 lines on failure.
- **Content:**
  - Every `chat.postMessage`, `chat.update` and `chat.delete` in order: seconds since the rig started (from `Call.at`), method, ts, thread_ts, and the first 80 characters of `text + flat(blocks)`. `flat` is copied from `tests/test_cards.py:30` by import.
  - Then `visible(C0ACCEPT)` grouped by thread.
  - Then the journal and the markers.
  - Anything matching `xox[a-z]-\S+` or `xapp-\S+` is redacted.
- **Test first:** `test_timeline_order_and_redaction`. Post two messages through the stand-in client directly, include a fake `xoxb-SECRET` in one, and assert the order, the grouping and the redaction.
- **Verify:**
  ```
  ... -k timeline
  ```
- **Exists because:** the brief requires a readable timeline.

### Task 7: design path, approve (the first regression case)
- **Files:** `tests/acceptance/test_lifecycle.py`.
- **Helpers** (in this file):
  - `hook(event, card, payload=None, *args)`: `subprocess.run([home/.local/bin/hermes, "muster", "hook", event, "--card", card, *args], input=json.dumps(payload), env=child_env)`.
  - `thread_messages()`: the visible bot messages under the run's thread, as `text + flat(blocks)`.
  - `expect_exactly(*patterns)`: the thread replies match these patterns in order, with nothing extra. This is the fail-closed bubble rule from design §5.
- **Steps:**
  1. **Launch.** Title `"Demo [in:d1]"`. The `[in:d1]` token is how `Director` routes the coordinator's wake turn, because the wake text carries the title. Run `hermes muster launch --cwd <clone> --branch feat/x --title … --brief <file>` and read the card from stdout.
  2. **Builder works.** `hook("prompt", card, {"hook_event_name": "UserPromptSubmit", "prompt": <brief>})`, then `{"hook_event_name": "PostToolUse", "tool_name": "Bash"}` (`progress.py:77`).
  3. **Assert A** (wait up to 30 s): the parent's text contains `:diamond_spin:` and `Working`.
  4. **Script and propose.** Write `plan.md` and compute `sha12 = sha256(text)[:12]`. Script `director.script("d1", ToolCall("terminal", {"command": "hermes muster recommend-design <card> --proposal v1:<sha12> --choice approve --review <root>/review.md"}), Text("[SILENT]"))`, with `review.md` written beforehand. Then run `hook("propose", card, None, "<plan.md>")`.
  5. **Assert B** (wait up to 60 s for a card with buttons, then hold for 3 scan periods):
     - The parent reads `🟡 Your decision: design v1`.
     - `expect_exactly(<builder question redirected | absent>, "Ella's full review · Design v1", "Design v1 ·.*Approve design v1")`. No "is reviewing" reply and no bubble.
     - A marker exists for the `recommend-design` child.
  6. **Tap.** `standin.block_action("U0000111", "C0ACCEPT", card_ts, <the Approve button from buttons()>)`.
  7. **Assert C:** the card starts `✅ Approved · Design v1`, the journal has exactly one `herdr agent prompt`, and the parent no longer shows `🟡`.
- **Positive control:** `test_bubble_rule_detects`. The same scenario up to B, with `display.platforms.slack.cleanup_progress: false`. `expect_exactly` must fail on the extra bubble.
- **Control pair:** at 007767e (`MUSTER_UNDER_TEST=<throwaway worktree>`), expect A and B to fail. On `main`, expect A to pass and B to fail (see Dependencies). Paste both timelines into the PR, and explain any difference not caused by Muster's diff.
- **Parent text:** the parent also carries #59's review link. That is in the parent's text, not in the reply allow-list.
- **Wake-turn cleanup:** if B fails only on the bubble, with the control proving detection, and Hermes's code shows `cleanup_progress` skips internal turns, mark that one assertion `xfail(strict=True, reason="<Hermes file:line>")`. The gate reports it as BLOCKED.
- **Verify:**
  ```
  MUSTER_ACCEPTANCE=1 HERMES_AGENT_DIR=~/.hermes/hermes-agent $PY -m pytest tests/acceptance/test_lifecycle.py -q -k "design_approve or bubble_rule"
  ```
- **Exists because:** defects 1 and 2 on the design path.

### Task 8: design send-back, late and out-of-order
- **Files:** `tests/acceptance/test_lifecycle.py`.
- **Scenarios:**
  1. **Send-back.** The coordinator uses `--choice revise --feedback <file>`. The tap is `block_action(…, {"action_id": "muster_text", "block_id": <the card's input block_id from Visible.extra["blocks"]>, "type": "plain_text_input", "value": "Tighten section 2"})`, as `slack.py:415` reads it. Assert:
     - the card reads `↩️ Revisions sent`;
     - one `herdr agent prompt` containing "Tighten section 2";
     - the parent reads `↩️ Feedback delivered`.
  2. **Late recommendation.** `Text("[SILENT]" + " " * 160, chunk_chars=8, delay_per_chunk=0.25)`, about 5 s, so the turn ends after the card is shown. B holds after the turn ends.
  3. **v2 while v1 is open.** Propose v2 before tapping v1, with the coordinator scripted twice. v1's card settles as stale, only v2 has buttons (`standin.buttons`), and `expect_exactly` holds for v2.
- **Verify:**
  ```
  ... -k "send_back or late or v2"
  ```
- **Exists because:** the brief's send-back receipt and late or out-of-order cases.

### Task 9: PR path
- **Files:** `tests/acceptance/test_lifecycle.py`.
- **Steps:**
  1. After the builder works, set `world.pr = "https://github.com/demo/demo/pull/7"`.
  2. Run `hook("stop", card, {"hook_event_name": "Stop"})` (`adhoc.verify` then completes the card).
  3. Script `[in:p1]` (title `"Demo [in:p1]"`) as `hermes muster recommend <card> --pr <url> --head <world sha> --choice nothing --review <file>`.
  4. Assert as in B, with `PR #7`.
- **Variants:**
  - **Leave open:** the card reads `⏸`, and no `gh pr merge` is journaled.
  - **Send back** (`--choice send-back --feedback <file>`, tap): one `herdr agent prompt`.
  - **Merge** (tap Merge): the journal shows `gh pr merge <url> --squash --match-head-commit <sha>`, answered by the world, and the `gh` stub shows no call.
- **Verify:**
  ```
  ... -k pr_
  ```
- **Exists because:** defect 2 also appeared on the PR path, and both paths are required.

### Task 10: duplicate tap, non-approver tap, restart
- **Files:** `tests/acceptance/test_lifecycle.py`.
- **Scenarios:**
  1. **Duplicate tap.** The same action is sent twice. There is one `herdr agent prompt`, and `ephemerals` shows "Already answered" (`slack.py:396`).
  2. **Non-approver.** The tap comes from `U0000222`. `ephemerals` shows "Not authorized" (`slack.py:481`), nothing is journaled, and the card's edit count is unchanged.
  3. **Restart.** `gw.stop(); gw.start()` while the review is open. Wait until the `chat.postMessage` count has been stable for 3 scan periods, plus one notifier tick (≥ 11 s). Then:
     - the card's `chat.postMessage` count across both lives is exactly 1;
     - the parent's ts is unchanged;
     - `expect_exactly` holds.
- **Verify:**
  ```
  ... -k "duplicate or non_approver or restart"
  ```
- **Exists because:** the brief's missing, duplicate and stale cases, and the review lens `channel-safety`.

### Task 11: gate and docs
- **Files:**
  - `tests/acceptance/gate.py` and `tests/acceptance/test_gate.py` (ordinary suite).
  - `docs/setup.md`: a "Before installing an update" subsection.
  - `docs/channels.md`: replace the "Not verified live" line.
  - `docs/learnings.md`: one `## slack-acceptance (2026-10-xx)` section.
- **Gate behavior:** `$PY tests/acceptance/gate.py [--muster <path>]`.
  1. Require `HERMES_AGENT_DIR`, else print `BLOCKED: HERMES_AGENT_DIR unset` and exit 2.
  2. Run `MUSTER_ACCEPTANCE=1 $PY -m pytest tests/acceptance -q -rA --junitxml=<tmp>`.
  3. Read `~/.hermes/config.yaml`. Copy out **only** these key paths into a new dict: `display.tool_progress`, `display.cleanup_progress`, `display.platforms.slack.*`, and `plugins.entries.muster.settings.notify_platform` and `notify_topics`. Discard the rest. A parse error prints `config: unreadable`, never the exception text.
  4. Print one verdict and exit 0 only on PASS:
     - **PASS:** all passed and the config matches `DISPLAY`.
     - **FAIL:** anything failed.
     - **BLOCKED:** any skip, xfail or xpass, each with its reason.
     - **CONFIG MISMATCH:** the differing key **names** only.
- **Test first:**
  - Canned junit XML for pass, fail, skip and xfail.
  - Canned configs, including one with `slack_bot_token: xoxb-SECRET` and one that is unparseable.
  - Assert the verdict and the exit code, and that `xoxb-SECRET` appears in neither stdout nor stderr.
- **Docs:**
  - `docs/channels.md`: "checked offline against a real Hermes gateway and Slack stand-in; not verified live". V2 and V3 are not done.
  - `docs/setup.md`: run `gate.py` before every install, and only PASS proceeds. Add the `display.platforms.slack.cleanup_progress: true` line as an activation step **for Jason to apply**. This run changes no production config.
- **Verify:**
  ```
  python3 -m pytest -q && HERMES_AGENT_DIR=~/.hermes/hermes-agent $PY tests/acceptance/gate.py
  ```
  Expect BLOCKED or CONFIG MISMATCH until Jason applies the config line. That output is the honest result; report it.
- **Exists because:** design §6.

### Tasks 12–15: V2 live and V3 browser (only after Jason's setup and his go-ahead)

**Task 12: setup check**
- **Files:** `tests/acceptance/live_setup.py` and `tests/acceptance/identities.json`.
  - `identities.json` holds `allowed_team_id: "T7ZUQSGLR"`. The test app and channel are intentionally in the production workspace.
  - It also holds a production deny-list: bot `user_id` `U0B01SEV4D9`, plus the production `bot_id` and `app_id`, which Jason adds once at setup.
- **Behavior:**
  - Require `~/.hermes-muster-acceptance/ACCEPTANCE_HOME`.
  - Read that home's `.env` for the test bot token only.
  - Call `auth.test`, then `bots.info(bot=bot_id)`, and print `team_id`, `user_id`, `bot_id` and `app_id`.
  - **Require** `team_id == allowed_team_id`.
  - **Refuse** if `user_id`, `bot_id` or `app_id` matches any production identity.
  - Write `acceptance.json` (the four values plus the test channel id) only after Jason types `yes`. Task 13's live preflight then requires an exact match on all four.
- **Test first**, with a canned client:
  - **Positive:** the allowed team, with a different test bot (`UTEST…`/`BTEST…`/`ATEST…`), is accepted.
  - **Negative:** production bot credentials in the allowed team (`U0B01SEV4D9` and the production `bot_id`/`app_id`) are refused, one case per field.
  - **Negative:** a different `team_id` is refused.

**Task 13: live mode**
- **Files:** `tests/acceptance/conftest.py`, adding `MUSTER_ACCEPTANCE_MODE=live`.
- **Behavior:**
  - There is no stand-in, `root` is the acceptance home, and there is no proxy.
  - Preflight runs the common and live-only checks (design §5a). The identity is re-checked with `auth.test` immediately before the first write, and again before each delete.
- **Test first:** fixture homes with a production `bot_id`, and with a missing marker, abort before any socket opens.

**Task 14: live lifecycle**
- **Behavior:**
  - Run Task 7 up to assertion B, and Task 9's leave-open variant up to its card. There is no tap in V2.
  - Read back with `conversations.replies`.
  - Cleanup deletes only the ts values this run recorded, in the pinned channel.
  - Artifacts go to `~/.cache/muster-acceptance/runs/<date>/`; runs older than 30 days are deleted. The run aborts after 10 minutes.

**Task 15: V3 browser**
- **First step:** check whether Python `playwright` imports in `$PY`. Hermes's `google-meet` extra pins `playwright==1.62.0`. If adding that extra is the route, ask Jason first.
- **Profile:** a dedicated `--user-data-dir` under the acceptance home, mode 0700 and git-ignored. The test user logs in once, headed, by hand, and the harness never reads its cookies.
- **Behavior:** click the real `Approve design v1`, take a screenshot at each checkpoint, save a trace zip, and assert receipt through the journal.

## Review and PR

- **Reviews:** a cheap sub-agent reviews each task's diff against `docs/review-lenses.md`, and a sub-agent attacks the whole branch before the PR.
- **PR body:** the control-pair timelines, the gate verdict, the Task 0 commands and diff, the `check-ref-format` exception, the kit upstream ask (not filed), and every BLOCKED item with its reason.
