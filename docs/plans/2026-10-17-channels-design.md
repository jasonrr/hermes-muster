# Design: channels — Telegram and Slack as peers (issue #40)

Status: v2 approved by Jason 2026-10-17 (Proposal v2 efb6803da851, ledger t_9b5b2aa0). v1 (Proposal v1 c11b93522bce, ledger t_9b5b2aa0) was approved by Jason 2026-10-17 with
D1 (a), D2 one channel per deployment and one workspace, D3 a new implementation issue; he then asked for two
revisions, both in v2: partial answers survive a Slack restart (§9), and every button in a Slack block has its
own action id (§7). Architecture unchanged. Nothing here is implemented; the plan is `2026-10-17-channels.md`. Sources: muster at e544f9c; Hermes source
`~/.hermes/hermes-agent` at 2034867d (`v0.22.2+canary.20261017`, slack_bolt 1.30.0), plus the Hermes docs
(llms.txt). Line numbers are evidence for this commit only; the plan cites symbols.

## 1. Problem

muster's decision core (`decisions.py`, `bridge.py`) is already platform-neutral (decision 2026-10-08). The
gateway is not. `gateway.py` mixes the neutral scan (present, wait, answer, edit to outcome) with Telegram
specifics: PTB guards, `cl:mu`/`ea:` callback data, ForceReply, and the forum-topic lifecycle. Telegram is also
assumed in `core.notify_target` (TELEGRAM_HOME_CHANNEL DM fallback), `events.muster_pages`
(`notify_platform != "telegram"`), `gateway._dispatch` (`platform != "telegram"`) and `config.require`
(topics only on Telegram). Adding Slack there means `if telegram … else …` paths in every one.

Words used here:
- **channel**: a muster communication backend (`telegram`, `slack`). Code name: `channel`.
- **chat**: a Telegram chat, or a Slack channel (`C…`). Stored as `chat_id`.
- **conversation**: the place one run's messages go: a Telegram forum topic, or a Slack thread.
  One per ledger. Stored in `conversation.json`.

## 2. Non-goals

- More than one channel at once in one deployment. One `notify_platform` per muster. State already records
  each conversation's platform, so this can come later without migration.
- More than one Slack workspace. One team id, checked on every inbound action.
- Deleting Slack messages or threads. Slack retention is the workspace's.
- Roles beyond one approver set (see §6).
- A transport framework. The contract has exactly the callers listed in §4.
- The isolation/profile work (§10). Implementation itself, live Slack sends, app registration.

## 3. The finding that shapes Slack

On Telegram, muster puts guards in PTB group -1, ahead of Hermes's handlers, so only the notify user can tap a
muster prompt. **Slack cannot do that.** The Slack adapter registers its own `hermes_clarify_choice_N`,
`hermes_clarify_other` and `hermes_approve_*` listeners *before* plugin ones (`plugins/platforms/slack/adapter.py`
`_register_plugin_action_handlers` is called after the core `app.action(...)` calls, :1686-1701), and Bolt runs
only the first matching listener. The Hermes docs say plugin factories go first; the source says otherwise.
Those core handlers authorize with Hermes's allowlist (`_is_interactive_user_authorized`, private), which is
"may talk to the agent", not "may approve this merge".

So on Slack, muster does **not** use `send_clarify` or `send_exec_approval`. It posts its own Block Kit message
with **muster-owned action ids** (`muster_choice_<i>`, `muster_text`, `muster_multi`, `muster_submit`,
`muster_allow`, `muster_deny`, `muster_deny_text`; §7), registered with
the public `ctx.register_slack_action_handler`, and authorizes every action itself. This is still the approved
surface (a documented plugin API); it is Hermes's clarify/approval *state machines* we skip, because their
authorization cannot be narrowed. Upstream ask (to draft, not file): a plugin-first or namespace filter for
Slack interactive actions, or an `authorize` hook on `send_clarify`/`send_exec_approval`.

