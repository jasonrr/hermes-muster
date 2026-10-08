# Plan: automatic approvers (issue #2)

Approved design (2026-10-08): `auto_approvers`, a list that is empty by default. Each entry is
`{login, id, label?, repos}`. Ported from rc-intake's `rc_intake.automatic()`. The human approver
is unchanged. One commit per task, on `muster/2`.

Setup (docs/learnings.md: the system python3 has no pytest):
`uv venv .venv && uv pip install --python .venv/bin/python pytest`. Full suite: `.venv/bin/python -m pytest -q`.

Tests stub `core.run` with a `fake_run(argv)` that branches on argv (`tests/test_core.py` `fake_world`).
A test that needs other issues or events wraps `fake_world`'s fake the way `Recovery.__init__` does
(`tests/test_core.py` ~line 570): it answers the issue list and the timeline itself and passes the rest on.

## Task 1: config accepts and checks `auto_approvers`

- Files: `muster/config.py`, `plugin.yaml`, `README.md`.
  - `DEFAULTS["auto_approvers"] = []`, placed after `approver_id` and commented like its neighbours.
  - `AUTO_LABEL = "automatic-approval"` (module constant).
  - `require()`, after the `missing` check and after the `repos()` loop: `auto_approvers` must be a
    list. Each entry must be a dict with a non-empty str `login`, an `id` passing the same int test as
    `approver_id` (an int, not a bool, > 0), and `repos` a non-empty list whose every item, lowercased,
    is a key of `repos()` lowercased. `label`, when present, must be a non-empty str. Each failure raises
    `ConfigError(f"muster: auto_approvers[{i}]: <what>")`.
  - `plugin.yaml` config_schema: `auto_approvers: {type: list, default: [], description: ...}`.
  - README: a table row after `approver_id`, the example config shows the shape commented out,
    and the "One named human approves" bullet (line 17) names the exception.
- Test first (`tests/test_config.py`, `fresh_settings` fixture; base settings
  `approver_login="me", approver_id=7, repos=["o/a"]`): `[]` passes. A valid entry with and without
  `label` passes, and so does a repo given in another case (`O/A`). Parametrize the rejections: not a list;
  entry not a dict; missing login / id / repos; id `"7"`, `True`, `0`; repos `[]`; repos `["o/b"]` (not in
  `repos`); label `""`. Each raises `ConfigError` matching `auto_approvers`.
- Verify: `.venv/bin/python -m pytest -q tests/test_config.py`
- Exists because: the issue requires that `require()` reject a malformed entry. Without the check, a
  missing `id` crashes the tick with a KeyError on every issue.

## Task 2: `core.automatic()` and `core.approve()`

- Files: `muster/core.py`, next to `approval()`.
  - `automatic(repo, issue, events)` returns `(event, who)` for the first entry that matches, else None.
    `who = {"login", "id", "label"}`, with the label defaulted to `config.AUTO_LABEL`. An entry matches
    when all of these hold. (1) `repo.lower()` is in the entry's repos, lowercased. (2) `issue["user"]`
    has the entry's id and its login (any case). (3) the issue's labels include the entry's label (any
    case). (4) the newest `labeled` event for the main `label` and the newest one for the entry's label
    both exist, and the actor of each has the entry's id and login. The event returned is the newest
    main-label event.
  - `approve(repo, issue, events)` returns `(approval(events), None)` when the human approved, else
    `automatic(...)` or `(None, None)`.
- Test first (`tests/test_core.py`, the `labeled()` helper, `SENTRY = {"login": "sentry[bot]", "id": 39604003}`,
  set `config.settings["auto_approvers"]` with monkeypatch.setitem):
  - Bot-opened issue, bot made both label events → `(event of the main label, who)`.
  - Labels removed and re-added: the newest entry-label event is by JASON → None.
  - The repo is not in the entry's repos → None.
  - Two entries, the second matches → its `who`.
  - `auto_approvers=[]` → `approve` equals `(approval(events), None)` for the existing JASON and OTHER cases.
  - Issue opened by someone else, entry label gone from the issue, or the login matches but the id does not → None.
- Verify: `.venv/bin/python -m pytest -q tests/test_core.py -k "automatic or approve"`
- Exists because: this is the rule the issue names. `approve` is the single call that both
  `intake` and `recover_card` make, so the two can never disagree.

