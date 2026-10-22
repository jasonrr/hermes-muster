# Learnings

## muster (PR #1, 2026-10-07)

- `python3 -m pytest` assumed by the plan, but the system python3 has no pytest → verify commands fail as written; run them inside a project `.venv` (`uv venv .venv && uv pip install --python .venv/bin/python pytest`, then `. .venv/bin/activate`).
- A contract test file that mixes modules (test_kanban_contract imported rc_intake and rc_run) cannot port whole in the first module's task → assign each test to the task that ports the module it imports, or it silently drops.
- The plan inventoried scripts, not the live crontab: the source ran a third every-minute job (`rc_run.py flush`) the plan never named → read `crontab -l` (or the scheduler) when porting cron-driven code; muster-flush.sh was added at Task 7.
- `hermes plugins validate` uses a RecordingContext whose get_config returns the default, so it never sees Hermes rejecting a plugin setting key whose first segment is reserved (`model`, `plugins`, `security`, `settings`; hermes_cli/plugins_state.py) → every command crashed under real Hermes; only a temp-HERMES_HOME run of the real CLI caught it (setting renamed `agent_model`).
- Hand-mirroring a host path resolver (board db) missed profiles, the `HERMES_KANBAN_DB` pin and slug normalisation → when code runs inside the host process, import the host's resolver instead of copying it.

## base branch (issue #4, 2026-10-07)

- A config suffix on a free-form path (`owner/name=/path@base`) → any `@` already in the path parses as a branch that `check-ref-format` accepts, so the clone path is silently cut; exempt only the unambiguous form (`/@`), document the rest, and validate every git ref argument (`--base -x` reached `git fetch` as an option) in the one function all launches route through (`launcher.plan`).

## hardening (issue #7, 2026-10-07)

- Free text passed as an argparse positional after options (`kanban block --kind K <card> <reason>`) → a reason starting with `--kind=` is parsed as the option and the block fails with exit 2; put `--` before the positionals whenever any of them is agent- or human-written.

## 2026-10-07 carried from rc-intake docs/learnings.md

- A hermes call without `HERMES_HOME` set → it reads another home's board; set it before any `hermes` call. Do not pin `HERMES_KANBAN_HOME` to it: hermes shares one board root across profiles (`kanban_home()`), so pinning forks the board away from the gateway; ask `hermes_cli.kanban_db.kanban_db_path` for the db path.
- A hook that can fail the agent → the agent stalls on a hook error; every fallible step goes inside the retry/log net, exit 0 (only `done` with no context exits 1).
- A Notification hook matcher that includes `idle_prompt` → a wait card for every idle ping; exclude it.
- A hook that opens a wait card without reading the ledger first → a wait card on a finished or blocked ledger; read the ledger first.
- Removing a worktree without checking the pane's agent state yourself → a live agent loses its checkout; check state before `worktree remove`.
- Idle judged from a clock or a single sample → false idle; idle is a persisted first-seen time of an unchanged fingerprint.
- Starting the agent with the task in its argv → the brief is lost or mangled when the agent is slow to start; start the agent empty, then prompt.
- Judging a prompt delivered by the call returning → a brief that never arrived looks sent; save "sending" before the prompt call and judge delivery by herdr state + `completion_seq` + the UserPromptSubmit hook's sha256.
- Trusting `kanban show --json` for the block kind → it lacks `block_kind`; read it from the board sqlite read-only.
- Treating an untrusted-path `agent_not_ready` as a failure → a retry loop on a question only a person can answer; it is a person-wait.
- A test fake that keys a resource globally when the real system keys it per repo (fake herdr: branch unique across all clones) → a cross-repo bug cannot even be reproduced; key fakes the way the real system does.

## issue-run outbox (issue #5, 2026-10-07)

