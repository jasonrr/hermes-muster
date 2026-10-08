# Design v2: actionable channel decisions (issue #17)

## Problem

A muster ping says "Reply in Herdr pane w1S:p2". Herdr has no deep link and does not show that id, so
every decision needs the human at the machine. Pings drop the options an agent asked, and a finished
build gets "review X" instead of a recommendation. After a send-back, a revised build never pings again
because `done` on a completed ledger does nothing (PR #18).

## Verified platform facts (2026-10-08)

Claude Code 2.1.294, live in a throwaway tmux session:
- A `PermissionRequest` hook runs **in parallel** with the pane dialog; whichever answers first wins. A
  decision returned 25 s later still resolved the open dialog. A pane answer first: the tool proceeds and
  the hook keeps running to its own end (not killed).
- For AskUserQuestion, the hook returning `{"behavior":"allow","updatedInput":{...tool_input,"answers":{<question>:<text>}}}`
  answers natively: the agent received "→ Blue", and free text ("Teal, actually") arrived verbatim. This
  works under `--permission-mode auto`, which muster panes use.
- For a permission prompt, `allow`, or `deny` with a `message` to the agent, both work.
- Timeout: default 600 s, no documented maximum. On timeout the hook gives no decision and the pane dialog
  stays (from the docs; my live probe of this was blocked).

Hermes 0.21.5 (source read):
- **Approval service** (`tools/approval*.py`): in-memory, tied to a live Hermes agent turn. Requests are
  enqueued only through a private function (`_await_gateway_decision`). It has no external ingress and is
  lost on restart. **Not reusable without private internals.**
- **Clarify service**: `tools/clarify_gateway.register / wait_for_response / resolve_gateway_clarify`
  (public module functions) and `adapter.send_clarify(chat_id, question, choices, clarify_id, session_key)`.
  Telegram, Slack, Discord, WhatsApp and Google Chat implement it, with native buttons and an "Other"
  free-text button. It is in-memory, and an old button after a restart gets Hermes's own "expired" toast.
  **Reusable as is.**
- Plugin hooks: `register_telegram_handler(factory)` gets `(Application, adapter)` at connect, and
  `pre_gateway_dispatch` sees every inbound message, including `reply_to_message_id`, and can swallow it.
- Clarify's typed answer ("Other") is matched to the Hermes chat session, not to our request, so we route
  typed answers ourselves (step 3).

## Approach

1. **Decision request (transport-neutral core, `muster/decisions.py`).** One JSON file per request,
   `<data dir>/decisions/<id>.json`, with a per-request file lock. Fields: random id; kind (`question` |
   `permission` | `build` | `feedback`); ledger and wait card; provenance (repo, issue or branch, pane);
   proposal version (#14) or reviewed PR head sha; questions and options exactly as the agent wrote them;
   status `open → answered → done | failed | stale`; the answer; the presented message ids; and an audit
   list (time, step, result). Every transition reads the current status first. A second click, a replay or
   a restart on a non-`open` request changes nothing and reports the recorded outcome.
2. **Pane side: a Claude `PermissionRequest` hook** (new, the bridge) for AskUserQuestion and permission
   prompts. It writes an `open` request, then polls the file and returns:
   - AskUserQuestion: `allow` plus `updatedInput.answers`, the exact label or the typed text.
   - Permission prompt: Allow once → `allow`; Deny or a typed reply → `deny` with that text as `message`.
   - Stale or timed out (24 h): no decision, so the pane dialog decides, as today.

   Existing wait cards stay; their body names the request id. An answer in the pane (the existing
   PostToolUse/prompt hook closes the wait) marks the request `stale`, and the waiting hook exits with no
   decision.
3. **Gateway side, inside the Hermes gateway (`__init__.py` registers it).**
   - `register_telegram_handler` captures the adapter and the event loop. A small task scans
     `decisions/` every 2 s. Each open request not yet presented in this gateway process (so after a
     restart too) is presented with `clarify_gateway.register` + `adapter.send_clarify`, one per question.
     It has a synthetic session `muster:<id>` and clarify id `mu<id>:<n>`. The ping text carries the
     question, the agent's context, every option with its description, the proposal heading, and
     provenance. A worker thread waits on `wait_for_response` and records the answer.
   - Authorization: a PTB handler in an earlier group, pattern `^cl:mu`, rejects any clicker who is not
     `notify_user_id`, and any chat that is not the notify chat, before the core clarify handler sees the
     click. This is on top of Hermes's own allowlist. It forks no button code.
   - Typed answers: a `pre_gateway_dispatch` hook. From the authorized human in the notify chat, a message
     that replies to one of a request's presented messages, or that follows the "Other" button on one, is
     resolved into that clarify and swallowed (`skip`). Any other message passes to the Hermes agent
     untouched. We never infer an answer from unrelated chat.
   - After the answer lands, the request message is edited to "Delivered ✓" once the hook reads it, or
     "Failed: <why>; answer in the pane".
4. **Builds.**
   - `done` still completes the ledger and pings "Ready for review". That ping is status, not a decision;
     it stays because the recommendation comes from a separate agent turn that can fail.
   - The escalation skill turns the woken Hermes agent into the coordinator. It reads the diff,
     surrounding code, `gh pr checks` and PR state, and separates what it verified from what the agent
     claimed. Then it runs `hermes muster recommend <ledger> --pr <url> --head <sha>
     --choice merge|send-back|nothing --review <file> [--feedback <file>]`. The review states the deploy
     consequence from the repo's production note, or "deploy on merge: unknown".
   - The `build` request is presented with three choices, the recommended one marked. Its handler, run in
     the gateway worker thread:
     - **Do nothing**: recorded, and the message is edited to "No action. PR open, not merged."
     - **Merge**: revalidate first: PR open, head == the reviewed sha, base unchanged, required checks
       green (`gh pr checks --required`). Then `gh pr merge --squash --match-head-commit <sha>`. Then read
       back `MERGED` before saying so. Merge never implies deploy.
     - **Send back**: a `feedback` request presents the proposed feedback, with "Send as written" or a typed
       edit. It is delivered to the existing pane with `herdr agent prompt` only if herdr shows the agent
       idle, and verified by the UserPromptSubmit sha (`core.prompt_seen`, existing). Otherwise it fails
       honestly and stays retryable. It makes no new run or card, and records review cycle n+1.
5. **Re-review.** When `hook done` (issue runs) or the ad-hoc `stop`/verify path finds the ledger already
   done with a send-back recorded, it verifies first: PR head == pushed worktree HEAD, and the tree is
   clean (`runs.verify`). Then it opens a review card with idempotency key `review:<ledger>:<head sha>`,
   subscribed notify+wake and completed "Revised, ready for re-review: <url> at <sha7>". That wakes the
   coordinator to recommend against the new head. A new head makes every older build request `stale`; its
   handler answers "stale: PR moved to <sha7>". Original completion and history are kept; the ledger is
   not reopened.
6. **Slack** (contract only, not built): `send_clarify` and `pre_gateway_dispatch` are already
   platform-neutral. A Slack adapter adds the authorization guard (`register_slack_action_handler`
   scoped to our ids) and the chat or user ids from config. Documented in `docs/decisions/`.
7. **Skills**: escalation and run may now prepare and recommend. They never merge, send back or answer
   themselves; only the human's click executes.

## Guarantees

All status changes happen under the request's file lock and read the status first. "Reported" below
means the Telegram message is edited, the audit list gets an entry, and the log gets a line.

### Races
- **Pane vs. channel (questions).** Claude takes whichever answer comes first. Evidence of what happened
  is the PostToolUse payload, whose `tool_response` carries the answers Claude actually used:
  - It matches the channel answer: the request is reported "Delivered ✓".
  - It does not match: the request is reported "Answered in the pane: <answer>".
  - A channel answer is never reported as delivered just because the hook returned it.
- **Pane vs. channel (permission prompts).**
  - The tool ran after a channel Deny: reported "Allowed in the pane".
  - The tool was denied after a channel Allow: reported "Denied in the pane".
  - Otherwise: reported as the channel decision.
- **Double click or replay.** Clarify resolves an entry once. A later click finds the request not `open`
  and is answered with the recorded outcome. Nothing runs twice.
- **Click vs. hook deadline.** The hook takes the lock at its deadline:
  - If the request is still `open`, it marks it `stale` ("expired") and exits with no decision.
  - If it is already `answered`, it returns the answer.

  A click after that gets "expired; answer in the pane".
- **Merge vs. new commit.** `--match-head-commit <reviewed sha>` makes GitHub refuse the merge if the head
  moved between our check and the merge, so the check-then-merge window is closed by GitHub, not by us.
- **Concurrent requests.** Each has its own messages, and replies bind by stored message id. A typed
  message that replies to nothing goes to the "Other" request only if exactly one request is waiting for
  text. Otherwise it passes through to the Hermes agent, and we never guess.

### Restarts
- **Gateway restart.** Clarify entries are in-memory and lost. The scan task re-presents every `open`
  request as new messages, and the old messages are edited to "Superseded: see the newer message". Every
  presented message id is persisted, so a typed reply to an old message still binds. An old button gets
  Hermes's own "expired" toast. The pane hook is a separate process and keeps waiting through the restart.
- **Hook process gone** (pane closed, agent exited, machine rebooted). The request records the hook's pid
  and its start time. When the scan finds that process gone, it marks the request `stale` and reports
  "The agent is no longer waiting".
- **Hermes or machine down when a request is made.** The request file is written before anything is sent,
  so the first scan after start-up presents it. The hook waits regardless.

### Execution recovery (build actions)
- Before any effect, the request records `executing` and the intent: the action, the reviewed sha, and
  for a send-back, the sha256 of the prompt.
- **After a crash, nothing consequential runs again by itself.** On start-up the scan finds the
  `executing` request and reads back the world:
  - **Merge.** If `gh pr view` shows MERGED with that head, it is `done` and reported "Merged". Otherwise
    it is `failed` ("interrupted; not merged"), and a fresh build request is presented after
    revalidation. You click again.
  - **Send-back.** If the UserPromptSubmit evidence has the prompt's sha, it is `done`. Otherwise it is
    `failed` ("not delivered"), with a Retry choice. Before resending, a retry checks that evidence again,
    so the feedback can never be delivered twice.
- A failed effect is never reported as success. A merge counts only once `gh` reads back MERGED, and a
  send-back only once its sha is seen.

### Permission timeout
- The hook is registered with timeout 86400 s (24 h). Its own deadline is 86340 s, so it decides before
  Claude kills it.
- **Any failure means no decision, so the pane dialog decides. Muster never allows on error.** Failures
  include a timeout, an unreadable request, a missing gateway, and a bug that crashes the hook.
- A channel Allow is "allow once" (no `updatedPermissions`). Deny carries your typed text as `message` to
  the agent.
- Each open question holds one sleeping hook process that polls once a second. Requests older than the
  deadline are reported "Expired after 24 h; answer in the pane".

## Safety boundaries
- Only `notify_user_id`, in the notify chat, can act; any other clicker is told "not authorized" and logged.
- Every effect is revalidated before it runs, and read back before it is reported.
- Request ids are random. The UI payloads carry no credentials, and muster never reads the bot token.
- No new dependency. No change to `.github/`, CI, lockfiles or agent-instruction files.

## Cut (my additions only)
- Our own Telegram buttons and Bot API client: we use Hermes clarify + `send_clarify`.
- Hermes's exec-approval service: it needs private internals; clarify covers allow/deny.
- Escape plus a typed prompt to answer dialogs: replaced by the native hook answer.

## Test plan
Fakes: a fake adapter (`send_clarify`, edits) and `clarify_gateway` driven directly, plus the existing
fake herdr and gh. Cases:
- Unauthorized clicker, wrong chat, a reply to a non-muster message, ambiguous plain text (passes
  through).
- Two concurrent requests.
- Answered in the pane first (stale), archived ledger.
- A new PR commit after the recommendation, a failing required check.
- Double click and replay, gateway restart (re-present; the hook keeps waiting).
- Hook timeout, delivery failure, merge read-back failure, verified success.
- Re-review on issue and ad-hoc runs, two send-back cycles, repeated `done` on one head.

## Trade-offs needing your yes (complexity budget)
- A persisted request with a status, and muster code running inside the gateway process (a 2 s scan
  task, a worker thread per open request). A gateway restart is needed on upgrade.
- muster holds merge authority behind your click.
- One blocking hook process per open question, up to 24 h.
- It depends on Hermes's clarify module functions, which are public names but not a documented plugin
  API, so a Hermes upgrade could break them; tests pin the calls.

## Answered
- Ping: muster's actionable message replaces the plain ping for questions and permission prompts (wait
  cards subscribe `wake` only).
- Merge method: squash.
- Permission prompts: bridged through the Claude `PermissionRequest` hook, presented with Hermes clarify.

- Production: the gateway runs under launchd, and a restart is allowed. Tests use fakes. A live
  end-to-end check needs the gateway to load this branch's plugin. I will check where the installed plugin
  points first, and ask before installing the branch or restarting.
