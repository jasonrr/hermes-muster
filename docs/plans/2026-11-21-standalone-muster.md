# Standalone muster: plan

- **Goal:** a `muster` command that runs with no Hermes installed (own settings file, own file ledger, command-line
  decisions), callable by any harness (Hermes, MausBot, Claude Code, Codex). `hermes muster` behaves exactly as today.
- **Design:** `docs/decisions/2026-11-21-standalone-muster.md`
- **Branch:** `feat/standalone-muster`
- **Verify (whole feature):** `"${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}" -m pytest -q`
  (system `python3` has no pytest: docs/learnings.md line 5). Below, `$PY` means that interpreter.

## Rules for every task

- **One switch.** `muster.config.standalone` (module-level bool, default `False`). It is `True` only when the process
  was started through `cli.standalone()` (Task 1). Every new branch reads this flag. With the flag `False`, no line of
  behaviour may change, except the two text changes Tasks 3 and 4 record: the existing suite must pass without
  edits, except where a task names the test file it edits.
- **No Hermes import or `hermes` argv when the flag is `True`.** Task 7's test enforces it.
- Tests are behaviour tests against the public functions, with `tmp_path` state, in the style of `tests/test_runs.py`.
  Tests reach subprocesses only through `core.run` (the one seam, `muster/core.py:151`). No source-reading tests.
- Free text placed after options goes after `--` (docs/learnings.md line 17).
- Do not touch `muster/hermes_private.py`, `muster/channels/`, `muster/hosts/`.
- Already known (do not re-derive): `tests/test_hermes_private.py` can fail on unchanged main when the local Hermes
  checkout drifts (docs/learnings.md line 234). Run it on `origin/main` before blaming the diff.

## Task 1
**Standalone entry point and settings file**

- **Exists because:** without it there is no way to start muster outside `hermes muster`, and `config.load` needs a
  Hermes plugin context.
- **Model:** sonnet
- **Files:** `muster/config.py`, `muster/cli.py`, `muster/core.py`, `muster/launcher.py`, `pyproject.toml`,
  new `tests/test_standalone.py`.
- **Produces for later tasks:** `config.standalone`, `config.home()`, `config.load_file()`, `config.muster_bin()`,
  `core.muster_cmd()`, `cli.standalone(argv=None)`.

Steps (write the tests in `tests/test_standalone.py` first; each bullet below is one test, then its code):

1. `config.py`: add `standalone = False`. Add
   ```python
   def home() -> Path:
       """Standalone muster's home: $MUSTER_HOME, else ~/.muster."""
       return Path(os.environ.get("MUSTER_HOME") or "~/.muster").expanduser()

   def load_file():
       """Standalone settings: <home>/config.toml, the same keys as plugin.yaml's config_schema."""
       import tomllib
       global standalone
       standalone = True
       path = home() / "config.toml"
       try:
           data = tomllib.loads(path.read_text())
       except FileNotFoundError:
           raise ConfigError(f"muster: no settings file at {path}; see docs/setup.md#standalone") from None
       except tomllib.TOMLDecodeError as error:
           raise ConfigError(f"muster: {path}: {error}") from None
       unknown = sorted(set(data) - set(DEFAULTS))
       if unknown:
           raise ConfigError(f"muster: {path}: unknown settings {unknown}")
       for key, default in DEFAULTS.items():
           settings[key] = data.get(key, default)

   def muster_bin() -> str:
       return shutil.which("muster") or str(Path(sys.argv[0]).resolve())
   ```
   (`import sys` at the top.) Tests: a file with `repos`, `approver_login`, `approver_id` loads; a missing file, bad
   TOML and an unknown key each raise `ConfigError`; tests set `MUSTER_HOME` to `tmp_path` with `monkeypatch.setenv`
   and restore the flag with `monkeypatch.setattr(config, "standalone", False)` before calling `load_file`.
2. `config.data_dir()`: first lines become
   `if standalone: p = home() / "data"; p.mkdir(parents=True, exist_ok=True); return p`. Test: with the flag on, the
   path is `<MUSTER_HOME>/data` even when `plugins.plugin_storage` is importable.
3. `config.require()`: the last check (`cannot find the hermes executable`, `config.py:118`) runs only when
   `not standalone`. Test: flag on, `hermes` absent from PATH, valid settings: `require()` does not raise.
4. `core.py`:
   - `operating_profile()`: first line `if config.standalone: return "default"`.
   - Add
     ```python
     def muster_cmd():
         """How this muster is called from a shell: `muster` standalone, `hermes [-p profile] muster` under Hermes."""
         return [config.muster_bin()] if config.standalone else [*hermes_cmd(), "muster"]
     ```
     and change `hook_cmd()` to `return [*muster_cmd(), "hook"]`. Test: flag off, `hook_cmd()` is unchanged
     (`[<hermes>, "muster", "hook"]`); flag on, it is `[<muster bin>, "hook"]` and `hermes_cmd` is never called.
   - `prepare_env()`: unchanged (it only sets env).
