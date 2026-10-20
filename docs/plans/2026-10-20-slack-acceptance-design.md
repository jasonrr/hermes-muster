# Slack acceptance testing for Muster: design

**Decision needed from Jason:** approve this revised design and the builder plan below it (v4).

**Already decided in earlier reviews:**
- Option B is V1.
- Tool activity is cleaned up after a successful turn.
- V2 and V3: plan now, build later.

**This version's changes:**
- The plugin's code path is now explicitly allowed in preflight.
- The live identity check requires the allowed workspace and denies production's bot, user and app.

This run writes only the design and the builder plan. A fresh builder implements the plan once Jason has approved the design. Approving research is not approval to code.

## 1. Why Jason caught what tests missed (traced, read-only)

- **No diamond.** `:diamond_spin:` exists in the workspace (emoji search). However, `muster/progress.py:247` still returns plain `Working · last activity …`. The unit tests check status text for each state, and none of them encoded the agreement to use the emoji.
- **The stale review and tool activity come from three owners.** Thread `1792437076.603429` (run t_4e0f4a2d) shows them in this order:
  1. 15:13:07 and 15:21:19: the "🔍 Ella is reviewing design v1 / PR #118" reply. Hermes's kanban notifier posts it. Its text is the summary that `events.review` and `adhoc.complete` set on a card subscribed with `notify+wake` (`core.subscribe`).
  2. 15:13:09 and 15:21:24: the coordinator's tool-progress bubble. Hermes posts it during the woken turn. Production sets `display.tool_progress: all` globally, which overrides Slack's tier default of `off`, and sets `cleanup_progress: false`.
  3. 15:14:19 and 15:22:45: Muster's full review, then its card.

  Muster never receives the message ids for items 1 and 2.

  Muster's tests run its Slack code against `tests/fake_slack.py`. They also use `fake_hermes.Subs`, which records subscriptions but never posts a ping. So **no Hermes-generated message exists in any Muster test**. That is the gap.
- **Sibling run t_5b73f25f merged as #59.**
  - It added the diamond, and a link from the parent to the review.
  - It does not retire the "is reviewing" reply or the bubble. Its learnings say `cleanup_progress` likely never fires for wake turns.
  - So the harness's first cases fail at 007767e. On `main`, the diamond case passes and the stale-reply and bubble case stays red until Jason decides that fix (see §4).

## 2. Four kinds of evidence

| | What it proves | V1 | V2 live | V3 browser |
|---|---|---|---|---|
| Payload | What Muster and Hermes sent (`chat.postMessage`, `chat.update`, `chat.delete` arguments, in order) | ✅ | ✅ | ✅ |
| Read-back | What Slack stores after edits and deletes (`conversations.replies`) | stand-in | ✅ real | ✅ |
| Client layout | What the human actually sees: order, collapsing, emoji image | ❌ | ❌ | ✅ screenshot |
| Interaction receipt | A real click reaches Muster and the builder gets the answer | Socket Mode envelope through real Bolt | ❌ (no clicker) | ✅ real click |

API read-back never proves animation or layout. A screenshot proves that the custom emoji image rendered, not that it animates.

## 3. Options

- **A. Extend Muster's in-process fakes so they emit pings and bubbles. Rejected as the main layer.** We would have to write our own copy of Hermes's notifier and progress display, and the test would then validate itself. It stays honest only for Muster's own payload text, so V1 adds just the diamond assertions at that level.
- **B. A real gateway in a child process. Recommended V1.** Hermes's own test kit already provides every piece:
  - `GatewayUnderTest` and `hermetic_env` (`tests/e2e/core/platforms/_helpers.py`);
  - `SlackStandin`, Hermes's local Slack Web API and Socket Mode server, which drives the **real** Slack adapter through `slack_shim/sitecustomize.py`. It keeps a `visible()` list of what a human sees after edits and deletes, and serves `conversations.replies`;
  - `FakeLLMServer` with a `Director`, which scripts the coordinator's woken turn.

  Muster is loaded from this checkout as a real plugin. Muster adds one test module and an isolation shim. Hermes's own `tests/e2e/core/platforms/test_slack_contract.py` uses the same pattern.