## Task 3: launch, brief, card and recover carry the automatic approval

- Files: `muster/core.py`.
  - `approved_by(repo, number, auto)` returns the human line,
    `"{approver_login} approved issue {repo}#{number} for work by labeling it `{label}`."`, or the
    automatic one: `"Issue {repo}#{number} was approved automatically. {login} (id {id}) opened it with
    labels `{label}` and `{auto label}`, under {approver_login}'s standing rule. {approver_login} did
    not label it."`
  - `brief(repo, number, bug, base="main", auto=None)`: the first sentence is `approved_by(...)`.
  - `card_argv(repo, issue, event, auto=None)`: when `auto` is set, the body gets the line
    `f"{approved_by(...)} Label event {event['id']}.\n"` before PROVENANCE.
  - `launch(repo, issue, card, event=None, auto=None)`: `record["auto"] = auto`, and
    `record["bug"] = bool(auto) or is_bug(issue)`.
  - `relaunch.prepare`: `brief(repo, number, record["bug"], rec["base"], record.get("auto"))`.
    The `.get` is there because launch.json records written before this change have no `auto`.
  - `intake`: `event, auto = approve(repo, issue, timeline(repo, number))`. The skip line says
    `... is not {approver_login}'s and no auto_approvers entry approves it`. The dry-run line says
    `would launch automatically` when `auto` is set and prints bug for it. `card_argv(..., auto)` and
    `launch(..., event, auto)` are passed it.
  - `recover_card`: `current, auto = approve(repo, issue, timeline(repo, number))`, then the existing
    event-id comparison, then `record["auto"] = auto` before `relaunch`.
- Test first (`tests/test_core.py`):
  - `brief(..., auto=who)` says "approved automatically", "did not label it" and "This issue is a bug."
    (with bug True), and does not say "jasonrr approved".
  - `card_argv(..., auto=who)` has a body containing "approved automatically" and "Label event 10.".
    Without `auto`, it does not.
  - End to end: wrap `fake_world` so that issue 397 is `{"user": SENTRY, "labels": [agent-ready,
    automatic-approval]}` and its timeline is both label events by SENTRY. `tick() == 0`, the submitted
    brief says "approved automatically" and "This issue is a bug.", and launch.json has
    `auto == who` and `bug is True`.
  - Dry run with the same world prints "would launch automatically".
  - Recover: build `Recovery` with an auto-approved issue (a parametrized variant of the fixture, or a
    new fixture subclass that sets `issue`/`events`). `recover("t_abc123") == 0` and the resent brief
    still says "approved automatically".
- Verify: `.venv/bin/python -m pytest -q tests/test_core.py`
- Exists because: the issue requires the bug briefing, attribution that never says the human labeled
  it, and the label event id on the card. Recover rewrites the brief, so it has to keep the attribution.

## Last: full check

`.venv/bin/python -m pytest -q`, then `hermes plugins validate .` (the issue's done-when).

## Fixes from the plan attack (2026-10-08)

- Issue shape: the REST list and `gh api repos/X/issues/N` both give `user: {login, id}` and
  `labels: [{name}]`. `automatic` reads `(issue.get("user") or {})`, because `fake_world`'s list items have no `user`.
- `require()`: the auto check goes after the `repos()` loop and before `wf = workflow_path()`.
  `isinstance(list)` is checked first (a bare YAML key is None). plugin.yaml's description is quoted.
  A comment says config.py is the only check, since the schema cannot type nested entries.
- Card body: the auto line goes between the URL line and PROVENANCE, so `endswith(PROVENANCE)` still holds.
- `who` is a plain dict, so `record["auto"] == who` survives the JSON round trip.
- Extra Task 2 tests: the returned event is the main-label one even when the two ids differ; entry
  label matching ignores case; the human labeled first and the bot relabeled later → automatic.
- Task 3 recover: a `Recovery` subclass, `AutoRecovery`, sets `issue`/`events` to the bot's, and its `run`
  wrapper also answers `/issues?` with that issue so the first `tick()` launches it.
- Accepted: `rec["text"]` is fixed at first build. A recover keeps the same label event (it is checked),
  so the approval it describes is unchanged; only a config edit could make it stale.
- README: a bot re-applying the main label makes a new card (as a human relabel does). Re-applying
  only its own label does not.