5. `launcher.pane_env()` (`muster/launcher.py:28`): when `config.standalone`, the first pairs are
   `[f"MUSTER_HOME={config.home()}", "MUSTER_PANE=1"]` in place of the `HERMES_HOME`/`HERMES_KANBAN_HOME` pairs, and
   `core.hermes_home()` is not called; the `gh_config_dir` pairs stay. When not standalone, append nothing new.
   Test both modes.
6. `cli.py`: add
   ```python
   def standalone(argv=None):
       """The `muster` console command: the same subcommands as `hermes muster`, on the files backend."""
       import argparse
       parser = argparse.ArgumentParser(prog="muster", description="Human-directed coding agents in terminal panes")
       setup(parser)
       return main(parser.parse_args(argv), None)
   ```
   In `main`, replace `config.load(ctx)` with `config.load_file() if ctx is None else config.load(ctx)`. Test:
   `cli.standalone(["tick"])` with `muster.intake` stubbed as `tests/test_cli.py:45-49` does returns the stub's code
   and leaves `config.standalone is True` (reset it with monkeypatch in the test).
7. `pyproject.toml`: add
   ```toml
   [build-system]
   requires = ["setuptools>=68"]
   build-backend = "setuptools.build_meta"

   [project.scripts]
   muster = "muster.cli:standalone"

   [tool.setuptools]
   packages = ["muster", "muster.channels", "muster.hosts"]
   ```
   The supported install is editable (`uv tool install --editable <checkout>`), because `config.PROMPTS` is
   `<checkout>/prompts` (`config.py`, `PROMPTS = Path(__file__).resolve().parent.parent / "prompts"`). No test; Task 6
   documents it. Then run `hermes plugins validate .` and confirm it still passes.

**Corrections (binding; where they differ from the text above in this task, these win):**

- `load_file()` also forces `settings["notify_topics"] = False` (no chat exists standalone).
- `decisions.gateway_up()` (`decisions.py:36`): first line `if config.standalone: return True`. Reason: its three
  callers (`bridge.py:97`, `events.py:124`, `conversation.py:138`) then treat muster as able to present requests
  itself, which it is (through `muster decisions`): subagent permission prompts become requests, and wait cards take no
  subscription. Add `muster/decisions.py` to this task's files; test both modes.
- Add `config.muster_name()`: `"muster" if standalone else "hermes muster"` (for usage and error text; Task 3 uses it).
- Test hygiene, for every test in this plan that calls `load_file` or `cli.standalone`: write
  `<MUSTER_HOME>/config.toml` from the sandbox values in `tests/conftest.py` (`repos` as `owner/name=<clone>` strings,
  `worktrees`, `gh_config_dir`, `approver_login`, `approver_id`); first
  `monkeypatch.setattr(config, "settings", dict(config.settings))` so the overwrite does not leak; reset the flag with
  an autouse fixture (`monkeypatch.setattr(config, "standalone", False)`); and
  `monkeypatch.setattr(config, "muster_bin", lambda: "/bin/muster")`, because under pytest `sys.argv[0]` contains
  `.hermes`. Put these in one fixture `standalone_home` in `tests/conftest.py` (not autouse) and use it in Tasks 1-7.
- `pane_env` tests that pin the exact list (`tests/test_core.py:626,635,798`, `tests/test_runs.py:587`,
  `tests/test_operating_profile.py:162`) must pass unedited with the flag off.
- Manual check after step 7 (report its output): `uv tool install --editable . && cd /tmp && muster --help`.

- **Verify:** `$PY -m pytest -q tests/test_standalone.py tests/test_cli.py tests/test_config.py tests/test_core.py tests/test_launch.py tests/test_operating_profile.py`

## Task 2
**File ledger behind `core.kanban()`**

- **Exists because:** the ledger is `hermes kanban`; without a replacement nothing can run with no Hermes.
- **Model:** sonnet
- **Files:** new `muster/ledger.py`, `muster/core.py`, `muster/runs.py`, `muster/launcher.py`, `muster/intake.py`,
  `tests/test_kanban_contract.py`.
- **Consumes:** `config.standalone`, `config.data_dir()` (Task 1). **Produces:** `ledger.call(board, args) -> str`,
  `ledger.last_event(board, card) -> int`, `ledger.block_kind(board, card) -> str | None`,
  `ledger.cards(board) -> list[dict]` (Task 4 uses it).
- **Follows:** the file store in `muster/decisions.py:62-107` (one JSON file per record, `core.save_json` for the
  atomic write, `fcntl.flock` on a sibling lock file).

`ledger.py` contract (the same argv and JSON as `hermes kanban --board <b> ...`; `args` is everything after the board):