A side effect makes Slack simpler than Telegram: muster's handler reads the durable request file, so a button
keeps working across a gateway restart. Slack needs no re-presentation on boot, no Superseded edits, no waiter
threads. That holds only if every answer muster has taken is durable too, including the answers to some but not
all questions of a request (§9).

The alternative, Bolt `app.middleware` in the platform factory to veto core handlers, is untested, global to
every Slack event, and would still route answers through Hermes's in-memory clarify state that dies on restart.
Rejected.

## 4. Architecture

```
muster/gateway.py        channel-neutral: scan loop, request status, stale/alive, backoff, answered(),
                         compose(), on_answered(), settle edits, on_dispatch -> channel.reply()
muster/channels/__init__.py   NAMES = ("telegram", "slack"); get(name) -> module; unknown name -> ConfigError
muster/channels/telegram.py   today's Telegram code moved out of gateway.py: factory, guards, labelled clarify,
                              approval card + Hermes wait, ForceReply, typed replies, forum-topic lifecycle
muster/channels/slack.py      factory (captures adapter + app.client), Block Kit prompts, action handler,
                              thread parent lifecycle
muster/hermes_private.py      unchanged; Telegram-only private names stay here (Slack adds none)
```

Dependency direction: `gateway -> channels.<name> -> {decisions, conversation, core}`. A channel module never
imports `gateway`; it reports an answer through a callback the gateway passes in (`on_answer(rid, n, value)`)
— the same seam `gateway.answered` is today. `bridge.py` and the pane side do not change. `decisions.py` gains
one function, `answer_part` (§9); its statuses and transitions do not change.

The contract, as a `typing.Protocol` in `channels/__init__.py` (documentation and the contract tests' target;
no base class):

```python
class Channel(Protocol):
    name: str                      # "telegram" | "slack": equals Hermes's platform name
    durable_prompts: bool          # True: a sent prompt stays answerable across restarts (slack)
    def register(ctx, on_answer) -> None
        # wire Hermes: telegram -> register_telegram_handler; slack -> register_platform_handler("slack")
        # + register_slack_action_handler for the muster_* ids
    def validate(settings) -> list[str]      # names of missing/invalid settings; config.require joins them
    def ready() -> bool                      # adapter captured (factory ran)
    async def destination(req) -> tuple[bool, dict | None]   # (ready, send metadata): lifecycle-aware
    async def present(req, meta) -> Sent     # all questions of a question/build/feedback/design request
    async def present_approval(req, meta) -> Sent
    async def settle(req) -> bool            # edit every message of an ended request to req["outcome"]
    async def release(rid) -> None           # drop in-process waits (telegram); no-op (slack)
    async def reply(event) -> dict | None    # pre_gateway_dispatch: {"action": "skip"} when muster took it
    async def step(ref) -> None              # one lifecycle step of a conversation (create/close/retire)

Sent = {"messages": {str(n): [message ids]}, "failure": str | None}
```

`gateway.CHANNEL = channels.get(config.settings["notify_platform"])`, chosen once at `register()` by name, never
by "not Telegram". `register()` registers only the selected channel's Hermes surfaces, so a Slack deployment
never installs Telegram guards (and the reverse).

What each piece of today's `gateway.py` becomes:

| today | goes to |
|---|---|
| `scan`, `handle`, ALIVE/stale, `retry`/backoff, `vetted`, `answered`, `shape`, `compose`, `on_answered`, `background`, `record` | gateway (neutral) |
| `presented()` bookkeeping (boot, earlier messages) | gateway; Superseded edits only when `not durable_prompts` |
| `authorized`, guards, `ask_for_text`, `present`, `present_approval`, `waiter`, `approval_waiter`, `release`, `_dispatch` body, `in_topic`, every topic function | `channels/telegram.py`, moved verbatim |
| `render`, `buttons`, `title` | gateway (shared text); each channel applies its own caps (Telegram 32/3500, Slack 75/3000) |

