"""Telegram implementation of the core `Notifier` protocol."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ultron.bot.routing import destination_for
from ultron.config import Settings
from ultron.notify import Space
from ultron.state import State

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096  # characters per message, counted in UTF-16 units like Notion's
KEY_ASKS = "telegram.asks"  # JSON: {"chat_id:message_id": topic} for questions Ultron asked
MAX_ASKS = 20
KEY_OPEN = "telegram.open_question"  # JSON: {"chat_id:thread_id": the latest question}
OPEN_FOR = timedelta(minutes=30)  # a plain message this soon after a question answers it
EMPTY = "…"  # Telegram refuses empty messages


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Telegram-sized pieces, cut at a line break (or else a space) where possible, so
    long messages don't break mid-word. Never returns an empty piece."""
    if not text.strip():
        return [EMPTY]
    pieces: list[str] = []
    while _units(text) > limit:
        # The longest prefix within the limit (by code points, then trimmed for UTF-16)
        cut = limit
        while _units(text[:cut]) > limit:
            cut -= 1
        window = text[:cut]
        at = window.rfind("\n")
        if at < limit // 2:
            at = window.rfind(" ")
        if at < limit // 2:
            at = cut
        if text[:at].strip():
            pieces.append(text[:at].rstrip())
        text = text[at:].lstrip()
    if text.strip():
        pieces.append(text)
    return pieces


def record_ask(state: State, chat_id: int, message_id: int, topic: str) -> None:
    asks = json.loads(state.get(KEY_ASKS) or "{}")
    asks[f"{chat_id}:{message_id}"] = topic
    state.set(KEY_ASKS, json.dumps(dict(list(asks.items())[-MAX_ASKS:])))


def topic_for(state: State, chat_id: int, message_id: int) -> str | None:
    """Which question a reply answers, if it's a reply to one Ultron asked."""
    return json.loads(state.get(KEY_ASKS) or "{}").get(f"{chat_id}:{message_id}")


# --- the open question ---
# Questions used to arrive with ForceReply, which pins "replying to…" in Telegram's
# message box until that exact message gets a swipe-reply, so it stuck around after the
# owner had answered (owner's report, 25 Oct). Now the latest question in each chat (and
# topic) stays open for 30 minutes and the next plain message answers it; a swipe-reply
# to an older question still goes to that one.


@dataclass(frozen=True)
class OpenQuestion:
    topic: str
    message_id: int
    text: str


def _place(chat_id: int, thread_id: int | None) -> str:
    return f"{chat_id}:{thread_id or 0}"


def open_ask(
    state: State, chat_id: int, thread_id: int | None, message_id: int, topic: str, text: str
) -> None:
    opened = json.loads(state.get(KEY_OPEN) or "{}")
    opened[_place(chat_id, thread_id)] = {
        "topic": topic,
        "message_id": message_id,
        "text": text[:500],
        "at": datetime.now(UTC).isoformat(),
    }
    state.set(KEY_OPEN, json.dumps(opened))


def open_question(
    state: State, chat_id: int, thread_id: int | None, now: datetime | None = None
) -> OpenQuestion | None:
    """The question still waiting for an answer in this chat (and topic), if any."""
    found = json.loads(state.get(KEY_OPEN) or "{}").get(_place(chat_id, thread_id))
    now = now or datetime.now(UTC)
    if not found or now - datetime.fromisoformat(found["at"]) > OPEN_FOR:
        return None
    return OpenQuestion(found["topic"], found["message_id"], found["text"])


def close_ask(state: State, chat_id: int, thread_id: int | None, topic: str | None = None) -> None:
    """The question here was answered (only if it's `topic`, when given)."""
    opened = json.loads(state.get(KEY_OPEN) or "{}")
    found = opened.get(_place(chat_id, thread_id))
    if found and (topic is None or found["topic"] == topic):
        del opened[_place(chat_id, thread_id)]
        state.set(KEY_OPEN, json.dumps(opened))


class TelegramNotifier:
    def __init__(self, bot: Bot, settings: Settings, state: State | None = None) -> None:
        self.bot = bot
        self.settings = settings
        self.state = state

    async def _send(self, text: str, space: Space, reply_markup=None):
        """Send `text` in as many messages as it takes; the markup goes on the last one,
        which is returned."""
        dest = destination_for(space, self.settings)
        pieces = split_message(text)
        message = None
        for n, piece in enumerate(pieces):
            message = await self.bot.send_message(
                chat_id=dest.chat_id,
                text=piece,
                message_thread_id=dest.thread_id,
                reply_markup=reply_markup if n == len(pieces) - 1 else None,
            )
        return message

    async def send(self, text: str, space: Space = Space.BRIEFS) -> None:
        await self._send(text, space)

    async def ask(
        self, text: str, topic: str, space: Space = Space.BRIEFS, placeholder: str | None = None
    ) -> None:
        """Send a question. The owner's next plain message there (within 30 minutes), or a
        reply to this message, is routed back to `topic` by the message handler.
        `placeholder` is unused since ForceReply was dropped; kept for the protocol."""
        message = await self._send(text, space)
        if self.state is not None:
            record_ask(self.state, message.chat_id, message.message_id, topic)
            thread = destination_for(space, self.settings).thread_id
            open_ask(self.state, message.chat_id, thread, message.message_id, topic, text)

    async def offer(
        self,
        text: str,
        topic: str,
        choices: list[tuple[str, str]],
        space: Space = Space.BRIEFS,
    ) -> None:
        """Send a message with buttons (one row, or several); taps arrive as
        `offer:<topic>:<key>`. A long message is split, with the buttons on its last part."""
        await self._send(text, space, reply_markup=keyboard(topic, choices))


def keyboard(topic: str, choices) -> InlineKeyboardMarkup:
    """[(key, label)] is one row; [[(key, label)], …] is several rows."""
    rows = choices if choices and isinstance(choices[0], list) else [choices]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"offer:{topic}:{key}")
                for key, label in row
            ]
            for row in rows
        ]
    )