- **C. A live channel served by a second gateway. Staged as V2.** It would prove real read-back, real Socket Mode delivery and real scopes. **Blocker:** it cannot share the production Slack app. Socket Mode spreads one app's events across all of that app's connections, so a test gateway would receive some of Jason's real taps. It needs a second Slack app.
- **D. Playwright driving Slack's web client. Staged as V3, local only.** Playwright 1.63 and a cached Chromium are installed. This is the only way to check layout and real clicks. A bot cannot press its own buttons, and the Web API cannot press Block Kit buttons, so it needs a logged-in **human test identity**. Claude-in-Chrome works only interactively, not unattended.

## 4. Expected lifecycle (the harness asserts this). Approval needed

Design path, at the moment Jason must decide (the PR path is the same, with `PR #n`):
```
┌ Feature: Edit the blog post hero directly from…  · Issue #117
│ 🟡 Your decision: design v1 · since 15:14              ← parent: the ONLY live status, edited in place
└ replies (audit history, append-only)
   Design v1 ready · decide on Ella's review below.       ← builder's question, already redirected
   Ella's full review · Design v1 …                        ← kept
   Design v1 · Ella recommends: approve  [Approve design v1] [Send back with revisions…] [Leave pending]
                                                           ← no "🔍 Ella is reviewing design v1" reply
                                                           ← no tool-progress bubble left behind
```
After Approve, the card becomes `✅ Approved · Design v1 · The builder received your approval`. The parent goes back to `:diamond_spin: Working · last activity 1 min ago`.

Rules:
- `:diamond_spin:` appears only in the working state. Quiet, waiting, reviewing, turn-ended, ended, finished and archived never show it.
- **Retire stale status, but keep audit history.** Once a recommendation exists for version N, no visible reply may say "is reviewing" for version N. The full review, the settled card and the redirected question stay.
- **Tool activity (owned by Hermes). Decided: clean up after a successful turn.** This is the native setting `display.platforms.slack.cleanup_progress: true`.
  - Bubbles show while Ella works and are deleted once a turn succeeds. A failed turn keeps its bubbles, for diagnosis.
  - Trade-off: it applies to every Slack turn, including Jason's own chats with Ella. `tool_progress: off` for Slack is the fallback.
  - Keeping bubbles "below the card" is not offered: Slack keeps a message at the position it was posted, and a bubble posted before the recommendation stays above it. Changing that order would need its own design.
  - Probably not effective: #59's learnings say `cleanup_progress` likely never fires for internal wake turns, because `session_ingress.deliver_response` skips the callbacks. The decision stands; the harness proves it either way.
    - The first harness run answers it. If wake turns are not covered, the cleanup assertion stays a required check. It may be marked `xfail` against a named Hermes gap so that ordinary local runs stay readable. The installation gate still reports it as **BLOCKED** (§6) until cleanup works, or until Jason explicitly approves a fallback or waiver.
    - Whether to propose an upstream change is then a question for Jason; nothing is filed unasked.
    - The plan's activation step lists the config change. This run makes no production change.

## 5. V1 shape

- **Where it lives.** `tests/acceptance/` runs in a test interpreter built from Hermes's own lockfile, never Hermes's production runtime.
  - The ordinary `python3 -m pytest` collects only the harness's pure unit tests (the shim and the gate).
  - The lifecycle tests run only through the gate, or with `MUSTER_ACCEPTANCE=1`.
- **Hermes's test kit is not a public API.** Every import from it is confined to one module, `tests/acceptance/hermes_kit.py`. Each import there names the Hermes file and line and the upstream ask. The ask, "ship the stand-ins as plugin test fixtures", is noted and not filed. A guard test fails loudly if the kit's shape changes.
  - This is a test-only reading of AGENTS.md: `muster/hermes_private.py` stays the only *production* module that touches private Hermes names.