- Storage: `<data dir>/ledger/<board>/<card id>.json`; one lock file `<data dir>/ledger/<board>/.lock`, taken
  exclusively for every call. Card record:
  `{"id", "title", "body", "status", "assignee": None, "created_at": int(time.time()), "created_by", "key",
  "block_kind": None, "kinds_blocked": [], "events": 0, "comments": [{"body", "created_at"}], "subs": [...]}`.
  Card id: `"t_" + secrets.token_hex(4)`.
- Parse `args` with `argparse` (one subparser per verb, `exit_on_error=False`), so `--` and option order behave as
  the real CLI. Any parse failure, unknown card or refused move raises `core.CommandError` with a one-line message;
  an unknown card's message contains `no such task`; an unknown subscription's contains `no such subscription`.
- Verbs and rules (each is proved by a named test in `tests/test_kanban_contract.py`):
  - `create [--body B] [--idempotency-key K] [--created-by W] [--json] -- <title>`: with a key, return the existing
    card with that key whose status is not `archived` (docs/learnings.md line 93), even when `done`; else a new card,
    status `ready`. Prints the card record as JSON.
    `# ponytail: key lookup scans every card file; add an index file when a board passes ~10k cards`
  - `show <card> [--json]`: prints `{"task": <record without comments and subs>, "comments": [...]}` (always JSON).
  - `comment [--] <card> <text>`: appends; any status; no event.
  - `block [--kind K] [--] <card> <reason>`: only from `ready` (else refuse). If `K` is set and already in
    `kinds_blocked`: status `triage`. Else status `blocked`, `block_kind = K`, append `K` to `kinds_blocked` when set,
    `events += 1`. The reason is appended as a comment so `show` carries it.
  - `unblock <card>`: only from `blocked`; status `ready`, `block_kind = None`.
  - `complete <card> --summary S`: only from `ready` or `blocked`; status `done`; `events += 1`.
  - `archive <card>`: from any status but `archived`; status `archived`.
  - `notify-subscribe <card> --platform P --chat-id C [--thread-id T] [--user-id U] [--chat-type Y]
    [--notifier-profile N] [--delivery-mode M]`: one row per (platform, chat_id, thread_id); a second call for the
    same key updates only the options given. New rows carry `last_event_id: 0, last_ping_event_id: 0`.
  - `notify-unsubscribe <card> --platform P --chat-id C [--thread-id T]`: remove the row, else `no such subscription`.
  - `notify-list <card> [--json]`: the rows as a JSON list.
- `last_event(board, card)`: the card's `events` (0 for an unknown card). `block_kind`: the field.
  `cards(board)`: every record, oldest first.

