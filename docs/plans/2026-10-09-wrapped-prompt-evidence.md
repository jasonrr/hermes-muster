# Wrapped prompts count as delivered (issue #27)

## Problem (reproduced, root cause confirmed)

When the prompt is long, Claude Code wraps it before the UserPromptSubmit hook sees it, as
`"\n\n<pasted_content id=\"ef30\">\n" + text + "\n</pasted_content id=\"ef30\">\n"`.
`core.prompt_seen` hashes that wrapped string. `decisions._send` (and `core.delivered` for the
startup brief) wait for the hash of the bytes muster actually sent, so a delivered prompt never matches.

Evidence, checked byte for byte:
- Run t_0e034c97, session 43300d37: the hook recorded `a3427c6b…`, the hash of the wrapped text.
  The inner text hashes to `83dfa8dc…`, exactly the `intent.prompt_sha` of feedback 11f57922.
- This run (t_c8422c1b): the hook recorded `23a66848…`, the hash of the wrapped text. The inner text
  equals `launch.json`'s brief byte for byte. The startup path only got through because herdr's
  `working` status covered for the evidence mismatch.

The redundant retries come from the same cause. The retry's wire text is identical to the original's
(same `origin`, cycle and text), so `_send`'s `seen` check would have reported "Sent ✓" without
resending. Instead it saw no evidence, found the agent working, and offered yet another retry.

## Approach

1. `core.prompt_seen` also records `inner`: the sha256 of the inner text. It does this only when the whole
   prompt fully matches that exact wrapper, with the same id on the open and close tags and the exact
   newlines. Anything else records no `inner`. `core.seen` accepts a match on `sha256` or `inner`.
   - The inner text must hash to exactly what muster sent, so modified or unrelated feedback can never match.
   - No general wrapper stripping: one anchored regex for the one observed shape.
2. Retiring a retry that is still pending when the late acknowledgment arrives:
   - `_retry` stores the original's `prompt_sha` on the retry request.
   - New `decisions.reconcile(req)`: if an `open` feedback request carries a `prompt_sha` the run's
     evidence has now seen, move it `open → done` with the outcome "Sent ✓". It goes through the existing
     `transition` guard, so a click arriving at the same moment cannot also send.
   - The gateway's existing per-request `handle()` calls it once per scan, before presenting. The existing
     scan code then edits the message to the outcome and releases its Hermes prompt.
   - The "Sent ✓" outcome also lets `rereview` see the send-back as delivered.

The startup brief (`core.delivered`) is fixed by step 1 with no change of its own.

## Cut, and why

- Changing the busy-agent refusal: with step 1 in place, the issue's second confirmation finds the
  original's evidence and ends "Sent ✓" before the busy check runs. A busy agent with no evidence still
  means not delivered, and working status is not proof of receipt (per the issue).
- Re-marking the original failed request as sent: its message already says a retry follows. The retry
  carries the outcome, so there is no second copy of state.
- Generic unwrapping of other shapes: none observed, and the issue warns against it.

## Safety

No new process, credential or state file. One optional field on prompt-seen records. Older records
without `inner` still match on `sha256`. A torn line is skipped as before. Requester authorization
is untouched: the hash still has to match the exact bytes muster sent.

## Test plan (tests first, each watched failing)

- `tests/test_runs.py`: a wrapped prompt is seen under its inner hash. Mismatched ids, extra text
  outside the wrapper, or modified inner text are not seen.
- `tests/test_decisions.py`: the World's herdr fake delivers wrapped, as Claude does.
  - The send is "Sent ✓" (it fails today).
  - A second confirmation while the agent is working, with the original's wrapped evidence present,
    is "Sent ✓" with no second prompt.
  - Delayed acknowledgment: a retry still open when evidence arrives is retired by `reconcile` to
    "Sent ✓". A click after that does nothing.
- `tests/test_gateway.py`: one scan retires a seen retry and presents nothing.
- Full `pytest` and `ruff` before the PR.

## Decision needed

Approve this design: (1) wrapped evidence plus (2) retiring a pending retry when the late
acknowledgment arrives.

## Plan

Verify everything with `python -m pytest -q && ruff check .` (repo root).

1. **Wrapped evidence** (`muster/core.py` `prompt_seen`/`seen`, `tests/test_runs.py`). Pattern: the torn-line
   test `test_a_torn_prompt_seen_line_neither_hides_nor_swallows_evidence`. Test first: a wrapped
   `"\n\n<pasted_content id=\"ef30\">\nthe brief\n</pasted_content id=\"ef30\">\n"` is seen under
   sha256("the brief"), while mismatched ids, text outside the wrapper, or a changed inner are not. Then add a
   module regex `PASTED`, record `inner` only on a fullmatch, and have `seen` accept `sha256` or `inner`.
   Verify: `python -m pytest -q tests/test_runs.py`. Exists because `prompt_seen` hashes the wrapped prompt.
2. **Send-back through a wrapping Claude** (`tests/test_decisions.py` World fake at line ~198). Test first: the
   World delivers wrapped (a `wraps` flag). The send ends "Sent ✓". A retry tapped while the agent is `working`,
   with the original's wrapped evidence present, ends "Sent ✓" with one prompt. No code beyond task 1.
   Verify: `python -m pytest -q tests/test_decisions.py`. Exists because these are the issue's regressions.
3. **Retire a pending retry** (`muster/decisions.py` `_retry` + new `reconcile`, `muster/gateway.py` `handle`).
   Pattern: `gateway.handle`'s silent-hook stale branch (transition, release, return). Test first, in
   `tests/test_decisions.py`: after a not-seen send, the retry carries `prompt_sha`. Once the evidence arrives,
   `reconcile(retry)` moves it to done "Sent ✓", and a later tap/execute does nothing. In `tests/test_gateway.py`:
   one scan retires an open feedback request whose `prompt_sha` is in `run.evidence_dir` and presents nothing.
   Then `_retry` passes `prompt_sha=(req.get("intent") or {}).get("prompt_sha")`. `reconcile(req)` returns True
   after `transition(rid, ("open",), "done", outcome="Sent ✓")` when the request is open feedback with
   `prompt_sha` and `core.seen(req["run"]["evidence_dir"], sha)`. `handle` calls it via `asyncio.to_thread` for
   feedback, then releases and returns. Verify: both test files. Exists because a retry offered before a late
   acknowledgment otherwise stays open and can record "Not sent" for delivered instructions.
4. **Learnings** (`docs/learnings.md`): one `pattern → consequence` bullet.

### Fixes from the plan attack

- Regex: `re.compile(r'\n\n<pasted_content id="([0-9a-f]+)">\n(.*)\n</pasted_content id="\1">\n', re.S)`,
  `fullmatch` only. Tests include a multi-line inner text.
- World's herdr fake wraps by default (`wraps = True`), as Claude does; the late-hook tests wrap their evidence too.
- `reconcile` reads `(req.get("run") or {}).get("evidence_dir")` and does nothing without it (an issue run
  whose links lack `launch_dir`). A run relaunched with a new `launch_dir` is just missed, benignly.
- `handle` calls `reconcile` after the `status != open or presenting` return and before the
  `presented.boot == BOOT` return, so it runs on every scan of an already-presented retry. When it retires one,
  `handle` calls `release(rid)` and returns. The message edit follows on the next scan (`live` is a snapshot).
- A tap after retirement is refused (`gateway.answered` checks `status != "open"`, gateway.py:562).
