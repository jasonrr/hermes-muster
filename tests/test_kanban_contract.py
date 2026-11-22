"""The real hermes kanban CLI, in a throwaway home, does exactly the moves core and events rely on.

The unit tests stub hermes; this file checks the stubs against the real CLI. It skips when hermes is
not installed, and says so: a skip here is not evidence the contract holds.
"""

import json
import os
import shutil
import subprocess

import pytest

import muster.core as core
import muster.launcher as launcher
from muster import config, ledger

HERMES = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")


@pytest.fixture(params=["hermes", "files"])
def k(request, tmp_path, monkeypatch):
    if request.param == "files":
        monkeypatch.setattr(config, "standalone", True)
        monkeypatch.setenv("MUSTER_HOME", str(tmp_path))

        def run(*args, ok=True):
            try:
                out = ledger.call("muster", list(args))
            except core.CommandError as error:
                assert not ok, (args, str(error))
                return ""
            assert ok, (args, out)
            return out
        run.backend = "files"
        return run
    if not os.path.exists(HERMES):
        pytest.skip("hermes not installed: kanban contract NOT checked")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_KANBAN_HOME": str(tmp_path)}
    subprocess.run([HERMES, "kanban", "boards", "create", "muster"],
                   capture_output=True, env=env, timeout=120, check=True)

    def run(*args, ok=True):
        result = subprocess.run([HERMES, "kanban", "--board", "muster", *args],
                                capture_output=True, text=True, env=env, timeout=120, check=False)
        assert (result.returncode == 0) == ok, (args, result.stdout, result.stderr)
        return result.stdout
    run.backend = "hermes"
    return run


def new(k, key=None):
    return json.loads(k("create", *(["--idempotency-key", key] if key else []), "--json", "--", "title"))


def status(k, card):
    return json.loads(k("show", card, "--json"))["task"]["status"]


def test_an_unassigned_card_is_ready_and_the_key_makes_create_idempotent(k):
    first, again = new(k, "o/r#1@9"), new(k, "o/r#1@9")
    assert first["id"] == again["id"] and first["created_at"] == again["created_at"]
    assert first["status"] == "ready" and first["assignee"] is None


def test_a_ready_card_completes_directly(k):
    """The mainline `done` path (test_events.py's fake_run) assumes this: complete needs no block first."""
    card = new(k)["id"]
    k("complete", card, "--summary", "s")
    assert status(k, card) == "done"


def test_a_second_same_kind_block_routes_to_triage_and_triage_is_a_dead_end(k):
    """Why events opens a wait card per wait instead of blocking and unblocking one card."""
    card = new(k)["id"]
    k("block", "--kind", "needs_input", card, "one")
    k("unblock", card)
    k("block", "--kind", "needs_input", card, "two")
    assert status(k, card) == "triage"
    k("unblock", card, ok=False)
    k("complete", card, "--summary", "s", ok=False)


def test_a_held_card_blocks_without_a_kind_so_its_later_blocks_still_block(k):
    """muster.hold: a generic block, an unblock on release, then the run's own needs_input or capability block."""
    for later in ("needs_input", "capability"):
        card = new(k)["id"]
        k("block", "--", card, "Possible duplicate; not dispatched")
        assert status(k, card) == "blocked"
        k("unblock", card)
        k("block", "--kind", later, card, "the run asks")
        assert status(k, card) == "blocked"


def test_a_reason_after_double_dash_that_looks_like_a_flag_is_kept_whole(k):
    """events.open_wait passes `--` before the card: a question may start with `--kind=`."""
    card = new(k)["id"]
    k("block", "--kind", "needs_input", "--", card, "--kind=capability boom")
    assert status(k, card) == "blocked"
    assert "--kind=capability boom" in k("show", card)


def test_a_launch_failure_blocks_with_kind_capability(k):
    """launcher.setup_trouble's kind: a ready card blocks for a human, and the event carries the kind."""
    card = new(k)["id"]
    k("block", "--kind", "capability", card, "Setup trouble, not a product decision: x.\nDetails: y")
    assert status(k, card) == "blocked"
    k("complete", card, "--summary", "s")
    assert status(k, card) == "done"