Wiring (all guarded by `config.standalone`; the `else` path is today's code, untouched):

- `core.kanban(*args)`: `if config.standalone: from . import ledger; return ledger.call(config.settings["board"], list(args))`.
- `core.board_exists()`: return at once when standalone (the directory is made on first use).
- `runs.last_event(card)` (`runs.py:81`): standalone returns `ledger.last_event(config.settings["board"], card)`.
- `runs.acked(card)` (`runs.py:89`): standalone returns `True` (no notifier exists; nobody waits for a ping).
- `launcher.block_kind(card)` (`launcher.py:470`): standalone returns `ledger.block_kind(...)`.
- `core.notify_target()` (`core.py:216`): standalone returns
  `{"chat_id": "local", "user_id": "local", "chat_type": "dm"}` before anything else, so `core.subscribe` works
  unchanged and stores its row in the ledger.
- `intake.py:381`: `core.run(card_argv(...))` becomes `core.kanban(*card_argv(...)[4:])`. `card_argv` itself is
  unchanged, so tests that check its value still pass, and under Hermes the argv that reaches `core.run` is identical.

Tests: in `tests/test_kanban_contract.py`, remove the module-level `pytestmark` and make the `k` fixture
`params=["hermes", "files"]`. The `hermes` param keeps today's body and calls
`pytest.skip("hermes not installed: kanban contract NOT checked")` when the binary is missing. The `files` param does
`monkeypatch.setattr(config, "standalone", True)`, `monkeypatch.setenv("MUSTER_HOME", str(tmp_path))`, and returns
```python
def run(*args, ok=True):
    try:
        out = ledger.call("muster", list(args))
    except core.CommandError as error:
        assert not ok, (args, str(error))
        return ""
    assert ok, (args, out)
    return out
```
Every existing test must pass on both params unedited, with one exception:
`test_board_exists_reads_the_board_list_from_the_real_cli` gets `if request.param ...` skip for `files` (expose the
param through the fixture, e.g. `run.backend = request.param`). If a test fails only on `files`, fix `ledger.py`, never
the test. Add two files-only tests: a card written by one `ledger.call` is read by a fresh call (persistence), and an
archived card's key makes a new card.

**Corrections (binding; where they differ from the text above in this task, these win):**

- Parser: subclass `argparse.ArgumentParser` with `def error(self, message): raise core.CommandError(message)` and
  use that class for the root parser and every subparser (pass `parser_class=`). `exit_on_error=False` alone still
  raises `SystemExit` for missing or unknown arguments on Python 3.11. Add a test: a bad argv raises `CommandError`.
- `core.notify_target()`: standalone returns the `local` dict only when `notify_chat_id` is empty (in place of the
  `channels...home()` call, `core.py:224-227`); a configured `notify_chat_id` behaves as today.
- `test_subscribe_reads_back_...`: on the `files` backend, skip the `target=None` param (it reads Telegram's home);
  the group param must pass unedited. This and `board_exists` are the only two skips.
- `launcher.block_kind` standalone: `ledger.block_kind(config.settings["board"], card)`.
- `notify-subscribe` defaults for a new row: `notifier_profile="default"`, `delivery_mode="notify+wake"`,
  `user_id`/`chat_type`/`thread_id` `None`.
- `ledger.cards(board)` sorts by `(created_at, file mtime)`.
- This task supersedes the design's "the notify verbs do nothing": rows are stored, so `core.subscribe`'s read-back
  works unchanged.

- **Verify:** `$PY -m pytest -q tests/test_kanban_contract.py tests/test_runs.py tests/test_core.py tests/test_launch.py tests/test_events.py`

## Task 3
**Briefs and prompts name the right command**

- **Exists because:** every builder brief and coordinator skill tells the agent to run `hermes muster ...`; standalone
  that command does not exist, so hooks and `done`/`propose` would fail in the pane.
- **Model:** sonnet
- **Files:** `muster/config.py`, `muster/intake.py`, `muster/adhoc.py`, `muster/events.py`, `muster/hold.py`,
  `muster/decisions.py`, `muster/core.py` (`PROVENANCE`), `prompts/*.md`, `prompts/README.md`, `tests/test_prompts.py`,
  `tests/golden/`.
- **Consumes:** `config.standalone`, `config.muster_bin()`.

Steps:

1. `config.py`: add
   ```python
   def muster_cli() -> str:
       """The command a prompt tells an agent to type: `<muster>` standalone, `<hermes> muster` under Hermes."""
       return muster_bin() if standalone else f"{hermes_bin()} muster"
   ```
   Test (in `tests/test_prompts.py`, beside the existing `HERMES` monkeypatch at line 24): both modes.
2. In `prompts/*.md`, replace every `$hermes muster` with `$muster`, and every literal `hermes muster` in
   `escalation.md` and `dispatch.md` with `$muster` **only if that file is rendered through `config.prompt`**. Check
   first: `escalation.md`, `dispatch.md` and `review-standard.md` are registered whole as Hermes skills
   (`__init__.py:17-20`, "Hermes serves the registered file whole"), so they are NOT templates. For those three, keep
   the literal text and add one sentence under the first heading of each:
   "Outside Hermes the command is `muster` (not `hermes muster`), and `muster decisions list` replaces the kanban
   wake." Do not otherwise edit them.
3. Call sites that fill `hermes=`: `intake.py:100-101`, `adhoc.py:185,188`, `events.py:557`, `hold.py:158-162`,
   `decisions.py:924`. Each passes `muster=config.muster_cli()` in place of `hermes=config.hermes_bin()`, and
   `hook=f"{config.muster_cli()} hook"` in place of `f"{config.hermes_bin()} muster hook"`. If a template still needs a
   bare `$hermes` for something that is not `muster` (grep `\$hermes` in `prompts/` after step 2), stop and report it
   instead of guessing.
4. Text that prints a command for a human: `adhoc.py:264`, `hold.py:169,222` use
   `shlex.join([*core.muster_cmd(), "recover", ...])`; under Hermes the output is byte-identical to today.
5. `core.PROVENANCE` (`core.py:29`): leave the constant; it is Hermes-mode text. Standalone cards may carry it too
   (cosmetic; cut).
6. Goldens: under Hermes mode the rendered briefs must be byte-identical, so `tests/golden/` must NOT change. Run
   `$PY -m pytest -q tests/test_prompts.py` WITHOUT `MUSTER_REGOLD`; a golden diff means a call site changed Hermes
   output: fix the code, not the golden. Add one standalone test: `intake.brief(...)` rendered with the flag on
   contains `<muster bin> hook` and no `hermes`.

**Corrections (binding; where they differ from the text above in this task, these win):**

- More call sites: `adhoc.py:402` and `events.py:557` pass a command string to `events.gate`; both become
  `f"{config.muster_cli()} hook [--card {card}] propose <file>"` (keep each site's own `--card` part; `adhoc.py:188`
  also keeps its `--card {run['card']}` suffix). Under Hermes these render the same bytes as today
  (`tests/test_runs.py:513` must pass unedited).
- Usage and error text (`events.py:356,466,538`, `adhoc.py:264`): replace the literal `hermes muster` with
  `{config.muster_name()}`. Under Hermes the text is byte-identical (`tests/test_events.py:754` passes unedited).
  Step 4's `shlex.join` applies only to `hold.py:169,222`.
- Skill files: `escalation.md` and `dispatch.md` get this sentence under the first heading; `review-standard.md` is
  not edited: "Outside Hermes: type `muster` where this says `hermes muster`, and `muster ledger` where it says
  `hermes kanban --board <board>`; `muster decisions list` replaces the kanban wake. Never run
  `muster decisions answer` to merge, approve or allow unless the human asked for that in this conversation."
  This sentence is a Hermes-visible text change, accepted and recorded here (the only one in this task).
- Standalone tests assert the rendered text contains `/bin/muster hook` and not the string `hermes muster`.

- **Verify:** `$PY -m pytest -q tests/test_prompts.py tests/test_startup_prompt.py tests/test_hold.py tests/test_events.py tests/test_decisions.py`

## Task 4
**`muster decisions list | show | answer`**

- **Exists because:** standalone has no Telegram or Slack; without these commands no question, permission prompt,
  design or pull request can be decided, and the coordinator cannot learn a review is due.
- **Model:** sonnet
- **Files:** new `muster/inbox.py`, `muster/cli.py`, `muster/core.py` (`display`), `prompts/issue-dispatch.md` and
  `prompts/ad-hoc-run.md` (one forbidden-list line), new `tests/test_inbox.py`.
- **Consumes:** `decisions.open_requests`, `decisions.load`, `decisions.read_all`, `decisions.answer_part`,
  `decisions.transition`, `decisions.execute` (`decisions.py:72-158,511`); `gateway.shape`, `gateway.compose`
  (`gateway.py:336-357`, pure; importing `muster.gateway` imports no Hermes module); `ledger.cards` (Task 2).

CLI (register in `cli.setup` and `SUBCOMMANDS`: `"decisions": ("inbox", "main")`):

```
muster decisions list [--json]
muster decisions show <id> [--json]
muster decisions answer <id> (--choice LABEL ... | --text TEXT | --allow | --deny [--message M])
```
These three work on both backends (they read the same request files); `list`'s `reviews` part is standalone only.

- `list`: `requests` = every request from `decisions.open_requests()` with status `open`:
  `{"id", "kind", "ledger", "run": req.get("run"), "questions": [...], "choices": [...], "actions": req.get("actions"),
  "recommended": req.get("recommended"), "card": req.get("card")}`.
  `reviews` (only when `config.standalone`) = work the coordinator must review, from `ledger.cards(board)`:
  - a card whose `key` starts with `design:` (format `design:<ledger>:v<n>:<sha>`, `events.py:263-267`) and for which
    no request in `decisions.read_all(False) + decisions.read_all(True)` has `kind == "design"`, the same `ledger`
    and `proposal == {"version": n, "sha": sha}` → `{"kind": "design", "ledger", "proposal": f"v{n}:{sha}", "card"}`;
  - a card whose `key` starts with `review:` (format `review:<ledger>:<head>`, `decisions.py:232-236`) and for which
    no `build` request has that `ledger` and `head` → `{"kind": "build", "ledger", "head", "card"}`;
  - a card with status `done` that some run names as its ledger (a directory `<data dir>/runs/<card>/` or
    `<data dir>/intake/<card>/` exists) and for which no `build` request with that `ledger` exists at all →
    `{"kind": "build", "ledger", "card"}`.
  Text output: one line per entry, then `Next: muster recommend-design <ledger> --proposal ...` /
  `muster recommend <ledger> ...` as a hint line. `--json` prints `{"requests": [...], "reviews": [...]}`.
- `show`: the whole request (`decisions.load`); unknown id → message on stderr, exit 2.
- `answer`:
  - Refuse (stderr, exit 2) when `os.environ.get("MUSTER_PANE")`: "a builder pane cannot answer its own request".
    Threat this stops: the unsandboxed builder approving its own design, permission prompt or merge by mistake. It is a
    guard rail, not a sandbox; the brief's forbidden list is the stated control (README, Safety).
  - `permission`: `--allow` → `decisions.transition(rid, ("open",), "answered", answer={"decision": "allow"})`;
    `--deny` → `answer={"decision": "deny", **({"message": M} if M else {})}`. Other flags on a permission, or
    `--allow`/`--deny` on another kind → exit 2.
  - other kinds: `--choice` is given once per question, in order (a multi-select question takes its labels as one
    JSON array string, as `gateway.shape` expects); `--text` answers question 0 with free text. For each question
    `n`: `part = gateway.shape(req, n, value)`, then
    `decisions.answer_part(rid, n, part, len(req["questions"]), lambda parts: gateway.compose(req, parts))`.
    A label that is not one of `req["choices"][n]` with `--choice` → exit 2, nothing recorded (check all before the
    first `answer_part`). The count of `--choice` must equal the count of questions.
  - When the last `answer_part` returns `"completed"` and `req["kind"] in ("build", "feedback", "design")`: call
    `decisions.execute(rid, f"cli:{os.getpid()}")` in this process, then print the request's final `status` and
    `outcome`.
  - `"closed"`/`"duplicate"` or a failed transition: print "already answered or ended", exit 1.
- `core.display` (`core.py:134-140`): wrap the `agent.redact` import; on `ImportError` return
  `SECRET.sub("[redacted]", text)`. Without this, `bridge.wait` (`bridge.py:83,125-131`) swallows the ImportError and
  no permission request is ever created standalone.
- Add to the forbidden list in `prompts/issue-dispatch.md` and `prompts/ad-hoc-run.md`: "Do not run
  `muster decisions`." This changes two goldens: regenerate them with `MUSTER_REGOLD=1 $PY -m pytest -q
  tests/test_prompts.py` and check `git diff tests/golden` shows only that line (docs/learnings.md line 186).

Tests (`tests/test_inbox.py`, flag on, `MUSTER_HOME=tmp_path`): a question request made with `decisions.create` is
listed, answered by `--choice`, and reads back `answered` with `answer == {question_text: label}`; a wrong label
records nothing; a permission `--deny --message` reads back; `MUSTER_PANE=1` refuses; a `build` request answered
`--choice "Leave PR open"` ends `done` (this path needs no gh; `decisions.py:556-558`); a `design:` ledger card with
no matching request is in `reviews`, and leaves it once a matching design request exists; `display` redacts a
`ghp_` token with `agent.redact` unimportable (`monkeypatch.setitem(sys.modules, "agent.redact", None)`).

**Corrections (binding; where they differ from the text above in this task, these win):**

- `answer` works only when `config.standalone`. Under Hermes it prints "answer in your chat channel" and exits 2:
  there the channel's approver check is the control, and a pane could otherwise merge with the human's gh login.
  `list` and `show` work on both backends.
- Request ids from the command line must match `^[0-9a-f]{16}$` before any file access; else exit 2.
- Parser: nested subparsers with `dest="decisions_command"`; `--choice` is `action="append"`; `--choice`, `--text`,
  `--allow`, `--deny` are one mutually exclusive group (`--message` only with `--deny`); `show` always prints JSON
  (no `--json` flag). `--text` is allowed only on a request with exactly one question; docs say to write
  `--text=<words>` when the words start with `-`.
- `--choice` validation for a multi-select question (`q["multi"]`) whose value starts with `[`: parse the JSON array
  and check each label against `req["choices"][n]`.
- Before recording any answer: (a) `question`/`permission` older than `gateway.ALIVE_MAX` by the `alive` rule in
  Task 5 → stale it with that same transition, print it, exit 1; (b) `build`/`feedback`: `why = decisions.obsolete(req)`;
  if set → `decisions.transition(rid, ("open",), "stale", outcome=f"Not shown: {why}")`, print it, exit 1.
- `reviews` rules, replacing the three above. Skip every card whose status is `archived`.
  - design: `design:` card, and `n == (events.versions(ledger) or [0])[-1]`, and no `design` request (open or
    archived) with that ledger and proposal, and no `question` request from `decisions.for_ledger(ledger, "question")`
    with that proposal whose status is not `open` (the human answered the builder directly).
  - build re-review: `review:` card with no `build` request for that ledger and head.
  - build first review: card status `done`, `<data dir>/runs/<card>/run.json` or `<data dir>/intake/<card>/launch.json`
    exists (the file, not the directory: docs/learnings.md line 36), the run is not closed
    (`run.json` has no `"closed": true`), and no `build` request for that ledger exists.
  Each build entry also carries `run = decisions.run_of(ledger)` inside a `try` (omit on error), so the caller has the
  branch and worktree to find `--pr` and `--head`.
- `list` gets a third part, `blocked` (standalone only): every ledger card with status `blocked`:
  `{"card", "title", "block_kind", "reason": <last comment body>}`. Launch failures, cleanup warnings, Activate cards
  and plain waits reach nobody otherwise. Text mode prints request and card text with `json.dumps` quoting.
- New subcommand `muster ledger <args...>` (`argparse.REMAINDER`), standalone only (else exit 2 with "use hermes
  kanban"): prints `core.kanban(*args)`. The coordinator skill needs `show`, `comment` and `complete`.
  Refused when `MUSTER_PANE` is set, as `answer` is.
- The forbidden-list line is exactly: "Do not run muster's `decisions` or `ledger` commands." It has no placeholder.
  It changes four goldens (`tests/golden/issue-bug-human-bot.txt`, `issue-feature-auto-nonote.txt`,
  `issue-feature-human-nobot.txt`, `ad-hoc-footer.txt`); `git diff tests/golden` must show one added line in each and
  nothing else. This is the second and last accepted Hermes-visible text change.
- `core.display` fallback supersedes the design's "shows the text as is": GitHub token shapes are still redacted.
- Tests added: `answer` under Hermes mode exits 2 and records nothing; a bad id exits 2; a superseded design card is
  not in `reviews`; a `blocked` card is listed; `muster ledger show <card>` prints the card.
- Citation fix: the store helpers are `decisions.py:62-160`; `execute` is `decisions.py:511`.

- **Verify:** `$PY -m pytest -q tests/test_inbox.py tests/test_cli.py tests/test_prompts.py tests/test_bridge.py tests/test_core.py`

## Task 5
**Housekeeping in `muster flush` when no gateway runs**

- **Exists because:** only the gateway scan settles `executing` design requests, stales requests whose pane hook died
  and recovers a crash between answer and execute (`gateway.py:67-123,197-202,280-284`). Standalone has no gateway,
  so a design approval given before the builder asked would never be delivered.
- **Model:** sonnet
- **Files:** `muster/inbox.py` (add `sweep()`), `muster/runs.py` (`flush`), `tests/test_inbox.py`.

`inbox.sweep()`, called at the end of `runs.flush` (inside the flush lock) only when `config.standalone`; each request
in its own `try`, errors logged with `core.log("decisions", ...)`:

- `question`/`permission`, status `open` or `answered`, `time.time() - req.get("alive", req.get("created_at", 0)) >
  gateway.ALIVE_MAX` → `decisions.transition(rid, ("open", "answered"), "stale", outcome="Ended: the builder stopped
  waiting. Nothing was sent.")` (the same call as `gateway.py:199-200`).
- `design`, status `executing` → `decisions.settle_design(req)`.
- `build`/`feedback`/`design`, status `answered` → `decisions.execute(rid, f"cli:{os.getpid()}")`.
- `build`/`feedback`, status `executing`, `executing_boot` of the form `cli:<pid>` and that pid is not alive
  (`os.kill(pid, 0)` raises `ProcessLookupError`) → `decisions.recover(req)`. A live pid, or any other boot value, is
  left alone (an `answer` command is still running it).

Tests: a stale question goes `stale`; a fresh one stays `open`; an `answered` build with answer
`{"action": "nothing"}` ends `done`; an `executing` request with a live pid (`os.getpid()`) is untouched; with the
flag off, `flush` never calls `sweep` (patch it to raise).

**Corrections (binding; where they differ from the text above in this task, these win):**

- `sweep()` does NOT run inside `flush.lock`. `runs.flush` calls it after the `with open(... "flush.lock")` block
  ends, and `sweep` takes its own non-blocking lock `<data dir>/decisions/sweep.lock` (busy → return). `execute` can
  take 10 s or more (`_send`, `_merge`) and must not stall event delivery.
- `sweep` also stales an `open` `build`/`feedback` request when `decisions.obsolete(req)` returns a reason (the same
  transition as Task 4).
- `answer` (Task 4) and `sweep` may both call `decisions.execute` for one request. That is safe: every effect starts
  with an atomic `transition(("answered",), ...)` (`decisions.py:551-576`). Do not add a lock for it.
- `# ponytail: a cli:<pid> whose pid was reused after a reboot stays executing; add a start-time check if seen`.

- **Verify:** `$PY -m pytest -q tests/test_inbox.py tests/test_runs.py`

## Task 6
**Docs and repository rules**

- **Exists because:** `AGENTS.md` and the invariants lens forbid a reimplemented Hermes feature, which the file ledger
  now is by approved design; left unchanged, every reviewer flags the feature p1. And no user can install standalone
  without setup text.
- **Model:** sonnet
- **Files:** `AGENTS.md`, `docs/review-lenses.md`, `README.md`, `docs/setup.md`, `docs/architecture.md`,
  `docs/reference.md`, `plugin.yaml` is NOT edited.

Steps:

1. `AGENTS.md`: retitle the section "Hermes is one host". Keep every existing rule for code that runs under Hermes.
   Add: muster has two backends chosen by `config.standalone`; the files backend (`muster/ledger.py`,
   `muster/inbox.py`) exists by decision `docs/decisions/2026-11-21-standalone-muster.md`; `ledger.py` must pass
   `tests/test_kanban_contract.py` on both params; no other Hermes feature may be reimplemented.
2. `docs/review-lenses.md`, invariants lens: after "a reimplemented Hermes feature" add "(the files ledger and the
   decisions CLI are the approved exceptions; p1 = they drift from the kanban contract test)".
3. `docs/setup.md`: new section `## Standalone` (anchor `#standalone`, named by `config.load_file`'s error): install
   `uv tool install --editable <checkout>`; `~/.muster/config.toml` example with `repos`, `approver_login`,
   `approver_id`, `host`; `MUSTER_HOME`; the three timers as one crontab block
   (`* * * * * muster tick`, `* * * * * muster flush`, `*/5 * * * * muster cleanup`); what is absent (Telegram/Slack,
   topics, the duplicate check, `auto_approvers` launch without a duplicate check). Do not revert or reformat other
   parts of the file.
4. `docs/reference.md`: `muster decisions` commands and JSON shapes; `docs/architecture.md`: add `ledger` and `inbox`
   to the module map and a "Two backends" paragraph.
5. `README.md`: first paragraph says muster runs as a Hermes plugin or standalone; a short "From another harness"
   section: Claude Code / Codex / MausBot call `muster launch`, poll `muster decisions list --json`, answer with
   `muster decisions answer`, review with `muster recommend`; the coordinator skills are `prompts/escalation.md`,
   `prompts/dispatch.md`, `prompts/review-standard.md`, copied or symlinked into the harness's skills directory
   (Claude Code: `~/.claude/skills/<name>/SKILL.md`). State that MausBot's skills location is unverified (open
   question in the design).

**Corrections (binding; where they differ from the text above in this task, these win):**

- The `AGENTS.md` section to retitle is its only one, "Be a good Hermes plugin citizen" (line 3). The lens text
  "private Hermes names only in muster/hermes_private.py" stays.
- The crontab block starts with a `PATH=` line that contains the directory of `muster` (cron's PATH is bare), e.g.
  `PATH=/Users/<you>/.local/bin:/opt/homebrew/bin:/usr/bin:/bin`.
- `docs/setup.md#standalone` also states: prompts are found only through an editable install; anyone with a shell as
  you can answer decisions (the coordinator agent included), so the skill rule in Task 3 is the control;
  `notify_topics` is ignored.

- **Verify:** `$PY -m pytest -q tests/test_prompts.py && grep -n "standalone" AGENTS.md docs/setup.md docs/review-lenses.md README.md`

## Task 7
**End-to-end standalone run, with no Hermes**

- **Exists because:** each task is tested alone; only a whole run proves no hidden `hermes` call or Hermes import
  remains on the standalone path (the stated goal).
- **Model:** sonnet
- **Files:** new `tests/test_standalone_e2e.py`.
- **Follows:** the launch tests in `tests/test_launch.py` and `tests/test_runs.py:28-110` for how `tests/fake_herdr.py`
  is installed over `core.run`.

One test, flag on, `MUSTER_HOME=tmp_path`, `fake_herdr` dispatching `core.run`, wrapped so that any argv whose first
item is `hermes` fails the test, and with `sys.modules` entries `hermes_cli`, `hermes_constants`, `gateway.status`,
`agent.redact`, `plugins.plugin_storage` set to `None` (import raises):

1. `adhoc.launch` (through `cli.standalone(["launch", "--cwd", <clone>, "--branch", "feat/x", "--title", "t",
   "--brief", <file>])`) creates a ledger card (`ledger.cards` has one, status `ready`) and the pane's hook settings
   name `<muster bin> hook`.
2. A `notification` hook event for that card (call `adhoc.hook` the way `tests/test_runs.py` does) opens a wait card;
   `runs.flush()` runs without error.
3. A question request for that ledger (`decisions.create("question", ...)` in the shape `bridge.py:111-123` writes)
   appears in `cli.standalone(["decisions", "list", "--json"])` output and is answered by
   `cli.standalone(["decisions", "answer", <id>, "--choice", <label>])`.

If a step cannot pass without changing production code outside this plan's files, stop and report the hidden Hermes
need; do not stub it away.

**Corrections (binding; where they differ from the text above in this task, these win):**

- Block Hermes with `sys.modules["hermes_cli"] = sys.modules["hermes_constants"] = sys.modules["gateway"] =
  sys.modules["agent"] = sys.modules["plugins"] = sys.modules["tools"] = None` via `monkeypatch.setitem` (a
  submodule entry such as `gateway.status` is not enough once the package is imported).
- Use the `standalone_home` fixture (Task 1). `--cwd` is one of the sandbox clones; the brief file's content is
  `"Add a --json flag. Keep text output."`.
- Step 3's request: `decisions.create("question", <ledger>, tool={"name": "AskUserQuestion"}, proposal=None,
  wait=None, run={"repo": <slug>, "pane": "p1", "kind": "adhoc", "branch": "feat/x"}, alive=time.time(),
  questions=[{"text": "Which?", "header": "Pick", "multi": False, "options": [{"label": "A", "description": ""},
  {"label": "B", "description": ""}]}], choices=[["A", "B"]])`; answer with `--choice A`; assert the request is
  `answered` with `answer == {"Which?": "A"}`.
- Also assert: the hook settings and the brief written by step 1 contain `/bin/muster hook` and not `hermes muster`.

- **Verify:** `$PY -m pytest -q tests/test_standalone_e2e.py && $PY -m pytest -q`
