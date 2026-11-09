"""Telegram's side of the activity log: what the owner sends and taps, and every message
Ultron sends or edits. Handlers run "caused by" the update, so what they change says so."""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Update
from telegram.ext import ExtBot

from ultron.ops import activity

log = logging.getLogger(__name__)

LOGGED = {
    "sendMessage": "Sent",
    "editMessageText": "Edited a message to",
    "sendDocument": "Sent a file",
    "sendPhoto": "Sent a photo",
    "deleteMessage": "Deleted a message",
}


def _labels(markup: Any) -> list[str]:
    rows = getattr(markup, "inline_keyboard", None) or []
    return [getattr(b, "text", "?") for row in rows for b in row]


class LoggedBot(ExtBot):
    """Every message Ultron sends or edits goes into the activity log. `_do_post` is the
    one funnel all Bot methods go through (PTB documents it as the place to hook in)."""

    async def _do_post(self, endpoint: str, data: dict[str, Any], *args: Any, **kwargs: Any):
        if endpoint not in LOGGED:
            return await super()._do_post(endpoint, data, *args, **kwargs)
        try:
            result = await super()._do_post(endpoint, data, *args, **kwargs)
        except Exception as exc:
            _log_out(endpoint, data, None, failed=f"{type(exc).__name__}: {exc}")
            raise
        _log_out(endpoint, data, result)
        return result


def _log_out(endpoint: str, data: dict[str, Any], result: Any, failed: str | None = None) -> None:
    words = data.get("text") or data.get("caption") or ""
    document = data.get("document")
    if not words and document is not None:
        words = getattr(document, "filename", None) or ""
    buttons = _labels(data.get("reply_markup"))
    summary = LOGGED[endpoint] + (f": {activity.one_line(str(words))}" if words else "")
    if buttons:
        summary += f" [{' / '.join(buttons)}]"
    if failed:
        summary = f"FAILED {summary} ({activity.one_line(failed, 80)})"
    message_id = result.get("message_id") if isinstance(result, dict) else data.get("message_id")
    activity.record(
        "message_out",
        summary,
        text=str(words) or None,
        detail={"chat": data.get("chat_id"), "message": message_id, "buttons": buttons or None},
    )


# --- what the owner sends ---


def _never_raises(fn):
    """Logging the owner's messages must never stop them reaching their handler."""

    @functools.wraps(fn)
    def run(update: Any) -> Any:
        try:
            return fn(update)
        except Exception:  # noqa: BLE001
            log.exception("Couldn't log an update")
            return None

    return run


def tapped_label(update: Update) -> str:
    query = getattr(update, "callback_query", None)
    if query is None:
        return ""
    markup = getattr(query.message, "reply_markup", None) if query.message else None
    for row in getattr(markup, "inline_keyboard", None) or []:
        for button in row:
            if button.callback_data == query.data:
                return button.text
    return query.data or "?"


def cause_of(update: Update) -> str:
    try:
        if getattr(update, "callback_query", None) is not None:
            return f"you: tapped {tapped_label(update)}"
        message = getattr(update, "effective_message", None)
        words = (getattr(message, "text", None) or "") if message else ""
    except Exception:  # noqa: BLE001 - a cause is a nicety
        return "you"
    if words.startswith("/"):
        return f"you: {words.split()[0]}"
    return "you: message"


@_never_raises
def log_incoming(update: Update) -> None:
    """The owner's message or tap (called once the owner gate has let it through)."""
    with activity.caused_by(cause_of(update)):
        query = getattr(update, "callback_query", None)
        if query is not None:
            activity.record(
                "button", f"You tapped '{tapped_label(update)}'", detail={"data": query.data}
            )
            return
        message = getattr(update, "effective_message", None)
        if message is None:
            return
        words = getattr(message, "text", None) or getattr(message, "caption", None)
        edited = "edited a message" if getattr(update, "edited_message", None) else ""
        if words:
            verb = f"You {edited}: " if edited else "You: "
            activity.record("message_in", verb + activity.one_line(words), text=words)
        else:
            what = next(
                (
                    n
                    for n in ("voice", "photo", "document", "sticker", "video")
                    if getattr(message, n, None)
                ),
                "something",
            )
            activity.record("message_in", f"You sent a {what}")


@_never_raises
def log_stranger(update: Update) -> None:
    """Someone else wrote to the bot: noted without their words."""
    user = getattr(update, "effective_user", None)
    activity.record(
        "message_in", f"Ignored an update from someone else (user {user.id if user else '?'})"
    )


def traced(callback: Callable[[Update, Any], Awaitable[Any]]):
    """Run a handler "caused by" its update, so what it changes is tagged with it."""

    @functools.wraps(callback)
    async def run(update: object, context: Any) -> Any:
        if not isinstance(update, Update):
            return await callback(update, context)
        with activity.caused_by(cause_of(update)):
            return await callback(update, context)

    return run
