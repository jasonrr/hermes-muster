# Design: decisions with Markdown attachments (Slack and Telegram)

## Problem
A build or design decision posts a "detail" message above its card: the coordinator's full review and, for a
design, the whole proposal (`shared.detail`). Slack splits it into many sections and messages; Telegram splits it
into several messages. Jason found that hard to read; the same text as `.md` files was much better.

## Approach
1. **The detail becomes files, posted once above the card, in the run's thread/topic.**
   - Design v3: `<slug>-design-v3.md` (the saved proposal `proposals/v3.md`, whole, re-checked against the card's
     pinned hash; a mismatch attaches nothing and says so) and `<slug>-design-v3-review.md` (the full review).
   - Build: `<slug>-pr-59-review-<head7>.md` (the full review of that head).
   - Feedback cards (a long send-back confirmation) keep their inline text: it is what the tap authorizes.
2. **The card stays the decision surface**, unchanged in shape: headline, recommendation, the whole feedback or
   revisions, the coordinator's summary (≤ 900 characters), button hints, buttons. `recommend` and
   `recommend-design` now refuse a review over 900 characters without `--summary` (the coordinator's skill already
   always passes one), so no card ever carries a whole long review.
3. **Delivery uses each platform's native upload.**
   - Slack: Bolt's `files_upload_v2` on the app client muster already posts with (one call, both files, in the
     thread). It raises on failure. Its file permalink replaces the old detail permalink on the card and on the
     thread parent ("Ella's full review").
   - Telegram: Hermes's own document send shell. Blocker found: Hermes's public `adapter.send_document` (Slack
     and Telegram) answers a failed upload by posting a "couldn't send file" notice and returns *that notice's*
     success, so muster could not tell delivered from failed. So Telegram goes through
     `hermes_private.send_document`, which calls the same `_send_local_file` shell `send_document` uses, without
     the notice fallback (with the Hermes file:line, a shape test, and an upstream ask:
     `send_document` must report a failed upload as failed). Slack needs no private name.
4. **Failure is visible, never silent.** The card still goes out (a missing file never hides the decision). Its
   last line then says: "⚠️ The full review could not be attached (<error>). Ask Ella in this thread for it."
   Delivered files are recorded on the request (`detail`), so a Telegram re-presentation after a restart sends only
   what is missing and never re-uploads; a Slack card is not re-presented, as today.

```
Slack thread, design v3                              Telegram topic, design v3
📎 Full review and design v3 · Fix it                📎 fix-it-design-v3-review.md   Full review · Design v3 · Fix it
   fix-it-design-v3.md  fix-it-design-v3-review.md   📎 fix-it-design-v3.md          Design v3, full text · Fix it
*🟡 Design v3 · Fix it*                               🟡 Design v3 · Fix it
*Ella recommends sending it back with these           Ella recommends sending it back with these revisions:
revisions:*  > <every revision, whole>                > <every revision, whole>
*Why* <summary ≤ 900>                                 Why <summary ≤ 900>
*What each button does* …                             What each button does …
[Send back with these revisions]* [Approve…] [Leave]  Full review and design v3: the files above.
Full review and design v3 (link to the file)          [Send back…] [Approve design v3] [Leave pending] [Other]
```

## Cut
- A new document store, links to the local data dir, inline fallback dumps: the brief forbids or does not need
  them. Re-uploading on Slack after a failure: no re-presentation path exists there today; the card's explicit
  line plus the coordinator in-thread covers it.
- Changing review judgment, status messages or tool-progress: out of scope.

## Decision needed
Approve this design, including the one private Hermes call for Telegram (`_send_local_file`) in
`muster/hermes_private.py`, and an upstream ask drafted (not filed) for a truthful `send_document` result.
