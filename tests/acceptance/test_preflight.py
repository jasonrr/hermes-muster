"""The rig boots fail-closed: a valid layout passes both preflights, and each broken one aborts naming its check,
with nothing journaled and nothing posted (design §5a)."""

from pathlib import Path

import pytest

from tests.acceptance.conftest import REPO, Preflight, start


def test_rig_boots(rig):
    assert rig.gw.alive()
    marks = rig.marks()
    assert any("gateway" in m["argv"] for m in marks) and any("recover" in m["argv"] for m in marks)
    assert all(m["patched"] for m in marks) and rig.entries() == []


def test_preflight_accepts_valid_layout(rig):
    plugin = rig.hermes / "plugins" / "muster"
    assert plugin.is_symlink() and plugin.resolve() == REPO.resolve() and not REPO.resolve().is_relative_to(rig.root)
    for p in (rig.hermes, rig.user, Path(rig.settings["worktrees"]), rig.hermes / "plugin-data" / "muster"):
        assert p.resolve().is_relative_to(rig.root.resolve())


OUTSIDE = Path.home() / "muster-acc-should-not-exist"


def no_scripts(rig):
    (rig.user / ".local" / "bin" / "hermes").unlink()


@pytest.mark.parametrize("check, breaks, after", [
    ("gateway-marker", lambda: {"prepend": "STANDIN_ONLY"}, None),
    ("paths", lambda: {"settings": {"worktrees": str(OUTSIDE)}}, None),
    ("paths", lambda: {"plugin": REPO / "tests"}, None),
    ("channel", lambda: {"settings": {"notify_chat_id": "C0NOTALLOW"}}, None),
    ("path-scripts", lambda: {}, no_scripts),
], ids=["shim-off", "state-outside", "other-plugin", "channel", "no-wrapper"])
def test_a_broken_layout_aborts_in_preflight(make_rig, check, breaks, after):
    from tests.acceptance import hermes_kit as kit

    kwargs = breaks()
    if kwargs.get("prepend") == "STANDIN_ONLY":  # the stand-in's shim alone: Slack still redirected, muster unshimmed
        kwargs["prepend"] = kit.SLACK_SHIM.parent
    rig = make_rig(**kwargs)
    if after:
        after(rig)
    with pytest.raises(Preflight, match=f"^{check}: "):
        start(rig)
    assert rig.entries() == []
    assert rig.standin.calls_of("chat.postMessage") == []
    assert not OUTSIDE.exists()


def test_timeline_order_and_redaction(tmp_path):
    import time
    import types

    from slack_sdk import WebClient

    from tests.acceptance import conftest, hermes_kit as kit

    standin = kit.SlackStandin()
    standin.start()
    try:
        client = WebClient(token=standin.bot_token, base_url=standin.api_base)
        parent = client.chat_postMessage(channel=conftest.CHANNEL, text="parent line")["ts"]
        client.chat_postMessage(channel=conftest.CHANNEL, thread_ts=parent, text="reply with xoxb-SECRET-1 inside")
        rig = conftest.Rig(tmp_path, tmp_path, {}, {}, None, tmp_path / "s", types.SimpleNamespace(standin=standin),
                           None, None, started=time.monotonic())
        text = conftest.timeline(rig).read_text()
    finally:
        standin.stop()
    calls = [line for line in text.splitlines() if "chat.postMessage" in line]
    assert len(calls) == 2 and "parent line" in calls[0] and "reply with [redacted] inside" in calls[1]
    assert f"### thread {parent}" in text and text.count("### thread") == 1
    assert "SECRET" not in text and "xoxb" not in text
