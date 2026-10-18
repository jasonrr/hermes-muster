# muster prompts

All instruction text that muster gives an agent or model is in this folder, one file per purpose.
Edit the file to change the prompt; nothing else holds a copy.

- **Placeholders.** `$name` is data that the code fills in once, when the prompt is assembled. That data is never read
  as template text, so braces, backticks or `$` inside issue text, feedback or paths stay literal. To write a literal
  `$` in a prompt, type `$$`. If a placeholder has no value, or a file is missing, assembly raises an error before
  anything is sent: a launch fails before its agent starts.
- **Framing.** Blank lines at the start and end of a file are trimmed. The code adds the newlines that join one prompt
  to another, so an editor that trims trailing whitespace changes nothing that is sent.
- **Skills.** `dispatch.md`, `escalation.md` and `review-standard.md` are registered as Hermes skills themselves
  (`ctx.register_skill`, in `__init__.py`). Hermes serves the file whole, frontmatter included. Keep the
  frontmatter's `name` and `description`.
- **Overrides.** `workflow_prompt_file` replaces `builder-workflow.md`. No other prompt has an override.

## Inventory

Trust: **coordinator** = the Hermes agent the human talks to. **builder** = the coding agent in a herdr pane.
**model** = the cheap `ctx.llm` call. Text marked *untrusted* is data that the prompt itself tells the reader is
never to be taken as instructions.

| File | Read by | Consumer | Dynamic inputs |
|---|---|---|---|
| `dispatch.md` | coordinator | skill `muster:run`: ad-hoc dispatch, analysis workspaces; wakes are `escalation.md`'s | none |
| `review-standard.md` | coordinator | skill `muster:review-standard`, loaded by `muster:escalation`: review judgment, evidence and the human summary | none |
| `escalation.md` | coordinator | skill `muster:escalation`: wakes, build and design reviews, recommendations | none |
| `design-review-cue.md` | coordinator | `core.REVIEW_CUE`: end of a design-review card's summary, which routes the wake to the skill | none |
| `cleanup-warning-cue.md` | coordinator | `cleanup.ASK_LINE`: a line in a cleanup-warning card's body | none |
| `issue-dispatch.md` | builder | `core.brief`: the whole brief for a labeled issue | `$repo` `$number` `$branch` `$base` `$kind` (bug/feature) `$hermes` (path); `$approved_by` and `$bot`: identity sentences computed in code; `$workflow`: `builder-workflow.md` or the override; `$ask`: `ask-the-human.md`; `$production`: the repo's production note or `no-production-note.md`. Issue text never enters it. |
| `ask-the-human.md` | builder | "Asking the human" in the issue brief and the ad-hoc footer: questions and the proposal rule | `$hook`: `<hermes> muster hook`, with `--card <card>` for an ad-hoc run |
| `builder-workflow.md` | builder | "How to work" in the issue brief and the ad-hoc footer | none |
| `no-production-note.md` | builder | `core.production_note` when the repo has no note | none |
| `ad-hoc-run.md` | builder | `runs.footer`: appended to a human's ad-hoc brief (that brief is the human's own text) | `$card` `$repo` `$branch` `$base` `$hermes`; `$workflow` and `$ask` as in the issue brief |
| `reused-worktree.md` | builder | `core.reuse_note`: appended when a launch reuses a checkout that has work in it | `$commits` `$uncommitted` |
| `launch-envelope.md` | builder | `core.startup_prompt`: the first prompt, around the brief | `$brief`: the whole brief, JSON-encoded |
| `build-revision.md` | builder | `decisions.wire_text`: a send-back the human authorized | `$request` `$cycle`; `$feedback`: the feedback plus its tail, JSON-encoded (the pull request and issue stay untrusted) |
| `build-revision-issue.md` | builder | the tail of `build-revision` for an issue run | `$hermes` `$pr` |
| `build-revision-ad-hoc.md` | builder | the tail of `build-revision` for an ad-hoc run | none |
| `design-reply-approve.md` | builder | `decisions._design`: the answer to the approval question on Approve | `$proposal` (`Proposal v<n> <sha>`). Keep the first word `Approve`: `decisions.words()` reads it on records from before `action` was saved. |
| `design-reply-revise.md` | builder | the answer on Send revisions | `$proposal`; `$revisions`: the human's revisions |
| `duplicate-handoff.md` | builder | `hold.wire`: a possible duplicate, sent to the live run's agent | `$number` `$repo` `$hermes`; `$data`: JSON with the report and the model's reason (*untrusted*) |
| `duplicate-classifier.md` | model | `hold.judge`: instructions to the duplicate judge | none; the issues go separately as JSON input (*untrusted*) |

Not prompts, so they stay in code: hook refusal reasons the builder sees (`events.py`, `bridge.py`), card titles and
bodies, status and progress messages, button labels, logs and commit text.
