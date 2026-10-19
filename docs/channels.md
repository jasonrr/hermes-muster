# Telegram and Slack

Muster brings every decision a run needs into one chat: questions, permission prompts, design approvals,
reviews and merges. You never need to open herdr to decide anything; the panes stay there for watching a
run or typing into it. This guide covers what you see in each channel and how to set it up. The settings
themselves are in [setup](setup.md#chat).

## Who may answer

Only the approvers listed in `notify_user_id` (or, for a Telegram DM, the DM's user) can tap or reply to a
Muster prompt, and only in the configured chat. Being allowed to talk to your Hermes agent is not enough.
Anyone else's tap is refused and logged, and anyone else's message goes to the Hermes agent as an ordinary
message. If you try to answer a builder's question by talking to your Hermes agent, it tells you to answer
on the question's own message.

## What you see

- **A status message per run,** edited in place, so you can tell a working run from a quiet or waiting
  one. See [run status](#run-status).
- **Questions** with the builder's context and one button per option, plus a way to answer in your own
  words. A question waits up to 24 hours.
- **Permission prompts** with the command, **Allow once** and **Deny**. You can deny with a message, which
  the builder is told. There is no "allow for the session". An unanswered prompt is denied after at most
  10 minutes.
- **Design approvals:** your Hermes agent's review, the full design, and **Approve design**,
  **Send revisions to builder** or **Leave pending**.
- **Build reviews:** your Hermes agent's review, its recommendation, and **Merge**, **Review feedback…**
  or **Leave PR open**.

You can also answer any question in the pane; the first answer wins. After you answer, each message is
edited to say what happened, such as "Answer sent to the builder" or "Answered in the pane". "Sent" means
Muster handed your answer to Claude Code; an allow is permission, not proof that the command succeeded.
A double tap, replay or gateway restart never repeats an effect. Details of each decision are in the
[reference](reference.md#during-a-run).

## Telegram

- **Questions** use Hermes's own clarify prompt. Tap an option, tap Other and type, or reply to the
  message. For several choices, type the numbers (`1,3`).
- **Permission prompts** use Hermes's own approval card. Replying to the card denies the prompt and passes
  your words to the builder. Hermes's `approvals.timeout` applies, capped at Muster's 10 minutes.
- **After a gateway restart,** unanswered questions are shown again. Older copies say "Replaced after a
  restart", and answers already given are kept.

## Run topics

Turn on `notify_topics: true` to give each run its own Telegram forum topic in the notify group. The
topic is named `<short title> · #<issue>` or `<short title> · <branch>` (with several repositories,
`<repo>#<issue>`). Everything about the run happens there: its status message, questions, designs,
reviews, buttons and send-backs. Your Hermes agent's comments on the run land there too. General planning
stays in the main chat.

**Before you turn it on:** enable Topics in the group, and make the bot an admin with **Manage Topics** and
**Delete Messages**. Muster never changes these settings.

What to expect:

- **At launch** the topic is created. If Topics is off, a right is missing or the gateway is down, the run
  stays in the main chat, and you get one message saying why.
- **Answers count only in the run's own topic.** A Muster message that Hermes moved to General answers
  "Moved", and the request is shown again in its topic.
- **If you delete a topic by hand,** Muster notices when the next prompt lands outside it, creates a new
  topic and shows the open prompts there.
- **When the work is merged,** the topic gets "Finished. This topic is now closed." and is closed. A new
  request from the run, such as a reviewer's question, reopens it.
- **Closing or reopening a topic by hand** is respected: nothing reopens it except a new request, and a
  topic you reopen stays open until you close it.
- **When you archive the run's ledger card,** its open prompts end with "The run is archived" and its
  topic is deleted within about 30 seconds (5 minutes for a closed topic). The archived card keeps the
  pull request, proposals and comments.

**Not verified live:** if Telegram creates a topic but its reply is lost, Muster relies on Telegram's
"topic created" notice to find it. Whether Telegram delivers that notice to the bot that made the topic
has not been checked live. After your first launch with topics on, look in
`$HERMES_HOME/plugin-data/muster/logs/gateway.log` for a `topic notice: thread …` line. Without the
notice the run stays in the main chat, and at worst one empty topic is left open.

## Slack

**Not verified live:** Slack support is tested against a fake Slack client. No acceptance run in a real
Slack workspace has been recorded, so check it in yours before relying on it.

### Slack setup

1. Hermes owns the Slack app and its tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`); Muster reads no Slack
   secret. In the app's settings, turn on **Interactivity**, which Socket Mode needs for buttons.
2. Invite the bot to the channel.
3. Set `notify_platform: slack`, `notify_chat_id: C…` (the channel), `notify_user_id` (one or more member
   ids `U…`, separated by commas) and `notify_chat_type: group`. Slack has no DM fallback.
4. Restart the gateway. A wrong setting is refused by `hermes muster` and logged when Slack connects.

Only one workspace is supported: Muster posts with Hermes's first bot token.

### Using it

- **Questions** are Muster's own messages: one button per option and a "Your own words" box (press Enter
  to send). Multi-choice questions use checkboxes and **Submit**.
- **Permission prompts** show the command verbatim, with **Allow once**, **Deny** and a "Deny with a
  message" box. An unanswered prompt is denied after 10 minutes; Hermes's `approvals.timeout` does not
  apply on Slack.
- **Thread replies are never answers;** they go to your Hermes agent as usual. Anyone but an approver who
  taps gets "Not authorized", visible only to them.
- **Too big to show:** a command over 2,900 characters, or more options than Slack can show (25, or 10 for
  multi-choice), gets a card saying so, and you answer in the pane.
- **Restarts** post nothing again, and prompts stay answerable. Rarely, if the gateway dies just after a
  post, a prompt is posted twice; both copies answer the same request once.
- **Threads** (`notify_topics: true`): one parent message per run, with the run under it. The parent shows
  the title, the run's state and the issue link, then "Finished · PR #<n> merged" or "Archived". Muster
  deletes no Slack message; do not delete a parent by hand, or later messages may be orphaned.
- **Your Hermes agent in a thread:** Hermes's `thread_require_mention`, `strict_mention` and
  `allowed_channels` settings can keep un-mentioned thread replies from reaching it. Check them if it does
  not answer in a run's thread.

## Switching platform

Switching `notify_platform` leaves the old platform's topics or threads as they are. Open requests of a
run whose conversation is on the other platform are posted at the top of the new channel, headed with the
run's name.

## Run status

Each run has one status message, edited in place and never reposted. With Slack threads it is the
thread's parent; otherwise it is posted once, when the brief is sent. Every line comes from the pane's own
Claude Code hooks:

| Line | Meaning |
|---|---|
| Dispatched 14:01 · waiting for the builder to start | The brief was sent; the builder has not yet confirmed it. |
| Started 14:02 · no tool activity yet | The builder received the brief. |
| Working · last activity 3 min ago (14:37) | A tool finished recently, in the builder or one of its subagents. |
| Quiet 40 min · last tool 14:37 · long tools can be quiet | No tool has finished for 15 minutes. Unknown, not failed. |
| Needs your decision: question, design v2 · since 14:41 | Something is waiting for you. |
| Ella is reviewing design v2 · no action needed | Your Hermes agent is reviewing a design. |
| PR #9 opened · Ella is reviewing · no action needed | A verified pull request is under review. |
| PR #9 open · Ella reviewed it | The review is done and you decided. |
| Feedback delivered 15:10 · revision pending | The builder received your send-back or design revisions. |
| Turn ended 15:20 · the builder is waiting at its prompt, no question open | The builder stopped without asking anything. |
| Session ended 15:30 · the builder is not running | Claude Code exited. |
| Finished · PR #9 merged / Archived | Final. |

The status only reports; a quiet or unknown status never triggers a retry or any other action. It is
edited when the state changes, and at most every 5 minutes while only its times age. Its limits:

- A long tool, such as a test suite, fires nothing until it ends, so the run reads as Quiet.
- A question asked in plain text, instead of with the question tool, reads as "Turn ended".
- An MCP elicitation dialog does not read as waiting.
- Without `notify_topics`, nothing records the merge, so the line stays on the pull request until the
  ledger is archived.
- Times use the gateway host's clock.