Alternatives considered: (B) keep one gateway and branch on platform: rejected, it is the `if telegram … else`
shape the issue rules out. (C) an abstract base class with shared lifecycle code: rejected, the two lifecycles
share state names and guards but no steps (§8), so a base class would hold only `pass`. Modules behind one
Protocol, picked by name, are the smallest shape that makes both explicit.

## 5. Identity and routing

| | Telegram | Slack |
|---|---|---|
| workspace | — | team id `T…` (`notify_workspace_id`), checked on every action |
| chat | `notify_chat_id` (group or DM fallback) | `notify_chat_id` = channel `C…`; required, no DM fallback |
| conversation | forum topic `message_thread_id` | parent message `ts` (thread_ts) |
| user | `notify_user_id` | member `U…`, from `notify_user_id` (see §6) |
| prompt id | `message_id` (unique per chat) | `ts` (unique per channel) |
| question correlation | `cl:mu<rid>q<n>`, reply-to message id | `block_id` `mu:<rid>:<n>` on each prompt + action `value` |

- The existing generic keys (`notify_platform`, `notify_chat_id`, `notify_user_id`, `notify_chat_type`,
  `notify_topics`) cover Slack. One new key: `notify_workspace_id`, required when `notify_platform: slack`.
  No `notify_slack_*` parallel settings (the 2026-10-08 contract proposed them; this replaces that).
- `conversation.json` gains `workspace` (Slack only). `thread_id` holds Slack's parent ts. `presented.messages`
  keeps message ids per question; they are scoped by the run's conversation (one chat per deployment), so ts
  collisions across channels cannot occur in the first release.
- Concurrent runs: each run has its own thread. Each prompt is its own message whose `block_id` carries
  `rid` and `n`, so two questions in one thread, or two runs, never share an answer target. **A thread id alone
  never identifies a question** and muster never uses it to.
- Coordinator notify+wake: `core.subscribe` already passes `--thread-id`. Hermes's notifier sends into the thread
  (`metadata["thread_id"]` → `thread_ts`) and wakes session `agent:main:slack:group:<team>:<chan>:<thread_ts>`,
  the same key the human's thread replies get, provided `--chat-type group` (Hermes defaults new subscriptions to
  `dm`; muster already passes the configured `notify_chat_type`, default `group`, and `validate` requires `group`
  for Slack). The team in the wake key comes from `adapter.scope_id_for_chat`, which is right for one workspace.

## 6. Authorization

Four separate things:

1. **Seeing messages**: Slack/Telegram membership of the chat. muster does not manage it.
2. **Answering an agent's question** (question requests, Other/free text).
3. **Approving a consequential tool call** (permission requests).
4. **Build review** (build/feedback/design: merge, send back, approve a design).