- **Isolation, in three layers.**
  1. **Shim.** `tests/acceptance/shim/sitecustomize.py` wraps `muster.core.run` in every child process, whatever name Hermes loads the plugin under (matched by file path).
     - `herdr`, `gh` and `git` are forwarded to one **world server** in the test process. It holds a stateful `AcceptanceWorld`, a subclass of the repo's own `tests/fake_herdr.World`, which already models herdr, git and `gh pr list`. Every call is journaled. An unmodelled call is refused.
     - `hermes` runs for real, against the test home.
     - Anything else is refused.
     - Without a reachable world server, the shim refuses `herdr`, `gh` and `git` outright.
  2. **PATH.** The test home's `~/.local/bin` holds three scripts:
     - a `hermes` wrapper that runs the test interpreter, with the shim's environment written into the script itself, so it survives the terminal tool's environment sanitising;
     - `herdr` and `gh` stubs that journal the call and exit 97.

     `core.prepare_env` puts `$HOME/.local/bin` first, and `HOME` is the test home, so these scripts win over `/opt/homebrew/bin`. The production `hermes` launcher runs Python with `-I`, which ignores the shim, so it must never be reached.
  3. **No egress.** Children get `HTTPS_PROXY` and `HTTP_PROXY` pointing at a dead loopback port, with `NO_PROXY` set to loopback only. Any call to github.com or slack.com then fails fast.

  The shim exists because `core.prepare_env` puts `/opt/homebrew/bin` near the front of PATH, where the real `herdr` and `gh` live. Without it, a tap could prompt a real builder pane or merge a real PR.
- **Tool-bubble rule (fails closed).** At each checkpoint, the thread's visible bot messages must equal an exact allow-list: the parent, the builder's question, the full review and the card. Any extra message fails the test. A positive control, with `cleanup_progress: false`, must detect the bubble.

### 5a. Fail-closed preflight (before any launch, hook or tap)

The test aborts before it runs any real command or sends any message unless every check for its mode holds. Each check is verified in code, not assumed. A failed check names itself, and the journal and Slack (stand-in or live) must then show zero commands and zero messages.

**Common to both modes:**
1. **The shim is active everywhere.**
   - Every child that loads `muster.core` writes a marker: pid, argv, and `run is shim_run`.
   - Before the gateway starts, `shutil.which("hermes", path=<child PATH>)` must be the wrapper, and `herdr` and `gh` must resolve to the stubs.
   - A canary, `hermes muster recover nosuch`, run through the wrapper, leaves a marker. It reads only local files and journals nothing.
   - The gateway leaves a marker right after it starts.
   - Each coordinator `recommend*` call during a scenario must leave its own marker, or the scenario fails.
2. **Test-owned state.**
   - `HERMES_HOME`, `HERMES_KANBAN_HOME` (set explicitly), `config.data_dir()`, the plugin directory and the worktrees directory all resolve under the mode's **test root**: `tmp_path` offline, or the acceptance home live.
   - The one path allowed outside the root is the plugin's **code**. Its symlink must resolve exactly to the pinned checkout under test, which Muster only reads. Every writable path stays under the root.
   - These static checks run **before** the gateway starts.
   - None of them resolves under `~/.hermes`.
   - The kanban board is one this run created.
3. **Git and GitHub go to the world.**
   - Every configured repo path lies under the test root.
   - Muster's git and gh calls reach `AcceptanceWorld`. Its `git remote get-url` answers `https://github.com/demo/demo.git`, which satisfies launch's GitHub-origin check, so nothing real is fetched or pushed.
   - Two calls bypass `core.run`: `git check-ref-format` (`launcher.py:116,118`, `config.py:75,83`). They touch no repository, and the PR body states this exception.
4. **Identity and channel.**
   - `notify_chat_id` is in the mode's hard-coded channel allowlist.
   - The approver (`notify_user_id`) is the mode's test user.
   - The bot identity matches that mode's pinned identity.

**Offline only:**
- The adapter's API base is the stand-in's loopback URL.
- The child's Slack tokens equal the stand-in's own (`standin.bot_token`, `standin.app_token`). `hermetic_env` strips every real `SLACK_*` value.
- The proxy variables point at the dead port.
- The pinned identity is the stand-in's (`T0STANDIN`, `UBOT0001`, `B0STANDIN`).

**Live only** (§7):
- The test root is `~/.hermes-muster-acceptance/`, and it must contain the marker file `ACCEPTANCE_HOME`.
- Slack credentials come only from that home's `.env`. The environment variables that reach the child are `HERMES_HOME` (that home) and the shim's own settings.
- The identity is pinned in the home's `acceptance.json`, which Jason confirms once during setup:
  - `auth.test` must return the pinned `team_id`, `user_id` and `bot_id`.
  - `bots.info(bot=<bot_id>)` must return the pinned `app_id`. That is the separately verified source for the app id; `auth.test` returns none.
  - `team_id` must equal the allowed workspace. The test app intentionally lives in the production workspace, so the team is not denied.
  - `user_id`, `bot_id` and `app_id` must each differ from production's bot. Production's is user `U0B01SEV4D9` (Ella); its `bot_id` and `app_id` are recorded at setup.
