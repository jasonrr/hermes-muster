# Plan: Project status on launch (issue #3)

Approved design: four settings (`project_owner`, `project_number`, `project_status_field`,
`project_status_value`). The feature is on only when both owner and number are set. `core.project_status(repo, number)`
returns one line. It is called at the end of `core.relaunch()`, so a tick launch and a recover both run it.
The line becomes one card comment `project: <line>` and one printed (cron-log) line. Nothing in it can fail
the launch. One commit per task, on `muster/3`.

Setup: `uv venv .venv && uv pip install --python .venv/bin/python pytest` (docs/learnings.md: the system
python3 has no pytest). Verify commands are `.venv/bin/python -m pytest -q <file>`. The full suite is
`.venv/bin/python -m pytest -q`.

Real `gh` output shapes, read from `gh` 2026-10-08 against jasonrr's Project #1:
- `gh project view N --owner O --format json` → `{"id": "PVT_…", "number": N, …}`
- `gh project field-list N --owner O -L 100 --format json` → `{"fields": [{"id": "PVTSSF_…", "name": "Status",
  "type": "ProjectV2SingleSelectField", "options": [{"id": "47fc9ee4", "name": "In Progress"}, …]}, …], "totalCount": n}`
  (the default limit is 30 fields, hence `-L 100`)
- `gh project item-list N --owner O -L 300 --format json` → `{"items": [{"id": "PVTI_…", "content": {"url": …}}], "totalCount": n}`
  (`content.url` is what rc_intake.in_progress matched on, in production)

## Task 1: settings and their validation

- Files: `muster/config.py` (DEFAULTS gets the four keys, commented like their neighbours; `require()` appends
  `project_number` to `missing` when `project_owner` is set and the number is not a positive int (bool is
  refused the way `approver_id` refuses it), and appends `project_owner` when the number is set but the owner is
  empty or not a str). `tests/test_config.py`.
- Test first: `require()` raises ConfigError matching `project_number` for owner "o" with number 0, "5" or True.
  It raises matching `project_owner` for number 5 with owner "". It passes with both set, and with neither set.
  Follow `test_require_rejects_bad_approver_id` (parametrize plus `monkeypatch.setitem(config.settings, …)`).
- Verify: `.venv/bin/python -m pytest -q tests/test_config.py`
- Exists because: with a half-set pair, every launch would print a gh error on the card. A config error at
  `require()` fails the tick once, loudly, before any card exists (pattern: approver_id, branch_prefix).

## Task 2: move the status at the end of a launch

- Files: `muster/core.py` (new `project_status(repo, number)` beside `pull_requests`; one call at the end of
  `relaunch()` after the links comment); `tests/test_core.py` (`fake_world` gains a `project=None` param, a dict
  of the three JSON replies, answering `gh project view|field-list|item-list`; `item-edit` returns "").
- `project_status`: returns None when owner or number is unset (no gh call). Otherwise it runs view, field-list
  and item-list through `run()`. With no field named `project_status_field`, or no option named
  `project_status_value` on it, it returns "field/option not found". With no item whose `content.url` is
  `https://github.com/{repo}/issues/{number}`, it returns `not in Project #{n}`. Otherwise it runs `gh project
  item-edit --project-id <view.id> --id <item.id> --field-id <field.id> --single-select-option-id <opt.id>` and
  returns the status value. Mark the `-L 300` cap with a `# ponytail:` comment.
- In `relaunch()`, after the links comment:
  ```python
  try:
      moved = project_status(repo, number)
      if moved:
          kanban("comment", card, f"project: {moved}")
  except (CommandError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
      moved = f"failed: {' '.join(str(error).split())}"
      with contextlib.suppress(CommandError, OSError): kanban("comment", card, f"project: {moved}")
  if moved: print(f"{repo}#{number} project: {moved}")
  ```
  The exception tuple is the same one `launch()` catches. The comment carries the redacted CommandError text
  (`run()` redacts).
- Test first (`tests/test_core.py`, pattern: `test_an_approved_issue_gets_one_card…` with `fake_world` + `tick()`):
  (a) owner/number set, item present → exactly one `item-edit` whose four ids are the resolved ones, a card
  comment `project: In Progress`, and `tick() == 0`; (b) item absent → no item-edit, comment `project: not in
  Project #5`, prompt still sent, `tick() == 0`; (c) status value "Shipping" not an option → comment `project:
  field/option not found`; (d) `fail_on` the item-list → comment starts `project: failed:`, `tick() == 0`, no
  block; (e) defaults → no argv starting `["gh", "project"]` and no `project:` comment.
- Verify: `.venv/bin/python -m pytest -q tests/test_core.py`
- Exists because: this is the feature. It sits in `relaunch()` so that a recovered launch moves too. The design
  approval chose that.

## Task 3: README

- Files: `README.md` (four rows in the Configuration table after `worktrees`, plus one sentence under the
  table: the operator's `gh` needs the `project` scope, `gh auth refresh -s project`).
- Test first: none (docs).
- Verify: `grep -n "project_status_value\|auth refresh -s project" README.md`
- Exists because: the issue's "Done when" names it.

## Changes after the plan review

- Defaults: owner `""`, number `0`, field `"Status"`, value `"In Progress"`. When the feature is on, `require()`
  also rejects an owner, field or value that is not a non-empty str after `.strip()`. Tests use
  `monkeypatch.setitem`, not `config.settings.update`, so nothing leaks between tests.
- The item lookup reads `(item.get("content") or {}).get("url")`, because a draft item has no url. The URL
  comparison ignores case. Test (a) includes a draft item.
- The card comment sits outside the try that wraps `project_status`. A failed comment never turns a done move
  into "failed".
- Out of scope, named in the PR: `runs.relaunch` (ad-hoc runs). Also out: a recover after delivery re-sets the
  status, which was accepted in the design.
