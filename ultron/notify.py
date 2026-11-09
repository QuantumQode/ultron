"""How the core talks to the owner, independent of any front end.

The core only knows about `Space`s and the `Notifier` protocol. Telegram
(`ultron/bot/`) implements it today; a future iPhone app can implement it too.
"""

from enum import StrEnum
from typing import Protocol


class Space(StrEnum):
    CHAT = "chat"  # everyday requests and tasks
    REFLECT = "reflect"  # thoughts and life, coaching conversations
    BRIEFS = "briefs"  # morning/evening/weekly messages and alerts


class Notifier(Protocol):
    async def send(self, text: str, space: Space = Space.BRIEFS) -> None:
        """Send a message Ultron starts itself (alerts, briefs, check-ins)."""
        ...

    async def ask(
        self, text: str, topic: str, space: Space = Space.BRIEFS, placeholder: str | None = None
    ) -> None:
        """Send a question whose answer matters. The front end makes answering easy (in
        Telegram: the reply box opens on it) and routes the answer back with `topic`."""
        ...

    async def offer(
        self,
        text: str,
        topic: str,
        choices: list[tuple[str, str]],
        space: Space = Space.BRIEFS,
    ) -> None:
        """Send a message with buttons: `choices` is [(key, label)] for one row, or a list
        of such rows. The front end routes a tap back as (topic, key)."""
        ...
