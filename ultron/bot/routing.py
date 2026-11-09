"""Work out which Ultron space a Telegram message belongs to, and where to send to a space."""

from dataclasses import dataclass

from ultron.config import Settings
from ultron.notify import Space


@dataclass(frozen=True)
class Origin:
    chat_id: int
    chat_type: str  # "private", "group", "supergroup", "channel"
    thread_id: int | None  # forum topic id, None for General / non-topic chats


def space_for(origin: Origin, settings: Settings) -> Space | None:
    """Return the space a message came from, or None if it should be ignored.

    - Private chat with the bot: Chat (works before any group is set up).
    - The configured group: by topic id; General or unknown topics count as Chat.
    - Any group while no group is configured: Chat, so `/status` can show its ids.
    - Any other group once one is configured: ignored.
    """
    if origin.chat_type == "private":
        return Space.CHAT
    if origin.chat_type not in ("group", "supergroup"):
        return None
    if settings.telegram_group_id is None:
        return Space.CHAT
    if origin.chat_id != settings.telegram_group_id:
        return None
    topics = {
        settings.telegram_topic_reflect: Space.REFLECT,
        settings.telegram_topic_briefs: Space.BRIEFS,
        settings.telegram_topic_chat: Space.CHAT,
    }
    topics.pop(None, None)
    return topics.get(origin.thread_id, Space.CHAT)


@dataclass(frozen=True)
class Destination:
    chat_id: int
    thread_id: int | None


def destination_for(space: Space, settings: Settings) -> Destination:
    """Where Ultron sends a message it starts itself. Falls back to a DM with the owner."""
    if settings.telegram_group_id is None:
        return Destination(settings.telegram_owner_id, None)
    thread = {
        Space.CHAT: settings.telegram_topic_chat,
        Space.REFLECT: settings.telegram_topic_reflect,
        Space.BRIEFS: settings.telegram_topic_briefs,
    }[space]
    return Destination(settings.telegram_group_id, thread)
