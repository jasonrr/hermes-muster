# Plan: decisions with Markdown attachments

Design: [2026-10-20-markdown-decision-attachments-design.md](2026-10-20-markdown-decision-attachments-design.md)
(approved, Proposal v1). Verify everything with `.venv/bin/python -m pytest -q` from the worktree root
(learnings, PR #1: the system python has no pytest). Lint: `.venv/bin/ruff check muster tests` if ruff is installed
in the venv, else skip and say so.

Terms: a *decision card* is a `build` or `design` request (`muster/decisions.py` `recommend`, `recommend_design`).
Its *documents* are the files this plan attaches. A `feedback` request keeps today's inline detail message.

## Task 1: the documents of a card (channel-neutral)
- Files: `muster/channels/shared.py`, `tests/test_decisions.py`.
- Nearest pattern: `shared.detail` (same inputs: `req`, `events.proposals`, `progress.pr_name`, `title`).
- Test first (`tests/test_decisions.py`): for a design request pinned to v2 (`save_versions`, `design_args`),
  `shared.documents(req)` returns `[(f"{slug}-design-v2-review.md", review), (f"{slug}-design-v2.md", <v2 text>)]`
  with the texts exactly the saved review and the saved `v2.md`; for a build request,
  `[(f"{slug}-pr-9-review-{head[:7]}.md", review)]`; for a feedback request `[]`; a `v2.md` rewritten after the pin
  raises `ValueError` naming "Design v2". `shared.detail(req)` returns None for build and design (a feedback
  request still gets its text). Replace the old assertion `shared.detail(req).count("Long review.") == 500`.
- Code: `documents(req)`; slug = the lowercased `title(req)` with runs of non `[a-z0-9]` turned into `-`, stripped,
  cut to 40, else `run`. The pin check is `hashlib.sha256(text.encode()).hexdigest()[:12] == proposal["sha"]` (as
  `recommend_design`). `detail()` keeps only its feedback branch. `detail_label` stays (it names the documents).
  Add `attach_line(req)`: the card's line about the documents, from `req["detail"]`:
  `{"files": [...], "failed": str?}`: when `failed`, `⚠️ {label} could not be attached ({error}). Ask {who} in
  this thread for it.`; else None.
- Verify: `.venv/bin/python -m pytest -q tests/test_decisions.py`.
- Exists because: both channels must attach the same files with the same version-bound names.

## Task 2: a long review needs a summary
- Files: `muster/decisions.py`, `tests/test_decisions.py`, `prompts/escalation.md`, `prompts/review-standard.md`.
- Nearest pattern: `summary_of` refusals (refused, never cut).
- Test first: `recommend` and `recommend_design` with a review over `SUMMARY_MAX` and no `--summary` print a
  refusal naming `--summary` and return 1; at or under `SUMMARY_MAX` without a summary still succeed. Fix any
  existing test that passes a long review without a summary (give it a summary).
- Code: `summary_of(args, kind, review)` raises when no summary and `len(review) > SUMMARY_MAX`. Prompt text:
  "the full review is one tap away above it" → "attached as a Markdown file"; "under your full review and the
  design's full text" → "with your full review and the design attached as Markdown files"; the preview example
  "section 1 of the plan, just above this card" → "section 1 of the attached design file".
- Verify: `.venv/bin/python -m pytest -q tests/test_decisions.py tests/test_cards.py`.
- Exists because: without it a card with no summary would still carry a whole long review inline.

## Task 3: Slack attaches the documents
- Files: `muster/channels/slack.py`, `tests/fake_slack.py`, `tests/test_slack.py`.
- Nearest pattern: `slack.post_detail` (save before the permalink, parent re-edit), `slack.post`.
- Fake: `Client.files_upload_v2(**kw)` records into `self.uploads`, honours `fail`, returns
  `{"ok": True, "files": [{"id": f"F{n}", "permalink": f"https://example.slack.com/files/U1/F{n}/{filename}"}...]}`.
- Test first: a build and a design card each make exactly one `files_upload_v2` call in the run thread
  (`thread_ts`), with `file_uploads` whose `filename`s and `content` equal `shared.documents(req)`, then the card;
  no `chat_postMessage` holds the review; the card's context links the first file's permalink labelled with
  `detail_label`; a second scan uploads nothing; the thread parent links "Coordinator's full review" to that
  permalink (rewrite `test_an_open_review_is_linked_from_the_thread_parent_above_the_activity` and
  `test_a_long_review_is_its_own_message_whole...`). Failure: `fail["files_upload_v2"] = ["missing_scope"]` → the
  card still posts, and its context says `could not be attached (missing_scope)`; nothing claims attached. A design
  whose `v2.md` changed after the pin → no upload, card says so.
- Code: in `post_detail`, a build/design request goes to `attach(req, meta)`: skip when `detail.platform == name`;
  documents → one `T.client.files_upload_v2(channel=chat(), thread_ts=..., initial_comment=<escaped label · subject
  · title>, file_uploads=[{"content", "filename", "title": filename}])`; on success
  `detail = {"platform": name, "files": names, "id": files[0].id, "url": files[0].permalink}`, then the existing
  parent re-edit; on an exception or ValueError `detail = {"platform": name, "files": [], "failed": error}`
  (a Slack card is never re-presented). `link(req)` shows `attach_line` as a context line when set.
- Verify: `.venv/bin/python -m pytest -q tests/test_slack.py tests/test_cards.py`.
- Exists because: Slack is where the failure was reported.

## Task 4: Telegram attaches the documents
- Files: `muster/hermes_private.py`, `muster/channels/telegram.py`, `tests/fake_hermes.py`,
  `tests/test_gateway.py`, `tests/test_hermes_private.py`.
- Nearest pattern: `hermes_private.send_labelled_clarify` (private send shell, Hermes file:line comment);
  telegram `present`'s detail block.
- Fake: `Adapter._send_local_file(label, path, chat_id, reply_to, metadata, media_key, build_kwargs, on_error)`:
  reads the file, records `(chat, filename, text, caption, thread)` in `self.documents`, and on `fail_documents`
  calls `on_error(Exception("boom"))` and returns its result.
- Test first (`test_gateway.py`): a build card sends one document, a design card two (review, then design), in the
  run's topic, contents exactly `shared.documents(req)`, then the plain card ending with
  `Full review: the Markdown files above.`; a restart (new `gateway.State()`) re-presents the card and uploads
  nothing; with the second document failing, the card ends with the `could not be attached` line, and the next
  restart sends only the missing file. `test_hermes_private.py`: `_send_local_file`'s parameters in
  `plugins/platforms/telegram/adapter.py` are still `[self, label, path, chat_id, reply_to, metadata, media_key,
  build_kwargs, on_error]`.
- Code: `hermes_private.send_document(adapter, chat_id, path, name, caption, metadata)` → `_send_local_file(...,
  on_error=` a failed result `)`; comment names adapter.py:5341/5363 and the upstream ask (send_document reports a
  failed upload as success when its notice posts). In `telegram.present`, a build/design request: for each
  document not in `detail.files`, write it to a `tempfile.TemporaryDirectory()` under its name and send; save
  `detail = {"platform", "files": delivered, **({"failed": error} if any failed)}` after the loop. `render()` tail:
  delivered all → `f"{detail_label}: the Markdown files above."`, else `attach_line`; a feedback detail keeps
  "Full text: the message above.".
- Verify: `.venv/bin/python -m pytest -q tests/test_gateway.py tests/test_hermes_private.py`.
- Exists because: Telegram parity was explicitly requested, and Hermes's public send cannot report a failed upload.

## Task 5: samples, docs, full suite
- Files: `tests/test_cards.py`, `tests/golden/cards/*`, `docs/learnings.md`, `docs/channels.md`/`docs/reference.md`
  where they describe the full-review message.
- Test first: a golden `design-attachments-slack.txt` and `design-attachments-telegram.txt`: the upload/document
  lines (file names, caption) followed by the card as each channel renders it (reproducible sample messages).
- Verify: `.venv/bin/python -m pytest -q` (all) and lint.
- Exists because: acceptance item 6 asks for reproducible sample messages for both platforms.

## Plan attack (fresh sub-agent), and what changed
1. A request made before this change has `detail = {"platform", "id"}` and no `files`: it is *legacy*; attach
   nothing to it and keep its old tail ("Full text: the message above." / its message link). Tasks 3 and 4.
2. Hermes awaits `on_error(e)` (telegram adapter.py:5357): pass an async function; the fake awaits it. Each send is
   also wrapped in try/except, as the clarify send is.
3. Save `detail` after each delivered Telegram file, not after the loop, so a crash never re-uploads one.
4. `files.completeUploadExternal` returns only `{id, title}` per file (Slack's docs; slack_sdk async_client.py:4124
   returns it as is). The permalink comes from `files_info(file=id)`; without it the card says "the Markdown files
   in this thread" and the parent shows no link (cosmetic). The fake returns `{id, title}` only.
5. The "could not be attached" line shows only while the request is open, never on a settled card.
6. The pin is checked on the raw `v<n>.md` text, as `recommend_design` hashes it; the file is that raw text.
   Telegram's present catches the ValueError like Slack's.
7. `documents()`: the review file only when the card has a summary (otherwise the card already holds the whole,
   short review); the design file always for a design card. A build card with no summary attaches nothing.
8. `test_revisions_and_what_each_button_does_survive...` gets a summary; the SUMMARY_MAX comment and refusal text
   are updated. Tests rewritten: test_slack.py:900, :937 (feedback detail stays), :1091, :1110;
   test_gateway.py:2156.

## Added during the build (Jason's request): a compact Slack presentation
Slack offers no way to collapse a shared file's preview: `files.completeUploadExternal` takes only the channel or
thread, `initial_comment` and `blocks`, and an upload with no channel "will remain private … not actually shared
anywhere" (Slack docs), so a private upload plus a permalink is no proof the channel can open it, and a live check
was not authorized. Kept: one shared upload in the run's thread (channel members can open and download the files,
as with any file shared there). Added: the card's link row, `📎 Full review · Design v2`, one permalink per file
(`files_info`), so the card itself leads to each document. Each viewer can hide uploaded-file previews in Slack's own
preferences (Messages & media → "Show images and files uploaded to Slack"); muster changes no setting.
