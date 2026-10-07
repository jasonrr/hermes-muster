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
from muster import config

HERMES = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
pytestmark = pytest.mark.skipif(not os.path.exists(HERMES), reason="hermes not installed: kanban contract NOT checked")


@pytest.fixture
def k(tmp_path, monkeypatch):
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


def test_a_launch_failure_blocks_with_kind_capability(k):
    """core.setup_trouble's kind: a ready card blocks for a human, and the event carries the kind."""
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
    core.board_exists()
    monkeypatch.setitem(config.settings, "board", "no-such-board")
    with pytest.raises(config.ConfigError, match="no-such-board"):
        core.board_exists()


@pytest.mark.parametrize("target", [
    {"chat_id": "-1004455490829", "user_id": "8768235002", "chat_type": "group"},
    None,  # the DM fallback from TELEGRAM_HOME_CHANNEL
])
def test_subscribe_reads_back_chat_user_type_profile_and_mode_from_the_real_cli(k, tmp_path, monkeypatch, target):
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
    """core.recoverable/report_failure: a capability block, one unblock by recover, then the agent's
    needs_input block (another kind) still blocks; a second capability block would be triage, which is
    why recover comments instead of blocking. block_kind is read from the board db: show --json omits it."""
    card = new(k)["id"]
    k("block", "--kind", "capability", card, "The coding agent did not start")
    assert core.block_kind(card) == "capability"
    k("unblock", card)
    assert status(k, card) == "ready"
    k("block", "--kind", "needs_input", card, "The agent's session ended")
    assert status(k, card) == "blocked" and core.block_kind(card) == "needs_input"
    again = new(k)["id"]
    k("block", "--kind", "capability", again, "one")
    k("unblock", again)
    k("block", "--kind", "capability", again, "two")
    assert status(k, again) == "triage"