def test_a_blocked_card_refuses_a_second_block_and_completes_directly(k):
    card = new(k)["id"]
    k("block", "--kind", "needs_input", card, "one")
    k("block", "--kind", "needs_input", card, "again", ok=False)
    k("complete", card, "--summary", "s")
    assert status(k, card) == "done"


def test_a_wait_card_subscribes_blocks_and_archives_once(k):
    card = new(k, "t_x:wait:1")["id"]
    k("notify-subscribe", card, "--platform", "telegram", "--chat-id", "4242", "--user-id", "4242",
      "--chat-type", "dm", "--notifier-profile", "default", "--delivery-mode", "notify+wake")
    [sub] = json.loads(k("notify-list", card, "--json"))
    assert sub["chat_id"] == "4242" and sub["delivery_mode"] == "notify+wake"
    k("block", "--kind", "needs_input", card, "waiting")
    k("archive", card)
    assert status(k, card) == "archived"
    k("archive", card, ok=False)


def test_board_exists_reads_the_board_list_from_the_real_cli(k, monkeypatch):
    if k.backend == "files":
        pytest.skip("no board list to read: the directory is made on first use")
    core.board_exists()
    monkeypatch.setitem(config.settings, "board", "no-such-board")
    with pytest.raises(config.ConfigError, match="no-such-board"):
        core.board_exists()


@pytest.mark.parametrize("target", [
    {"chat_id": "-1004455490829", "user_id": "8768235002", "chat_type": "group"},
    None,  # the DM fallback from TELEGRAM_HOME_CHANNEL
])
def test_subscribe_reads_back_chat_user_type_profile_and_mode_from_the_real_cli(k, tmp_path, monkeypatch, target):
    if k.backend == "files" and target is None:
        pytest.skip("the DM fallback reads Telegram's home")
    (tmp_path / ".env").write_text("TELEGRAM_HOME_CHANNEL=4242\n")
    if target:
        for key, value in target.items():
            monkeypatch.setitem(config.settings, f"notify_{key}", value)
    monkeypatch.setattr(core, "kanban", lambda *args: k(*args))
    card = new(k)["id"]
    core.subscribe(card)  # raises unless all five fields read back
    [sub] = json.loads(k("notify-list", card, "--json"))
    want = target or {"chat_id": "4242", "user_id": "4242", "chat_type": "dm"}
    assert {f: sub[f] for f in ("chat_id", "user_id", "chat_type", "notifier_profile", "delivery_mode")} == \
        {**want, "notifier_profile": "default", "delivery_mode": "notify+wake"}


def test_recover_unblocks_a_launch_failure_once_and_a_later_ask_still_blocks(k, tmp_path, monkeypatch):
    """launcher.recoverable/report_failure: a capability block, one unblock by recover, then the agent's
    needs_input block (another kind) still blocks; a second capability block would be triage, which is
    why recover comments instead of blocking. block_kind is read from the board db: show --json omits it."""
    card = new(k)["id"]
    k("block", "--kind", "capability", card, "The coding agent did not start")
    assert launcher.block_kind(card) == "capability"
    k("unblock", card)
    assert status(k, card) == "ready"
    k("block", "--kind", "needs_input", card, "The agent's session ended")
    assert status(k, card) == "blocked" and launcher.block_kind(card) == "needs_input"
    again = new(k)["id"]
    k("block", "--kind", "capability", again, "one")
    k("unblock", again)
    k("block", "--kind", "capability", again, "two")
    assert status(k, again) == "triage"