- Hermes's Slack `allowed_channels` must contain only the pinned test channel.

**Negative tests (offline):**
- One test removes the shim from the children's `PYTHONPATH`.
- One points the data directory outside the test root.
- One sets `notify_chat_id` to a channel that is not allowlisted.

Each must abort in preflight with its named reason, with zero journal entries and zero stand-in messages. The live checks get their own negative tests: a fixture `acceptance.json` with a production `bot_id`, or with a missing marker, must abort before any connection.

- **Builder:** the real entry point, `hermes muster launch` and then `hermes muster hook … --card`, with payloads shaped like Claude Code's. **Coordinator:** a scripted woken turn calls `hermes muster recommend-design` or `recommend` through the terminal tool. **Human:** the stand-in pushes `block_actions` as the approver for buttons on the card, and a typed send-back.
- **Artifact** (printed on failure, written under `tmp`): `timeline.md` lists every `chat.*` call in order (method, ts, thread, first line). It also contains the final `visible()` thread and the shim journal.
- Scenarios:
  - Design path: working → reviewing → recommendation → approve → settled.
  - The same path with send-back, which reaches the builder as a revision.
  - PR path: done → reviewing → recommendation → leave open / send back.
  - Late or out-of-order: the recommendation lands before the woken turn finishes; a second proposal (v2) arrives while v1's review is open.
  - A duplicate tap and a non-approver tap.
  - A gateway restart mid-review: nothing is reposted and no message goes missing.

## 6. Gates

- **Every PR:** `python3 -m pytest -q`, which includes the new unit-level diamond assertions. The repo has no CI, so the builder runs this.
- **Before installation or activation, a PASS is mandatory.** An `xfail` or `xpass` on any required lifecycle assertion is reported as **BLOCKED**, not PASS, even though pytest exits green. It stays BLOCKED until the behavior works or Jason explicitly approves a fallback or waiver. V1 runs against the exact commit to install. A read-only check also confirms that production's `display.*` and Muster settings match what the test configures. Results:
  - PASS opens the gate.
  - FAIL blocks it.
  - SKIPPED blocks it too, and the report names the missing prerequisite, for example "HERMES_AGENT_DIR unset" or "Hermes test kit shape changed". A skip never counts as acceptance.
- **Local runs:** the ordinary `pytest` run may still skip V1 when its prerequisites are missing.
- **Wording:** a V1 pass is "verified against a real Hermes gateway offline", never "verified live". "Live" requires V2 or V3 artifacts.

## 7. V2 and V3 (staged)

**One-time human setup.** Jason does this himself, and secrets never go through chat:
1. Create `#muster-acceptance`.
2. Create a second Slack app with Socket Mode, `chat:write`, `groups:history` and `users:read` (`users:read` is needed for `bots.info`).
3. Create a **new**, dedicated Hermes home, `~/.hermes-muster-acceptance/`, and put the app's tokens only in its `.env`. Run the setup check once and confirm the identity it records in `acceptance.json`: `team_id`, `user_id` and `bot_id` from `auth.test`, `app_id` from `bots.info`, and the test channel id.
   - It is not the existing `dev` profile, which may hold work and is not assumed to be disposable.
   - The home holds a marker file, `ACCEPTANCE_HOME`, that preflight requires.
   - Its kanban board, plugin data and worktrees all live inside it.
4. For V3 only: create a test Slack user who is that home's only approver, and log that user in once in a headed, dedicated Playwright profile.

**Live preflight:** the common and live-only checks in §5a.

**Repeatable automation after setup:**
- The gateway runs with `HERMES_HOME=~/.hermes-muster-acceptance`, the same scripted LLM and the same isolation shim.
- Playwright clicks the real buttons and saves a screenshot and trace at each checkpoint.
- Runtime is capped at 10 minutes.
- Cleanup deletes only the message ids this run recorded, in the test channel, and only as the test app.
- Artifacts are kept locally for 30 days. None of this runs in CI: the repo is public and has no workflows.

## 8. Cut

| Cut | Why |
|---|---|
| A generic harness | Nothing needs it |
| CI | None exists, and the repo is public |
| Proving animation | No tool can |
| A Telegram leg | Not requested |
| The defect fixes | Sibling run t_5b73f25f owns them |
