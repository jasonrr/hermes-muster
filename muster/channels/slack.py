"""slack: muster's Slack channel."""

from .. import core
from ..config import ConfigError

name = "slack"
durable_prompts = True


def validate(settings):
    """A channel (C…), its approvers and group chat type: Slack has no DM fallback, and Hermes's wake session for
    a thread is a group one."""
    entries = core.approver_entries(settings["notify_user_id"])
    return ([] if str(settings["notify_chat_id"]).strip() else ["notify_chat_id"]) + (
        [] if entries and all(entries) else ["notify_user_id"]) + (
        [] if settings["notify_chat_type"] == "group" else ["notify_chat_type"])


def home():
    raise ConfigError("muster: Slack needs notify_chat_id (a channel id, C…); it has no DM fallback")
