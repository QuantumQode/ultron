"""Make sure the Notion pages exist, and tell the owner when they're created."""

import asyncio

from ultron.config import Settings
from ultron.integrations.notion import EnsureResult, ensure_workspace_for
from ultron.notify import Notifier, Space
from ultron.state import State


def created_message(result: EnsureResult) -> str:
    ws = result.workspace
    return (
        "I've set up your Notion pages:\n"
        f"📥 Inbox: {ws.inbox_url}\n"
        f"🗂 Board: {ws.board_url}\n"
        f"📅 Schedule: {ws.schedule_url}\n\n"
        "Jot anything into the Inbox, any format. I'll sort it onto the Board every hour, "
        "and your approved plans go on the Schedule."
    )


async def setup_workspace(settings: Settings, state: State, notifier: Notifier) -> EnsureResult:
    result = await asyncio.to_thread(ensure_workspace_for, settings, state)
    if result.created:
        await notifier.send(created_message(result), Space.CHAT)
    return result