First-release policy (decision needed, §13 D1): one set of **approvers**, configured as `notify_user_id`
(a string, as today, or a list). Only an approver, in the configured chat, in the run's conversation, in the
configured workspace (Slack), may do 2, 3 or 4. Everyone else in the channel can read and talk to the
coordinator agent (Hermes's own allowlist governs that), and nothing more. Roles (e.g. "anyone may answer
questions, only the owner merges") are cut until asked for.

Checks, in this order, before any state changes, on both channels:
- workspace (Slack `body.team.id`), chat (`body.channel.id` / `query.message.chat.id`), user in approvers;
- conversation: the message is in the request's thread/topic (`in_topic`, generalized: Slack
  `body.container.thread_ts` equals the run's `thread_id`);
- the request is still `open` (Slack reads the file; Telegram's Hermes state answers "expired");
- `decisions.transition(open -> answered)`: the double-tap and redelivery guard (already durable).

Refusals: Slack replies with an ephemeral "Not authorized" (`chat.postEphemeral` to the tapper) and logs user,
team, channel. Any exception in the check refuses (fail closed). A user removed from `notify_user_id` is refused
from the next config load (settings are read at gateway start; a change needs a gateway restart, as today). A
stale/double action gets an ephemeral "Already answered: <outcome>" and changes nothing.

## 7. Platform-native UX

**Telegram** keeps all of today's behaviour, moved, not rewritten: labelled clarify buttons, Other + ForceReply,
reply-to binding, Hermes approval card and wait (with `approvals.timeout`), forum topics.

**Slack** (one thread per run when `notify_topics` is on; else the channel itself):

```
#eng-agents
┌───────────────────────────────────────────────────────────────┐
│ muster · hermes-muster#40 Design channels · t_9f2c   ⏳ running │   <- parent, posted by the bot
└───────────────────────────────────────────────────────────────┘
  ↳ 3 replies
  │ hermes-muster #40
  │ Which storage should the cache use?
  │  [ Redis ]  [ SQLite ]  [ In-memory ]
  │  ┌ Your own words ──────────────────────────── ⏎ ┐       <- input, dispatch_action
  │  Proposal v2 5c0779c4 (full text on ledger t_9f2c)
  │ ─────────────
  │ hermes-muster #40 wants to run:
  │  `gh pr merge 41 --squash`
  │  [ Allow once ]  [ Deny ]   ┌ Deny with a message ── ⏎ ┐
  │ ─────────────
  │ Ella (coordinator): CI is green; recommend merge.
```

- Each question is its own message: section text, an `actions` block of choice buttons, and an `input` block
  with `dispatch_action: true` for free text. Free text is bound to that one question by its `block_id`, so
  several open questions in one thread never compete, and no ForceReply equivalent is needed.
- **Action ids are unique within each block** (Block Kit rejects a block whose elements repeat one; Hermes does
  the same with `hermes_clarify_choice_<i>`). The id says what the element is; the block and value say which
  request and question:

  | block (`block_id`) | element | `action_id` | `value` |
  |---|---|---|---|
  | `mu:<rid>:<n>:choices` | button i | `muster_choice_<i>` (i = 0..k-1) | `<rid>\|<n>\|<i>` |
  | `mu:<rid>:<n>:text` (input) | plain_text_input | `muster_text` | typed text |
  | `mu:<rid>:<n>:multi` | checkboxes, then a Submit button | `muster_multi`, `muster_submit` | option i: `<i>`; submit: `<rid>\|<n>` |
  | `mu:<rid>:approval` | Allow once, Deny | `muster_allow`, `muster_deny` | `<rid>` |
  | `mu:<rid>:deny` (input) | plain_text_input | `muster_deny_text` | typed reason |

  Registration: `ctx.register_slack_action_handler(re.compile(r"^muster_(choice_\d+|text|multi|submit|allow|deny|deny_text)$"), on_action)`,
  one handler, which parses `block_id` and refuses any id or block it does not know. `block_id`s are unique per
  message because each carries `rid` and `n`. Telegram is unaffected (its callback data already carries the index).
- Approval: Allow once / Deny buttons and a "Deny with a message" input. No session/permanent tiers (muster's
  cards never offer them; learnings #17).
- Handler: `ack()` first (Slack's 3 s), then the checks of §6 and the transition on the loop via
  `CTX.spawn_task`. On success the message is replaced with `chat.update` (blocks → one context line: the
  answer, then later the outcome). Telegram's "Received ✓" equivalent is that update.
- Thread replies that are not muster inputs go to Hermes untouched (the coordinator conversation). muster's
  `reply()` on Slack returns None for every message: cut test — free text has its own input, so swallowing
  thread replies would only take messages meant for the coordinator.
- Posting: the factory keeps `app.client` (Bolt's public `AsyncWebClient`, the bot token Hermes loaded);
  `chat.postMessage` with `blocks` and `thread_ts`. Hermes's `adapter.send` stays the route for plain notes.

## 8. Lifecycles

The approved separation stays (#24, #28, #33, topic lifecycle t_b9615602): **work resolved** (cleanup
`resolve`: PR merged → `resolved_at`, `finish`), **conversation closed/retired** (gateway `step`), **resources
cleaned** (cleanup removal, independent; a blocked removal never holds a conversation open).
`conversation.json` states stay the same names; what each means per channel:

| state | Telegram | Slack |
|---|---|---|
| pending/creating | `create_handoff_thread` once, adopt from the topic-created notice, else fallback | post the parent once (`chat.postMessage`, ledger id in text); save `asked` before the call; None/error → fallback to the channel, never a retry (a lost reply may have posted it) |
| open | topic open | parent "⏳ running" |
| closing → closed | finish note, `close_forum_topic`, retried till confirmed | finish note in thread, `chat.update` parent to "✅ done · PR #n merged"; retried till Slack confirms |
| closed + newer request | reopen topic | post in the thread anyway (threads cannot close) and update parent to "⏳ running"; same `created_at > closed_at` rule |
| retiring → deleted | stale open requests, `delete_forum_topic` (bounded removal, current retention policy) | stale open requests, `chat.update` parent to "archived"; terminal state named `deleted` for both (no Slack deletion; other users' replies are never deleted) |
| fallback | main chat | the channel, top level |

Resurrection guards, both channels: every create/reopen-equivalent reads the board fresh (`archived` → retire),
`deleted` is terminal, a request older than `closed_at` never reopens, and `stale_requests` ends an archived
run's prompts. Slack adds one: an action on a retired run's prompt reads the request (stale) and is refused with
its outcome. Telegram-only machinery not ported to Slack: topic-created notice reconciliation, deleted-topic
detection/recreate (`gone`, `repair`, `move_cards`), prerequisites (forum/admin rights), human close/reopen
notices. Slack has no equivalents; a human deleting the parent is an open item (§13 O1).

**Resumed vs replayed:** resumed work is a request created after `closed_at` (new pane activity), which
re-activates the conversation. A replayed event (restart rescan, Socket Mode redelivery, an old subscription
firing) carries an older `created_at` or hits a non-`open` request and changes nothing.

## 9. Reliability

- Durable: `decisions/*.json` (status, answer, **`partial`**, `presented.messages`, outcome, edited),
  `conversation.json`. In memory only: Telegram's Hermes clarify/approval waits (re-presented each boot, as
  today), the gateway's retry/backoff timers. Slack keeps nothing that matters in memory.
- **Partial answers.** Today a request with several questions keeps the answers it has in `S.partial`, in memory,
  and a restart loses them; on Telegram that is harmless because every question is presented again. On Slack the
  old messages stay live, so a lost partial would leave questions the human already answered (and saw marked
  answered) unanswered forever. So partial answers move into the request file:
  `decisions.answer_part(rid, n, part, total, compose)` under the request's flock: refuse unless `open`; refuse
  if question `n` already has a part (first answer wins; the action gets "Already answered: <part>"); save
  `partial[str(n)] = [answer, chosen]`; when all `total` parts are in, set `answer = compose(parts)`, status
  `answered`, and return `(req, True)` so the gateway calls `on_answered`. One locked read-modify-write, so two
  questions answered at the same moment (two taps, or a tap and a redelivery) cannot lose either. The gateway's
  `answered()` uses it on both channels and `S.partial` goes. Telegram keeps today's behaviour by clearing
  `partial` when it presents a request anew (its old prompts are dead then; Hermes answers them "expired").
  Slack never clears it: on the answer to question n, muster updates that question's message to
  "Answered: <part>", and after a restart the remaining questions' buttons still complete the same request.
- Create/send ambiguity: a create (topic or parent) is called once per destination; an unknown result falls back,
  never retries (learnings #22). A prompt send that fails is retried with backoff; a Slack send whose reply was
  lost can leave a duplicate prompt — harmless, because both carry the same `rid|n` and the transition admits one
  answer; the second prompt is updated to the outcome when the request ends (`settle` edits every saved id; an
  unsaved duplicate keeps live buttons that answer "Already answered").
- Ack: muster's Slack handler acks before any I/O. Redelivered or double actions: the transition guard.
  Hermes dedupes messages but not actions (research §12); muster needs no more than the guard.
- Confirmed vs delivered: unchanged wording rules (#21): "Sent to Claude", never ✓ unless muster verified.
- No mutating call as a probe (learnings, topic lifecycle). Slack has no probe at all.
- Restart: Telegram re-presents open requests (today). Slack does nothing; old buttons still work, and answers
  already given are in `partial`.
- Partial failure: Slack `chat.update` failing on settle → retried once next scan, then logged (as Telegram).

## 10. Configuration, migration, isolation

- `notify_platform` ∈ `channels.NAMES`; anything else is a `ConfigError` naming the valid ones. `config.require`
  calls `channels.get(name).validate(settings)`.
- Slack requires `notify_chat_id`, `notify_user_id`, `notify_workspace_id`, `notify_chat_type: group`; Hermes's
  own SLACK_BOT_TOKEN/SLACK_APP_TOKEN (read by Hermes, never by muster). Missing → a clear error at `require()`
  and no Telegram code path runs (`core.telegram_home` is called only by the Telegram channel's
  `notify_target`).
- `notify_topics` means "one conversation per run" on both channels (forum topic / thread). Keeps its name.
- Migration: none needed for Telegram installs. Defaults are unchanged; existing `conversation.json` already has
  `platform: telegram`; decisions files are unchanged. If an install switches platform, each conversation
  keeps its `platform`; the gateway steps only refs of its own channel; a request whose run's conversation is on
  the other platform is presented at the configured chat's top level, labelled with its run. Telegram topics of
  old runs are left as they are (no Telegram client is running to remove them); this is stated in the README.
- `gateway_up()` already reads `platforms[notify_platform].state`; Slack's state is in the same file.
  `events.muster_pages` checks "the selected channel is ready" instead of `== "telegram"`.
- Isolation (separate research): muster reads only its plugin settings and the Hermes runtime of the profile it
  is loaded in. Requirements on that work: the profile's Hermes must have the Slack platform connected with its
  own app tokens; muster's data dir is per profile (`plugin_data_dir`); nothing in this design reads the default
  profile or a personal connector. A profile is not a security boundary; the approver check of §6 is the
  control on who may act.

## 11. Test plan (for the implementation plan)

- Contract tests: the neutral gateway against a fake channel (present, answer, stale, settle, backoff,
  `durable_prompts` both ways).
- Telegram regression: the whole of today's `tests/test_gateway.py` passes against `channels/telegram.py`
  unchanged except imports: labelled choices, Other/ForceReply, permissions, build decisions, topic lifecycle,
  notify+wake routing.
- Slack native tests with a fake Bolt app/client (records `chat_postMessage`/`update`/`postEphemeral`, delivers
  `block_actions` bodies shaped as Slack's): two concurrent runs; two questions in one thread; labelled choice;
  free text via the input; unauthorized user; wrong channel, wrong thread, wrong team; double tap and redelivery;
  action after restart (new process, same files); permission allow/deny/deny-with-text; retired run's prompt
  refused; parent create lost → fallback with no second post.
- Partial answers (both revisions): a two-question request, question 0 answered, gateway state reset (a new
  `State()`, same files), question 1 answered → the request is `answered` with both parts; question 0 tapped again
  → "Already answered", nothing changes; both questions answered concurrently (two threads) → one `answered`, both
  parts. Telegram: re-presenting clears `partial`, matching today. `decisions.answer_part` unit tests: refuse
  non-open, first part wins, last part composes and transitions once.
- Unique ids: every block the Slack renderer builds (k choices, multi-select, approval) has no repeated
  `action_id`, and every `block_id` in a thread is distinct across two questions and two runs.
- Lifecycle: archived/abandoned/completed runs cannot reopen or repost through recovery, actions, replay or a
  stale subscription, on both channels; cleanup failure still reported separately.
- Config: unknown `notify_platform`, Slack without workspace/chat/user, Slack with `notify_chat_type: dm`;
  Slack config with no TELEGRAM_* anywhere never calls `telegram_home`; existing Telegram config and state load
  unchanged.
- Live acceptance (each step needs Jason's explicit go): one Slack run in a test channel: question, free text,
  permission, build tap, unauthorized tap by a second account, gateway restart with a prompt open; and probes
  O1-O3. Mock passes are not acceptance.

## 12. Safety boundaries

Approvals stay with named people (§6), fail closed. No Telegram fallback when Slack is chosen. No secrets read by
muster (Hermes owns the tokens). No deletion of anyone's Slack messages. No change to the request statuses or
the bridge's answer semantics.

## 13. Decisions and open items

- **D1 (Jason):** approver policy: (a) one approver set for questions, permissions and builds (recommended);
  (b) owner-only for builds/merges, approver set for questions and permissions.
- **D2 (Jason):** first-release scope: one channel per deployment, one Slack workspace (recommended), or
  concurrent Telegram + Slack now.
- **D3 (Jason):** tracking: this PR lands the design + plan and does not close #40's implementation; a new issue
  "Implement channels (plan 2026-10-17)" carries it, or #40 stays open.
- **O1, live probe, needs approval:** what `chat.postMessage` with a deleted `thread_ts` does (Hermes has no
  handling). Until probed, the plan treats a human-deleted parent as unsupported: messages may land orphaned.
- **O2, live probe:** `chat.update` replacing blocks on a muster card, and `chat.postEphemeral` from the
  plugin's client in Socket Mode. Expected per Slack's API; unverified here.
- **O3, live probe:** Slack app manifest has Interactivity on (Socket Mode needs it for block actions).
- Upstream drafts (ask before filing): Slack interactive-action precedence/auth hook for plugins; docs fix
  (factories are not first on Slack).

## Capability matrix

| Need | Hermes provides (verified in source) | muster work | Unresolved |
|---|---|---|---|
| Get Slack app + adapter | `ctx.register_platform_handler("slack", f)` → `f(AsyncApp, adapter)` (hermes_cli/plugins.py `register_platform_handler`); re-run per `connect()` | factory keeps `app.client`, adapter | — |
| Own action ids | `ctx.register_slack_action_handler(id, cb)`; `cb(ack, body, action)`; no Hermes auth; wrapper acks only on exception (slack adapter `_register_plugin_action_handlers`) | handler acks, authorizes, transitions | — |
| Guard Hermes clarify/approval | Not possible: core listeners registered first, Bolt first-match (adapter.py :1686-1701) | use own Block Kit (§3) | upstream ask |
| Threaded send | `adapter.send(chat, text, metadata={"thread_id": ts})` → `thread_ts`; `message_id` = ts | notes, finish note | deleted parent (O1) |
| Block Kit post/update | Bolt `AsyncWebClient` `chat_postMessage`/`chat_update`/`chat_postEphemeral` (public Bolt) | prompts, outcome edits | O2 |
| Thread creation | `create_handoff_thread` returns seed ts, None on any error | not used: muster posts its own parent (text it controls, ledger id) | — |
| Inbound identity | MessageEvent: `chat_id`=C…, `user_id`=U…, `thread_id`=parent ts, `scope_id`=team | none (replies left to Hermes) | — |
| Thread replies reach the gateway | admitted without @mention in a thread whose root the bot posted | none | — |
| notify+wake in thread | notifier sends with `thread_id`; wake key `…:slack:group:<team>:<chan>:<thread_ts>` | `--chat-type group` (validated) | multi-workspace scope (cut) |
| Gateway up | `gateway_state.json` `platforms.slack.state` | none (`gateway_up` is generic) | — |
| Action dedupe | none (messages only) | transition guard | — |
| Telegram today | all current surfaces (decision 2026-10-08) | moved to `channels/telegram.py` | — |
