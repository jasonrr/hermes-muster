"""telegram: muster's Telegram channel."""

from .. import core

name = "telegram"
durable_prompts = False


def validate(settings):
    """A forum topic lives in a Telegram group, never in the DM fallback."""
    if settings["notify_topics"] is not True:
        return []
    entries = core.approver_entries(settings["notify_user_id"])
    return ([] if str(settings["notify_chat_id"]).strip() else ["notify_chat_id"]) + (
        [] if entries and all(entries) else ["notify_user_id"])


def home():
    return core.telegram_home()