def test_the_run_ack_reads_event_ids_from_the_board_db_and_cursors_from_notify_list(k):
    """runs.acked compares notify-list's two cursors with the card's newest blocked/completed event
    id, which runs.last_event reads from the board db. The gateway's own cursor moves are not
    exercised here (no gateway runs): they are hermes' notifier as read on 2026-09-28."""
    import muster.runs as runs
    card = new(k, "run:o/r:fix/x")["id"]
    k("notify-subscribe", card, "--platform", "telegram", "--chat-id", "4242", "--user-id", "4242",
      "--chat-type", "dm", "--notifier-profile", "default", "--delivery-mode", "notify+wake")
    assert runs.last_event(card) == 0
    k("block", "--kind", "needs_input", card, "waiting")
    blocked = runs.last_event(card)
    assert blocked > 0
    k("comment", card, "a comment is not a notifier event")
    assert runs.last_event(card) == blocked
    k("complete", card, "--summary", "s")
    assert runs.last_event(card) > blocked
    [sub] = json.loads(k("notify-list", card, "--json"))
    assert sub["delivery_mode"] == "notify+wake"
    assert sub["last_event_id"] >= 0 and sub["last_ping_event_id"] == 0  # nothing pinged: no gateway


def test_a_comment_and_a_create_body_read_back_whole_from_show_json(k):
    """events.post_proposal skips a version whose header comment is already on the ledger, and a reviewer
    reads the snapshot from `show --json`: both need comments and the create body back verbatim."""
    text = "Proposal v1 0123456789ab for review\n\n## Approach\n--kind=x is text\n" + "x" * 20000
    card = json.loads(k("create", "--body", text, "--json", "--", "title"))["id"]
    k("comment", "--", card, text)
    shown = json.loads(k("show", card, "--json"))
    assert shown["task"]["body"] == text
    assert shown["comments"][-1]["body"] == text


def test_create_with_a_key_on_a_card_that_is_already_done_returns_that_card(k):
    """decisions.rereview repeats its create for a head it already made a review card for: no second card."""
    first = new(k, "review:t_x:abc")
    k("complete", first["id"], "--summary", "s")
    again = new(k, "review:t_x:abc")
    assert again["id"] == first["id"] and again["status"] == "done"
    assert status(k, first["id"]) == "done"


def test_a_card_written_by_one_call_is_read_by_a_fresh_call(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "standalone", True)
    monkeypatch.setenv("MUSTER_HOME", str(tmp_path))
    card = json.loads(ledger.call("muster", ["create", "--json", "--", "title"]))["id"]
    assert ledger.cards("muster")[0]["id"] == card
    assert json.loads(ledger.call("muster", ["show", card]))["task"]["title"] == "title"
    assert ledger.last_event("muster", card) == 0 and ledger.last_event("muster", "t_nope") == 0


def test_an_archived_cards_key_makes_a_new_card(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "standalone", True)
    monkeypatch.setenv("MUSTER_HOME", str(tmp_path))
    args = ["create", "--idempotency-key", "k", "--json", "--", "title"]
    first = json.loads(ledger.call("muster", args))
    ledger.call("muster", ["archive", first["id"]])
    assert json.loads(ledger.call("muster", args))["id"] != first["id"]


def test_a_bad_argv_is_a_command_error_and_an_unknown_card_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "standalone", True)
    monkeypatch.setenv("MUSTER_HOME", str(tmp_path))
    with pytest.raises(core.CommandError):
        ledger.call("muster", ["block", "--no-such-flag"])
    with pytest.raises(core.CommandError):
        ledger.call("muster", [])
    with pytest.raises(core.CommandError, match="no such task"):
        ledger.call("muster", ["show", "t_nope"])


def test_an_unknown_block_kind_is_refused_and_the_card_stays_ready(k):
    card = new(k)["id"]
    k("block", "--kind", "bogus", card, "why", ok=False)
    assert status(k, card) == "ready"


def test_a_card_id_that_is_not_a_card_id_never_reaches_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "standalone", True)
    monkeypatch.setenv("MUSTER_HOME", str(tmp_path))
    ledger.root("muster").mkdir(parents=True)
    (ledger.root("muster").parent / "other.json").write_text("{}")
    for card in ("../other", "t_zzzzzzzz", "t_1"):
        for argv in (["show", card], ["comment", card, "x"]):
            with pytest.raises(core.CommandError, match="no such task"):
                ledger.call("muster", argv)
