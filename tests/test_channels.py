"""channels: one module per platform, picked by name; the neutral gateway against a channel's contract."""

import pytest

from muster import channels, config
from muster.channels import slack, telegram


def test_get_picks_a_channel_by_name_and_refuses_any_other():
    assert channels.get("telegram") is telegram and channels.get("slack") is slack
    assert (telegram.name, telegram.durable_prompts, slack.name, slack.durable_prompts) == (
        "telegram", False, "slack", True)
    with pytest.raises(config.ConfigError, match="telegram, slack"):
        channels.get("discord")


def test_slack_has_no_dm_fallback():
    with pytest.raises(config.ConfigError, match="notify_chat_id"):
        slack.home()
