"""Telegram connectivity check (lives in bot/ because it imports telegram)."""

from telegram import Bot
from telegram.error import InvalidToken

from ultron.config import Settings
from ultron.errors import UserFacingError


class TelegramError(UserFacingError):
    pass


async def check_bot(settings: Settings) -> str:
    try:
        async with Bot(settings.telegram_token.get_secret_value()) as bot:
            me = await bot.get_me()
    except InvalidToken as exc:
        raise TelegramError(
            "Telegram rejected TELEGRAM_TOKEN. Copy it again from @BotFather."
        ) from exc
    return f"bot @{me.username} is reachable"