- Saving an event only after its delivery failed (the design the issue proposed) → a hook killed mid-move still loses it; save first, deliver from the outbox, and make every move read state first so a replay never repeats one.
- A routing check on "the directory exists" (`intake.recover`: `runs/<card>/` means an ad-hoc run) → it misroutes as soon as another kind writes there (issue cards' outboxes); key on the record file (`run.json`), not its directory.

## automatic approvers (issue #2, 2026-10-08)

- A rule that requires "both labels" from one dict keyed by label name → an entry whose label equals the main label collapses it to one label event, silently weakening the rule; reject the collision in `config.require()`, and reject unknown entry keys so a typo (`lable`) cannot silently fall back to the default.

## Project status (issue #3, 2026-10-08)

- The issue named `gh project field-list` as the source of the ids, but `item-edit --project-id` needs the Project's node id, which only `gh project view` returns → read the real `gh` JSON (`--format json` against a live Project) before writing a fake, not the issue's sketch.

## proposal snapshots (issue #14, 2026-10-08)

- A command the agent runs from its own shell, routed through the hook entry point (`hook propose`) → it reads stdin like a hook and hangs on the pane's tty until the 30 s timeout; exempt agent-run events (`done`, `propose`) from the stdin read in both `events.hook` and `adhoc.hook`.
- A PreToolUse gate keyed on the tool payload alone (`tool_name` + `tool_input`) → PostToolUse carries the same payload, so the answered question's close was denied and its wait card stayed open; gate on the hook event (`notification` = PreToolUse here), not on the payload.

## actionable decisions (issue #17, 2026-10-08)

- Answering a Claude dialog by keystrokes (Escape + prompt) → fragile and interrupts work; a `PermissionRequest` hook runs in parallel with the pane's dialog and its `updatedInput.answers` answers an AskUserQuestion natively (verified live, also under `--permission-mode auto`), so answer through the hook and let whichever side answers first win.
- A `PermissionRequest` hook that exits 2 (argparse on an unknown subcommand, an older plugin) → Claude reads it as a deny; register it as `<cmd> permission || exit 0` so any failure leaves the dialog to the pane.
- `gh pr checks` (with or without `--required`) exits 1 with "no checks reported" on a repository without checks → a merge gate built on it refuses every merge there; gate on `gh pr view --json mergeStateStatus` and let `gh pr merge --match-head-commit` stay GitHub's authority.
- Switching a card to the `wake` delivery mode → Hermes sends no ping at all; make it conditional on muster being able to page the human itself (platform, bridge hook in the pane's settings, a fresh gateway heartbeat), or idle and old-pane waits go silent.
- A plain message to a Telegram group bot in privacy mode → never reaches the gateway (only replies to the bot, @mentions and commands do); after "Other", send a `ForceReply(selective=True)` that mentions the human, so their phone opens a reply bound to the question.
- PostToolUse's `tool_input` for an AskUserQuestion gains `answers` and other fields → a fingerprint of the whole input never matches the PermissionRequest's; fingerprint `questions` only.
- A kanban wake is an internal event and skips `pre_gateway_dispatch` → a plugin cannot swallow the woken agent's commentary there; skip the wake (no subscription when muster pages itself) and have the skill reply a bare `[SILENT]`, which Hermes drops on internal turns, when it has nothing new.
- A subagent's permission prompt (payload carries `agent_id`, `agent_type`) → Claude shows its dialog only after the PermissionRequest hook returns (2.1.295), so herdr never shows the pane blocked and a bridge that waits for `blocked` drops it; skip that check for subagents, and hold one only while the gateway is up.
- A muster pane runs `--permission-mode auto`, so a harmless command never prompts → to test prompts live, add a `permissions.ask` rule for one exact command to the throwaway worktree's ignored `.claude/settings.local.json` after launch; Claude reloads it live and the ask rule overrides auto mode.
- Herdr's `agent_status` is read off the screen, so a scrolled-back pane read `idle` while its dialog was still up, and the gateway staled a live request as "Answered in the pane". Claude sends the PermissionRequest hook SIGTERM when the pane answers a permission prompt first (2.1.295), so take that as the signal and never herdr's screen status.
- Trusting that SIGTERM for a pane-answered AskUserQuestion too → none came (2.1.302): the bridge's hook heartbeated for 5 h, the request stayed `open`, and every topic reopen asked it again (#42). The host's own evidence is the AskUserQuestion's PostToolUse/PostToolUseFailure, or the next UserPromptSubmit; `bridge.settle` ends the request from those.
- muster rebuilt Hermes surfaces it could have reused: heartbeat, lock, typed-answer parser, clarify bookkeeping and settle tracking. It also shipped permission prompts as numbered clarify buttons over JSON, because Hermes's approval card was "private". A simplicity review against Hermes's source cut them. Two cuts were reverted on purpose: the narrow tap/reply authorization (talking to the Hermes agent is not permission to approve) and the outcome edits (the human must see whether an answer arrived). Rule in AGENTS.md: approved surfaces first, unavoidable private use in `muster/hermes_private.py`, tracked upstream (#20).
- An "Allow session" button sent back as `updatedPermissions` (a session `addRules` for the exact Bash command) → Claude asked again for the identical command, with or without an ask rule. Claude's own "Yes, and always allow access to <dir>" did not stop the next `touch` there either (2.1.295). So the card offers only Allow once and Deny; never offer a scope Claude does not honour.

## outcome labels (issue #21, 2026-10-09)

- A ✓ label written right after the PermissionRequest hook prints its answer → it confirms the stdout write, not that Claude applied it (the pane can win, or Claude can exit, at the same moment); label what muster knows ("Sent to Claude") and keep ✓ for outcomes muster verified.

## run topics (issue #24, 2026-10-09)

- Hermes's Telegram sends retry a gone thread without it (to General), and `SendResult` does not say which thread a message landed in → muster cannot tell from a send that a topic was deleted; probe the topic itself (`reopen_forum_topic`: TOPIC_NOT_MODIFIED when open, "thread not found" when deleted), and never answer a muster message found outside its topic.
- A subscription with `--chat-type forum` → the coordinator's wake gets session `…:forum:<chat>:<thread>` while the human's replies in that topic are `…:group:…` (the adapter builds inbound sources with "group"); keep the configured chat_type and add only `--thread-id`.
- Presenting a request the moment its new topic exists, then running the repair that re-presents the run's open requests → the prompt was sent twice; hold presentation until the repair for that thread has run.

## labelled buttons (issue #22, 2026-10-09)

- Putting option labels on buttons cut to a phone-safe width → two labels sharing a long prefix (`… us-east-1` / `… us-west-2`) or a blank label give buttons the human cannot tell apart, which Hermes's numbered buttons never did; number the buttons (and their body lines) whenever plain labels would collide or be blank.
- A topic name built from the readable title alone (`app#24 Fix it`) → two runs with the same title match the same topic-created notice and can adopt each other's topic; end every name with the ledger id and ignore a notice that matches more than one run.
- Hermes's `create_handoff_thread` returns None for every failure, including a timeout after Telegram made the topic → a retry on None can make a second topic; call create once per destination and, without the notice, fall back rather than retry.
- A repair that writes "done" after a loop that logs and skips failed card subscriptions → the coordinator's wake stays on the dead topic with no retry; keep the unmoved cards, retry them, and mark done only when notify-list shows each one at the new destination and not the old.

- Hashing the UserPromptSubmit `prompt` as delivery evidence → Claude Code hands a long paste to the hook wrapped as `\n\n<pasted_content id="…">\n…\n</pasted_content id="…">\n`, so a delivered send-back never matched and muster offered retries while Claude worked on it; record the hash of the text inside that exact wrapper too, and test with a fake that wraps as Claude does.

## retired run topics (issue #28, 2026-10-10)

- A topic retired only by the step that removes the worktree (cleanup's `conversation.finish`) → a ledger archived by hand, or a workspace removed by hand, left its topic `open`, and the deleted-topic probe made a new one when the human deleted it; gate every create and reopen on a fresh board read of the ledger (`archived` → delete, never recreate), and write `closed`/`deleted` only after Telegram confirms the call.
- Treating "no such task" from `hermes kanban show` as archived → a gateway pointed at the wrong board (or HERMES_HOME) would read every run as gone and delete every topic; delete only on an explicit `archived`.

## duplicate error reports (issue #34, 2026-10-16)

- Hermes's kanban `create --idempotency-key` returns only a non-archived card (kanban_db.py:1201) → an archived card's issue, still open and labeled, gets a fresh card on the next tick; record a resolved approval event (`held.json` closed, `launch.json` retired) and skip it in intake, so only a new label event re-authorizes.
- A first design keyed duplicates on arrival time (60 min window) → it holds every unrelated report in the window and still cannot tell one incident from two; the human redirected to a cheap model judgment through Hermes's own `ctx.llm` + `register_auxiliary_task`, with the live run's agent making the final call.
- Blocking a held card `--kind needs_input`, then unblocking it on release → the run's own later `needs_input` block sends the card to triage (same-kind re-block); hold with a generic block.
- A herdr prompt to an idle agent with a muster wait card open → the prompt hook takes it as the human's answer and closes the wait; deliver only when no `muster-wait` marker and no queued event exist in that run.

## finished runs with a vanished worktree (issue #33, 2026-10-16)

- Cleanup found runs only through worktrees that still exist, and retired a topic only after a removal it made itself → a `done` run whose checkout vanished another way (#480) was never seen again, and the gateway's probe kept its topic open forever; reconcile from the durable records (conversation.json, launch.json, run.json) by path, not only from herdr's listing.
- Wrapping an existing locked helper (`retire` takes `launcher.launch_lock`, non-blocking per open file) inside the same lock → the run refuses itself as "busy"; split the body out so one lock covers the recheck and the write.

## design review (issue #36, 2026-10-16)

- A side effect riding on an event the agent waits for (the coordinator's wake inside the `proposal` delivery) → its failure kept the version from arming, and as a queued entry it would stop the queue and switch off `reconcile`'s backstops; give it its own outbox event that `drain`, `reconcile` and cleanup's owner check step past (`runs.owned`).
- A one-tap action whose payload another agent wrote (Send back's revisions) → the human would send words they never saw, labelled as theirs; put the payload on the message itself, capped so the cut never reaches it.

## topic lifecycle (t_b9615602, 2026-10-16)

- A conversation closed only as a side effect of deleting a resource (`cleanup.remove` -> `conversation.finish`) → every resource blocker (dirty tree, child process, ownership mismatch, stale request) kept a finished run's topic open; close on the work's own end (the merge) and let resource checks gate only the removal.
- A "liveness probe" built from a mutating call (`reopen_forum_topic` on an in-memory timer) → it undid every human close and fired again on each restart; with no read API, detect a gone topic from evidence a send already leaves (a prompt that fell back to General), never by changing the thing checked.

## channels design (issue #40, 2026-10-17)

- The Hermes docs say plugin platform factories run before core handlers, and the 2026-10-08 decision planned a Slack guard on that → in the Slack adapter source, core `hermes_clarify_*`/`hermes_approve_*` listeners are registered first and Bolt runs only the first match, so a plugin cannot narrow who answers Hermes's own Slack prompts; muster's Slack prompts use muster-owned action ids, and claims about precedence are checked in the adapter source, not the docs.
- A "durable prompts" skip rule written as "every question has a saved message" → a permission request has no questions, so it read as fully shown and would never be presented; define the slots per request kind.

## channels implementation (issue #43, 2026-10-17)

- A test that patches the conversation clock (`shared.now`) for `closed_at` while requests stamp `created_at` with the real `time.time()` → every request looks newer than any close, so an "older than the close never reopens" test passed through the reopen path; set the patched clock from `time.time()` where both timestamps meet.
- A follow-up step after a terminal state (unsubscribing a `deleted` run's cards), gated on a new field → every older record lacks the field; gate on `unsubscribed is False`, not falsiness, or each old deleted reference would be stepped again.

## run progress (issue #46, 2026-10-17)

- Ordering "review-ready" against the newest tool event → the agent's own `hook done` runs in a Bash tool whose PostToolUse lands after it, so the run would never read ready; order it against the next human prompt (a send-back), not tool events.
- A "never repost" guard written as `saved.get("lost_thread") == thread` → with no topics the thread is `None`, so every main-chat run matched and was never posted (caught by the plan attack); compare a stored `{"thread": …}` record, never a bare value that may be None.

## undoable cleanup (t_b8907f9d, 2026-10-18)

- Backing up a checkout with `git stash push -u` → it rewrites the working tree and the stash stack every worktree shares; build the same three-parent commit with `commit-tree` from trees written through temporary `GIT_INDEX_FILE`s, so `git stash apply --index` restores it and nothing live is touched.
- `git bundle create` given a bare commit id → it silently leaves that commit out; name it with a ref (deleted right after), and verify by fetching the bundle into an empty repository, never by `bundle verify` alone.
- `git init` on an existing directory → succeeds and reuses it; a restore command that must not overwrite starts with `mkdir`.
- A file flagged assume-unchanged or skip-worktree → `git status`, `git add -u` and `git worktree remove`'s dirty check all skip its edits, so a backup built from them verifies yet misses them; read the flags with `git ls-files -v` (lowercase tag or `S`) and keep the checkout (issue #49).

## human collaboration UX (2026-10-18)

- Formatting Slack text by running agent text through Hermes's `adapter.format_message` alone → it escapes only broadcast mentions, so a raw `<@U…>` in agent text would still ping; escape with `shared.slack_text` first, then convert: `format_message` keeps existing entities and still turns `**bold**`, headings and `[x](url)` into mrkdwn.
- Writing Markdown into a Telegram clarify prompt → muster's clarify path (`send_labelled_clarify`, like Hermes's `send_clarify`) HTML-escapes the text, so `**` shows literally; Telegram cards get plain text (`shared.plain`), and only a separate `adapter.send` message (the full review) gets Hermes's MarkdownV2 conversion.
- Using a stored outcome string as a state marker ("Sent ✓" picked out delivered send-backs) → rewording it for humans would have silently broken re-review; match outcomes through a constant that also accepts the old string (`decisions.SENT`).
- Telegram's re-presentation after a restart cleared `partial`, so a human's answer to one question of several was lost on every gateway restart; keep the parts and present only the unanswered questions.
- Giving an early design tap to the version's question only when one was open at scan time → a question asked and answered in the pane between two scans was never seen, so a later ask for the same version got the old tap; reconcile ended questions first, even when none is open.

## clarify-code-organization (2026-10-19)

- Moving functions between modules keyed by bare name → `launch`, `recover`, `relaunch`, `note` and `log` exist in both core and runs, so a name-keyed map rewrites intake's own `launch()` calls to `adhoc.launch`; key every move by (source module, name), and qualify only references that resolve to a module global (symtable), never locals that share the name (`run`, `plan`).

## clear-decision-messages (2026-10-19)

- Making a tap act at once (one-tap send-back) → requests already on screen were created under the old rules (their card never showed the feedback), so the new fast path would have sent text the human never saw; gate the new behavior on a field only new requests carry (`hints`) and keep the old confirmation for the rest.
- A gateway test whose tap now reaches `decisions.run_of` or `obsolete` → it runs the real `hermes kanban` / `gh` CLI, which is slow enough to time out a 2 s wait; stub both in any test that drives a send-back through the gateway.

## slack-working-and-review-order (2026-10-20)

- A coordinator review woken by a kanban card in a Slack thread → Hermes posts its tool-activity bubble at the coordinator's first tool call, so Muster's finished review always lands below it (Slack orders replies by post time), and `display.platforms.slack.cleanup_progress` likely never fires for these turns (an authority-admitted wake replies through `session_ingress.deliver_response`, which skips the post-delivery callbacks); lead to the review from the thread parent instead of trying to reorder replies.
- Adding a field to the Slack thread parent that appears after the status line was edited (the review's permalink) → `gateway.scan` runs `progress_pass` before it presents requests and `progress.due` only re-edits on new text, so the parent would never show it; refresh the parent where the field is saved.

## slack-acceptance-design (2026-10-20)

- Muster's tests used `fake_slack` plus `fake_hermes.Subs`, which records subscriptions but posts no ping → Hermes-owned messages (the notify+wake "🔍 is reviewing" ping, the coordinator's tool-progress bubble) existed in no test, so a stale review line and a leftover bubble passed every suite. Test the thread with Hermes's real gateway and Slack adapter against its own stand-in, and assert the exact visible set of messages.
- An isolation stub for `herdr` or `gh` placed earlier on PATH → `core.prepare_env` prepends `$HOME/.local/bin:/opt/homebrew/bin`, where the real binaries live, and the production `hermes` launcher runs `python3 -I`, which drops `PYTHONPATH` and so any sitecustomize shim. Intercept at `core.run`, and put the wrappers in the test `$HOME/.local/bin`.

## human-directed-agent-messages (2026-10-20)

- A blanket "never prompt any agent" in a skill, written to keep review feedback behind the human's tap → the coordinator read it as "no mid-run delivery exists" and refused a message the human asked for; scope a prohibition to the thing it guards (its own feedback, approvals) and name the allowed path.
- zsh's `echo` turns a JSON `\n` escape back into a newline → check single-line encoded text with `printf '%s'`, not `echo`.

## markdown-decision-attachments (2026-10-20)

- Trusting Hermes's public `adapter.send_document` result → on a failed upload both the Slack and Telegram adapters post a "couldn't send file" notice and return that notice's `success=True`, so a caller would claim a file the human never got; Slack uploads through Bolt's `files_upload_v2` (it raises), Telegram through `hermes_private.send_document` with an `on_error` that returns the failure.
- Expecting a permalink from `files_upload_v2` → `files.completeUploadExternal` answers only `{id, title}` per file; ask `files_info` for the permalink, and say "the files in this thread" without it.
- Recording only which files a Telegram card got → a deleted topic remade by the repair re-presents the card in the new topic, which then said "the Markdown files above" over none; record the topic with the files and send them again when the card's topic differs.

## slack-acceptance (2026-10-20)

- Hermes's e2e harness gives the child gateway `HOME=<tmp>` and `HERMES_HOME=<tmp>/.hermes` → Hermes takes that for the native default home, gives it the bare `ai.hermes.gateway` label, finds the real account's plist (it reads `pwd`, not `HOME`) and refuses to start while production's gateway runs; give the child its own `HOME` apart from the gateway home.
- A dead-port `HTTPS_PROXY` with loopback in `NO_PROXY` → Hermes's Slack adapter sets the proxy on its clients explicitly, even for a 127.0.0.1 stand-in, and skips it only when `NO_PROXY` names `slack.com`; list Slack's hosts there and lean on the stand-in's fake token for Slack egress.
- The "slack-working-and-review-order" guess that `cleanup_progress` never fires for wake turns → wrong: against a real gateway the coordinator's bubble is posted and deleted within a second of its turn; the stale reply that stays is the notifier's "🔍 … is reviewing" ping, on both the design and the PR path.
- A scripted builder that only proposes → Muster's card ends "the builder hasn't asked yet"; a real builder then asks with AskUserQuestion, and its held `PermissionRequest` hook is what receives the approval, so drive both hooks. Likewise set the fake pane idle after a `Stop` hook, or a send-back is refused as "the builder was busy".

## stale-review-notices (2026-10-20)

- A card subscribed `notify+wake` → Hermes's notifier posts its completion as a thread reply that Muster never gets an id for, so it can never be retired. For a notice the status message already carries, subscribe `wake` (Hermes still wakes the coordinator with the summary as handoff), and switch a long-lived card's mode in place with `notify-subscribe` naming only its row key and `--delivery-mode` (last-write-wins).

## activation-after-merge (2026-10-21)

- A prompt edit committed straight to main without `MUSTER_REGOLD=1` (1296583, c459877) → main carried two failing tests (`test_brief_keeps_forbidden_list`, the prompt goldens) until the next prompt change; regenerate the goldens and fix the needles in the same commit as any prompt edit.
- A side step after an irreversible action caught only the exceptions its author listed, while `core.subscribe` raises `core.LaunchError` → a merge that landed would have been recorded as failed; after the irreversible part, guard a follow-on step with `except Exception`, log it (and the log write itself), and let a later pass retry it.

## agent-owned-setup (2026-10-22)

- A 308 s `herdr worktree create` (radicalcandorai#518) looked like slow dependency installs → 300 s of it was one `npm audit` bulk request that hung until npm's default 300 s `fetch-timeout`, inside the repo's synchronous post-checkout bootstrap; checkout, LFS, `uv sync` and both installs took ~8 s. Time a stall from npm's own debug logs (`~/.npm/_logs`: each file's birth is an install's start and its mtime its end; `verbose audit error` names the call) and from file birth times in the worktree, before redesigning the launch path.

## configurable-operating-profile (2026-10-22)

- Treating "which profile muster runs under" as a value muster could route to → Hermes loads each plugin, its `config.yaml` settings and its `plugin_data_dir` per profile home (`hermes_cli/plugins.py` `_plugin_managers_by_home`), so the only safe setting confirms the loading profile; naming another would split config from state. The plugin settings form also has only static `choices`, so a profile picker is a text field. Name the profile from the home (`get_active_profile_name`), as the gateway names itself to its notifier, not `current_profile_name`, which an env pin can override.
- `core.hermes_home()` now returns the served profile's home under a multiplexed gateway → anything read from it (the Telegram DM fallback's `TELEGRAM_HOME_CHANNEL` in `.env`) is that profile's, not the host's; a test that overrides the home must give that home its own `.env` or a set `notify_chat_id`.

## profile-safe-test-setup (2026-10-22)

- A test sandbox that replaced only `HERMES_HOME`/`HERMES_KANBAN_HOME` → run inside a Hermes agent (Bean: `HERMES_PROFILE=bean`), Hermes's `current_profile_name` took the inherited pin over the sandbox home and 72 subscription read-backs failed; a board pin (`HERMES_KANBAN_DB`) could likewise reach the live board once Hermes imports. The sandbox now drops every inherited `HERMES_*`; a test that wants another profile sets its own pin.
- Hermes's dev `.venv` editable install maps root modules statically, so a root module added after the install (`hermes_yaml`) is unimportable there while production works (its launcher puts the checkout root on `sys.path`) → conftest appends that root. `hermes --run-module pytest` is not a test runner: with a temporary `HERMES_HOME` it builds a ~1 GB dependency environment per data root, which `tests/test_kanban_contract.py`'s real-CLI fixture also triggers on every test.
