> **Historical record (2026-10-17).** Not operating instructions: later work may have changed what this describes. Current behavior is in the [README](../../README.md).

# Run progress: confirmed start and observed activity (issue #46) — design v2

## Problem

After a launch (or after the human approves a design) nothing reaches the human until the next question or the
pull request. A successful launch pings nobody (Hermes's notifier pings blocks and completions, not comments). The
human cannot tell "launched", "received its brief", "doing things", "waiting on me" and "quiet" apart.

## Chosen approach (the human picked "live status message", with refinements)

Hooks write evidence; the gateway shows it in **one status message per run, edited in place, never reposted**.

**Changes from v1 (the human's review of v1):** "waiting" comes from the bridge's existing open question and
permission requests, not from hook order; review-ready also comes from the ad-hoc completion path; receipt time and
last tool time are kept apart.

**Evidence.** Each writer touches one file under `<data dir>/runs/<ledger>/activity/`; the file's mtime is the
observation time. No JSON journal, no lock, no new process. Only hooks and the verified completion paths write
these files, so a message refresh can never reset an activity time.

| file | written by | means |
|---|---|---|
| `launched` | `core.ensure`, once the brief is sent (JSON: launch dir, brief sha256) | launch requested and sent |
| `prompt` | UserPromptSubmit hook | a prompt arrived (the brief, a send-back, a typed reply). Never shown as tool activity |
| `tool` | PostToolUse / PostToolUseFailure hook | a tool finished (main agent or a subagent) |
| `stop` | Stop hook | the agent ended its turn |
| `end` | SessionEnd hook (not `/clear`) | the session ended |
| `review` | the verified completion paths (content: PR url): issue runs `events.move("done")` after the ledger reads back `done`; ad-hoc runs `runs.complete` (verified by `runs.verify`); a verified revision in `decisions.rereview` (both kinds) | a pull request is ready for review |

`runs.hook` keeps refusing `hook done` for ad-hoc runs; their readiness comes from `runs.complete` and `rereview`,
which already verify the pull request. No new GitHub call.

**Brief receipt** is confirmed only by the existing UserPromptSubmit evidence: `core.seen(launch dir, sha256)` (the
hook's hash of the brief, wrapper-aware), and its time is that record's own `at`, not the `prompt` mtime (later
prompts move that). `seen` returns the matching record instead of True (callers only test truthiness).

**Waiting** reuses the bridge's request state as it stands: the ledger's `question` and `permission` requests whose
status is `open` (`decisions.open_requests()`, which the gateway scan already reads). They end only by the
existing paths (the channel's answer, `bridge.settle` on the AskUserQuestion's own PostToolUse or a
UserPromptSubmit, SIGTERM, the liveness check). So an unrelated tool finishing (a parallel subagent) never clears
"Waiting for you"; a subagent's open question stays waiting until its own request ends. No new dialog tracker,
and the `ask` file of v1 is gone. herdr screen state is never used.

**State (gateway side, a pure function of the files above, the run's open requests and its conversation reference):**

1. Ledger archived → `Archived` (final). Work resolved (conversation `resolved_at`, i.e. PR merged) →
   `✅ Finished: pull request merged` (final). Final messages are never edited again: a late or replayed hook
   cannot revive them.
2. `end` newest → `⛔ Session ended at 15:30.`
3. An open question/permission request → `✋ Waiting for you since 14:41 (2 open)` + the tool line below.
4. `review` newer than `prompt` (no send-back since) → `🔍 Ready for review: <PR url> (since 15:02)`.
5. `stop` newer than `tool` and `prompt` → `💬 Turn ended at 15:20: the agent waits at its prompt (no question open).`
6. Brief not confirmed → `🚀 Launched 14:01 · brief receipt not confirmed yet.`
7. Otherwise `Brief received 14:02 · ` + the tool line:
   - no `tool` yet → `no tool activity observed yet` (+ `(38 min)` once 15 min have passed since receipt)
   - `tool` under 15 min old → `last tool activity 3 min ago (14:37, as of 14:40)`
   - 15 min or more → `no new activity observed for 40 min (last tool 14:37). Long tools can be quiet; muster
     takes no action.`

Resumed after approval: the bridge ends the approval request when the answer is delivered (or `settle` sees the
AskUserQuestion's PostToolUse), so the message leaves "Waiting for you"; the next `tool` shows the agent at work.

**Edits.** Edit when the state (1–7) changes. While in state 7, refresh at most once per 5 min, and only if the
text changed (elapsed minutes). No other cadence, no heartbeat.

**Where the message lives (reusing the existing run-status message).**
- Slack with a run thread: the existing thread parent (`muster · <run>  ⏳ running`) gains the progress line;
  `✅ done` / `archived` stay as they are (they are the final states). No second message.
- Telegram, and Slack without a thread: there is no run-status message today, so the first one is posted once,
  in the run's topic (or main chat), when the run is launched; later questions land below it, so it never buries
  a decision. Its id is kept in `<data dir>/runs/<ledger>/progress.json` (message id, thread, shown text, state,
  edited_at, final). A restart re-reads it: no repost. A post Telegram/Slack clearly refused is retried once a
  minute; a post whose result was lost (exception) is not retried, so no duplicate is made (logged).
- If the run's topic is deleted and remade, the message is posted once in the new topic (the old one is gone).

**Never.** No retry, redispatch, approval, merge or cleanup from this code; it only sends/edits its own message.

## Scope and non-goals

In: issue runs and ad-hoc runs launched after the upgrade. Out (cut test: nothing breaks without them):
- an on-demand `hermes muster status` command (the message already shows the status);
- a separate "started" notice (you asked for one message);
- transcript or pane content, screen-state idle detection (forbidden by the issue);
- a resolved signal for runs without `notify_topics` (see gaps).

## Detection gaps (documented in the README)

- A long tool (a test suite) fires no hook until it ends: shown as "no new activity observed", not as failure.
- An MCP elicitation dialog, or a subagent permission prompt while the gateway is down, has no bridge request: it
  is not shown as waiting (its Hermes ping still goes out).
- A plain-text question (no AskUserQuestion) shows as "Turn ended … waits at its prompt".
- Without `notify_topics` nothing records the merge, so the message stays "Ready for review" until the ledger is
  archived.
- Panes launched before the upgrade have no `launched` file and get no message.
- Times are the gateway host's local clock.

## Safety boundaries

Sends and edits only muster's own message in the configured channel/thread; reads only muster's data files.
Writes from hooks are mtime touches inside muster's data dir; a failure is swallowed (a hook never fails the agent).

## Trade-offs

Mtime files over a JSON journal: zero locking and atomic by construction, at the cost of only "last time per kind"
(enough for every state above). A gateway pass over `runs/*/activity/launched` each scan: a few stats per live run.

## Test plan

- `tests/test_progress.py`: the state function for each state above, including launch with and without confirmed
  receipt, resumed after approval, long-running active (fresh tool activity at 2 h), quiet, waiting, review,
  revision after send-back, session ended, finished, archived.
- Gateway pass (Telegram and Slack fakes): post once; edit on state change; no edit inside 5 min in state 7;
  refresh after 5 min changes only the text; a refresh never changes any activity mtime; a restart (fresh process
  state, same progress.json) does not repost; a final message is not edited by later activity; a remade topic gets
  one new post; a Slack thread's parent carries the line; a refused post retries, a lost one does not.
- Waiting: an open main-agent question plus an unrelated subagent tool completion stays "Waiting"; the answer
  (request done via delivery or `settle`) leaves it and the next tool shows activity; an open subagent question is
  kept while the main agent's tools run; a permission request counts the same way.
- Review-ready: an issue run via `hook done`; an ad-hoc run via `runs.complete` (Stop delivery with a verified PR);
  a verified revision via `rereview`; a send-back prompt after review returns to working.
- Receipt: brief received with no tool yet shows "no tool activity observed yet"; a later prompt does not move the
  receipt time; a non-brief prompt does not confirm receipt.
- Hook tests (`events.hook`, `runs.hook`): each hook event touches its file; a replayed hook only moves an mtime.
- Full suite + `hermes plugins validate .`.

## Decision needed

Approve design v2: v1's message, refresh and thresholds unchanged; waiting from the bridge's open requests;
review-ready from the verified completion paths of both run kinds; receipt and tool times kept apart.
