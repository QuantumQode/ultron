"""Telegram handlers. Thin: read the update, call the core, format the reply."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from ultron.bot import activity_log
from ultron.bot.format import HELP, SPACE_LABELS, format_inbox, format_status, format_usage
from ultron.bot.notifier import close_ask, keyboard, open_question, split_message, topic_for
from ultron.bot.routing import Origin, space_for
from ultron.brain.chat import chat
from ultron.config import Settings
from ultron.errors import UserFacingError
from ultron.features import (
    accountability,
    brief,
    focus,
    journal,
    nightly,
    onboarding,
    planner,
    reflect,
    review,
    suggestions,
    watch,
    weekly,
)
from ultron.features.email_report import inbox_digest
from ultron.features.items import ItemStore
from ultron.features.mail_sync import run_mail
from ultron.features.sync import run_sync
from ultron.features.triage import EmailStore
from ultron.notify import Notifier, Space
from ultron.ops.guard import CostGuard
from ultron.ops.status import build_status, build_usage
from ultron.state import State

log = logging.getLogger(__name__)


@dataclass
class Deps:
    settings: Settings
    state: State
    guard: CostGuard
    notifier: Notifier | None = None
    make_llm: Callable[[], AbstractAsyncContextManager[Any]] | None = None
    reschedule: Callable[[], None] | None = None  # re-time the ritual jobs after a change


def _deps(context: ContextTypes.DEFAULT_TYPE) -> Deps:
    return context.application.bot_data["deps"]


def is_owner(update: Update, owner_id: int) -> bool:
    user = update.effective_user
    return user is not None and user.id == owner_id


def origin_of(message: Message) -> Origin:
    return Origin(
        chat_id=message.chat.id,
        chat_type=message.chat.type,
        thread_id=message.message_thread_id if message.is_topic_message else None,
    )


async def owner_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler. Silently drops anything not from the owner
    or from a chat Ultron doesn't serve, so strangers can't tell the bot is listening."""
    deps = _deps(context)
    message = update.effective_message
    if not is_owner(update, deps.settings.telegram_owner_id):
        activity_log.log_stranger(update)
        raise ApplicationHandlerStop
    if message is not None and space_for(origin_of(message), deps.settings) is None:
        raise ApplicationHandlerStop
    activity_log.log_incoming(update)


async def reply(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Reply in the same chat and topic, split into Telegram-sized pieces if needed."""
    thread = message.message_thread_id if message.is_topic_message else None
    pieces = split_message(text)
    for n, piece in enumerate(pieces):
        last = n == len(pieces) - 1
        await message.reply_text(
            piece, message_thread_id=thread, reply_markup=markup if last else None
        )


_background: set[asyncio.Task] = set()  # the loop only keeps weak references to tasks


def _sync_in_background(deps: "Deps") -> None:
    """Update the Board after the chat changed something, without delaying the reply."""
    if not (deps.notifier and deps.make_llm and deps.settings.notion_enabled):
        return

    async def go() -> None:
        try:
            await run_sync(deps.settings, deps.state, deps.guard, deps.notifier, deps.make_llm)
        except Exception:  # noqa: BLE001 - the hourly sync will catch up
            log.exception("Board update after chat failed")

    task = asyncio.get_running_loop().create_task(go())
    _background.add(task)
    task.add_done_callback(_background.discard)


CONFIRM_BUTTONS = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Looks right", callback_data="onb:yes"),
            InlineKeyboardButton("✏️ Change something", callback_data="onb:edit"),
        ]
    ]
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update.effective_message, HELP)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    message = update.effective_message
    origin = origin_of(message)
    report = build_status(deps.settings, deps.state, deps.guard)
    text = format_status(
        report,
        deps.settings.zone,
        chat_id=origin.chat_id,
        thread_id=origin.thread_id,
        space=space_for(origin, deps.settings),
    )
    await reply(message, text)


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    await reply(update.effective_message, format_usage(build_usage(deps.state, deps.guard)))


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    message = update.effective_message
    settings = deps.settings
    if not (settings.notion_enabled or settings.mail_enabled) or not (
        deps.notifier and deps.make_llm
    ):
        await reply(message, "Notion and email aren't set up yet, so there's nothing to sync.")
        return
    if settings.mail_enabled:
        await reply(message, "Checking your email…")
        try:
            mail_report = await run_mail(
                settings, deps.state, deps.guard, deps.notifier, deps.make_llm
            )
        except Exception as exc:  # noqa: BLE001 - say what failed, then still sync Notion
            await reply(message, _job_failed(deps.state, "mail", exc))
        else:
            deps.state.record_job_success("mail")
            await reply(message, mail_report.summary())
    if not settings.notion_enabled:
        return
    await reply(message, "Syncing your Inbox and Board…")
    try:
        report = await run_sync(
            settings, deps.state, deps.guard, deps.notifier, deps.make_llm, force=True
        )
    except Exception as exc:  # noqa: BLE001 - always answer the owner
        await reply(message, _job_failed(deps.state, "sync", exc))
        return
    deps.state.record_job_success("sync")
    await reply(message, report.summary())


def _job_failed(state: State, job: str, exc: Exception) -> str:
    """Record a failed on-demand run (so /status and chat know) and word it for the owner."""
    if isinstance(exc, UserFacingError):
        return f"❌ {exc}"
    error = f"{type(exc).__name__}: {exc}"
    log.warning("On-demand %s failed: %s", job, error)
    state.record_job_failure(job, error)
    what = {"mail": "email check", "sync": "Notion sync"}.get(job, job)
    return f"❌ The {what} failed: {error[:500]}"


async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    if not deps.settings.mail_enabled:
        await reply(update.effective_message, "Email isn't set up yet (EMAIL_ADDRESS in .env).")
        return
    zone = deps.settings.zone
    digest = inbox_digest(EmailStore(deps.state), datetime.now(zone))
    await reply(update.effective_message, format_inbox(digest, zone))


async def focus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    store = ItemStore(deps.state)
    text = " ".join(context.args or [])
    if not text:
        await reply(update.effective_message, focus.describe(deps.state, store))
        return
    today = datetime.now(deps.settings.zone).date()
    try:
        new = focus.set_focus(deps.state, store, text, today)
    except UserFacingError as exc:
        await reply(update.effective_message, f"❌ {exc}")
        return
    back = f" Then back to {focus.Focus(new.previous['blend']).label}." if new.previous else ""
    await reply(
        update.effective_message,
        f"Focus set: {new.label}.{back} I'll plan with this from now on.",
    )
    _sync_in_background(deps)


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    if deps.make_llm is None:
        await reply(update.effective_message, "Claude isn't set up yet.")
        return
    await reply(update.effective_message, onboarding.start(deps.state))


async def onboarding_reply(message: Message, deps: Deps, text: str) -> None:
    try:
        turn = onboarding.answer(deps.state, text)
        await reply(message, turn.text)
        if turn.needs_structuring:
            async with deps.make_llm() as llm:
                turn = await onboarding.structure(
                    deps.state,
                    llm,
                    deps.guard,
                    model=deps.settings.model_fast,
                    today=datetime.now(deps.settings.zone).date(),
                )
            await reply(message, turn.text, CONFIRM_BUTTONS if turn.confirm else None)
    except UserFacingError as exc:
        await reply(message, f"❌ {exc}")


async def onboarding_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)  # buttons work once
    message = query.message
    if query.data == "onb:edit":
        await reply(message, "What should I change? Just tell me in your own words.")
        return
    try:
        saved = onboarding.confirm(deps.state, ItemStore(deps.state))
    except UserFacingError as exc:
        await reply(message, f"❌ {exc}")
        return
    await reply(
        message,
        f"Saved {saved} entr{'y' if saved == 1 else 'ies'} to 📌 My schedule. I'll plan around "
        "them. You can edit them on the Board any time, or redo this with /schedule.",
    )
    if deps.notifier and deps.settings.notion_enabled:
        try:
            await run_sync(deps.settings, deps.state, deps.guard, deps.notifier, deps.make_llm)
        except UserFacingError as exc:
            log.warning("Board update after onboarding failed: %s", exc)


async def nightly_reply(message: Message, deps: Deps, text: str) -> None:
    """The owner answered "what are your priorities for tomorrow?"."""
    result = nightly.answer(deps.state, text)
    await reply(message, result.text)
    session = nightly.load(deps.state)
    if session is not None and session.skipped:
        draft = planner.load_draft(deps.state)
        if draft is not None and draft.days[0] == session.day:
            planner.discard(deps.state)  # e.g. the draft sent at the nudge
    if result.priorities is None or session is None:
        return
    days = planner.plan_days(date.fromisoformat(session.day))
    await make_plan(message, deps, days, result.priorities)


async def make_plan(
    message: Message,
    deps: Deps,
    days: list[date],
    priorities: str | None,
    feedback=None,
    replan: bool = False,
) -> None:
    if deps.notifier is None or deps.make_llm is None:
        await reply(message, "Claude isn't set up yet.")
        return
    thread = message.message_thread_id if message.is_topic_message else None
    await message.chat.send_action(ChatAction.TYPING, message_thread_id=thread)
    try:
        await planner.plan_and_offer(
            deps.settings,
            deps.state,
            deps.guard,
            deps.notifier,
            deps.make_llm,
            days=days,
            priorities=priorities,
            feedback=feedback,
            replan=replan,
        )
    except UserFacingError as exc:
        await reply(message, f"❌ {exc}")
    except Exception:  # noqa: BLE001 - always answer the owner
        log.exception("Planning failed")
        await reply(message, "❌ Something went wrong drafting the plan. Try /plan again.")


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/plan [today|tomorrow] [priorities…]"""
    deps = _deps(context)
    now = datetime.now(deps.settings.zone)
    days, priorities = planner.pick_days(" ".join(context.args or []), now)
    if not priorities:
        session = nightly.load(deps.state)
        if session and session.day == days[0].isoformat() and session.priorities:
            priorities = session.priorities
    when = " + ".join(f"{d:%a %d %b}" for d in days)
    await reply(update.effective_message, f"Drafting a plan for {when}…")
    await make_plan(update.effective_message, deps, days, priorities or None)


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/review: run today's evening review now (it normally starts at 21:00)."""
    deps = _deps(context)
    started = await review.start(deps.state, deps.notifier, datetime.now(deps.settings.zone))
    if started is None:
        await reply(update.effective_message, "Today's review has already started; see above.")


async def brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/brief: today's morning brief, now."""
    deps = _deps(context)
    try:
        await brief.send_brief(
            deps.settings, deps.state, deps.notifier, deps.guard, deps.make_llm, force=True
        )
    except UserFacingError as exc:
        await reply(update.effective_message, f"❌ {exc}")


async def weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/weekly: this week's review, now (it normally comes on Sunday at 19:00)."""
    deps = _deps(context)
    await reply(update.effective_message, "Writing up your week…")
    try:
        await weekly.run_weekly(
            deps.settings, deps.state, deps.guard, deps.notifier, deps.make_llm, force=True
        )
    except UserFacingError as exc:
        await reply(update.effective_message, f"❌ {exc}")
    except Exception:  # noqa: BLE001 - always answer the owner
        log.exception("Weekly review failed")
        await reply(update.effective_message, "❌ Something went wrong writing the review.")


async def weekly_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    query = update.callback_query
    choice = query.data.split(":", 2)[2]
    today = datetime.now(deps.settings.zone).date()
    text = weekly.apply(deps.state, choice, today)
    await query.answer(text[:200])
    draft = weekly.load_draft(deps.state)
    left = weekly.choices(draft) if draft else []
    useful = [c for c in left if c[0] != "skip"]
    await query.edit_message_reply_markup(
        reply_markup=keyboard(weekly.TOPIC, left) if useful else None
    )
    await reply(query.message, text)
    _sync_in_background(deps)


async def send_card(message: Message, deps: Deps, change_id: int) -> None:
    """A 💡 suggestion card, as its own message under the reply."""
    change = suggestions.get(deps.state, change_id)
    if change is None:
        return
    text = suggestions.card_text(deps.settings, deps.state, change)
    await reply(message, text, keyboard(suggestions.TOPIC, suggestions.choices(change)))


async def suggest_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[✅ Apply] [✏️ Tweak] [Dismiss] on a suggestion card."""
    deps = _deps(context)
    query = update.callback_query
    change_id, _, action = query.data.split(":", 2)[2].partition(":")
    now = datetime.now(deps.settings.zone)
    try:
        if action == "tweak":
            question = suggestions.start_tweak(deps.state, int(change_id), now)
            await query.answer()
            if deps.notifier:
                space = space_for(origin_of(query.message), deps.settings) or Space.CHAT
                await deps.notifier.ask(
                    question, f"{suggestions.TWEAK_TOPIC}{change_id}", space, "What should change?"
                )
            return
        if action == "apply":
            outcome = suggestions.apply(deps.settings, deps.state, int(change_id), now)
        else:
            outcome = suggestions.dismiss(deps.state, int(change_id), now)
    except UserFacingError as exc:
        await query.answer(str(exc)[:200])
        await query.edit_message_reply_markup(reply_markup=None)
        await reply(query.message, str(exc))
        return
    await query.answer(outcome.text[:200])
    await query.edit_message_reply_markup(reply_markup=None)
    await reply(query.message, outcome.text)
    if outcome.reschedule and deps.reschedule:
        deps.reschedule()
    if outcome.items_changed:
        _sync_in_background(deps)


async def tweak_reply(message: Message, deps: Deps, change_id: int) -> None:
    """The owner said what to change about a card: redraft it and send the new one."""
    thread = message.message_thread_id if message.is_topic_message else None
    await message.chat.send_action(ChatAction.TYPING, message_thread_id=thread)
    try:
        new = await suggestions.tweak(
            deps.settings,
            deps.state,
            deps.guard,
            deps.make_llm,
            change_id,
            message.text,
            datetime.now(deps.settings.zone),
        )
    except UserFacingError as exc:
        await reply(message, f"❌ {exc}")
        return
    await send_card(message, deps, new.id)


async def easy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/easy: no call-outs or check-ins for the rest of today."""
    deps = _deps(context)
    accountability.easy(deps.state, datetime.now(deps.settings.zone).date())
    await reply(
        update.effective_message,
        "Easy day. No call-outs or check-ins until tomorrow; the review and brief still come.",
    )


async def reflect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reflect: open a 🧠 Reflect session. Until /done, chat goes to the coach."""
    deps = _deps(context)
    _, new = reflect.start(deps.state, datetime.now(deps.settings.zone))
    await reply(update.effective_message, reflect.OPENING if new else reflect.ALREADY_OPEN)


REFLECT_BUTTON = "🧠 Make this a Reflect session"


async def _drop_tapped_row(query: Any) -> None:
    """Remove just the tapped button's row; other buttons on the message stay."""
    markup = query.message.reply_markup
    keep = [
        row
        for row in (markup.inline_keyboard if markup else [])
        if not any(b.callback_data == query.data for b in row)
    ]
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keep) if keep else None)


async def reflect_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[🧠 Make this a Reflect session] under a chat reply: open one, carrying on from it."""
    deps = _deps(context)
    query = update.callback_query
    await query.answer()
    await _drop_tapped_row(query)
    _, new = reflect.start_from_chat(deps.state, datetime.now(deps.settings.zone))
    await reply(query.message, reflect.FROM_CHAT if new else reflect.ALREADY_OPEN)


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/done: close the 🧠 Reflect session."""
    deps = _deps(context)
    message = update.effective_message
    closed = reflect.finish(deps.state, datetime.now(deps.settings.zone))
    await reply(message, reflect.CLOSED if closed else reflect.NONE_OPEN)
    if closed is None:
        return
    thread = message.message_thread_id if message.is_topic_message else None
    await message.chat.send_action(ChatAction.TYPING, message_thread_id=thread)
    written = await journal.write_pending(deps.settings, deps.state, deps.guard, deps.make_llm)
    for w in written:
        await reply(message, w.notice)
    if written:
        _sync_in_background(deps)


async def callout_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    query = update.callback_query
    key = query.data.split(":", 2)[2]
    text = accountability.answer_callout(deps.state, key, datetime.now(deps.settings.zone).date())
    await query.answer(text[:200])
    await query.edit_message_reply_markup(reply_markup=None)
    await reply(query.message, text)
    _sync_in_background(deps)


async def checkin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    query = update.callback_query
    choice = query.data.split(":", 2)[2]
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if choice == "replan":
        today = datetime.now(deps.settings.zone).date()
        await reply(query.message, "Replanning the rest of today…")
        await make_plan(query.message, deps, [today], None, replan=True)
    else:
        await reply(query.message, "Good. Keep going.")


async def brief_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[Replan today], or [Plan today] when there's no plan yet, under the morning brief."""
    deps = _deps(context)
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    now = datetime.now(deps.settings.zone)
    if query.data.endswith(":plan"):
        days, _ = planner.pick_days("", now)
        await reply(
            query.message, "Drafting a plan for " + " + ".join(f"{d:%a}" for d in days) + "…"
        )
        await make_plan(query.message, deps, days, None)
        return
    await reply(query.message, "Replanning the rest of today…")
    await make_plan(query.message, deps, [now.date()], None, replan=True)


async def review_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    query = update.callback_query
    key = query.data.split(":", 2)[2]
    result = await review.tap(deps.state, deps.notifier, key, datetime.now(deps.settings.zone))
    await query.answer(result.ack[:200])
    if result.rows is not None:
        markup = keyboard(review.TOPIC, result.rows) if result.rows else None
        await query.edit_message_reply_markup(reply_markup=markup)
    _sync_in_background(deps)  # ticks reach the Board and Schedule


async def review_reflect(message: Message, deps: Deps, text: str) -> None:
    await reply(message, "Thanks. Writing up today, then on to tomorrow…")
    try:
        await review.finish(
            deps.settings, deps.state, deps.guard, deps.notifier, deps.make_llm, reflection=text
        )
    except UserFacingError as exc:
        await reply(message, f"❌ {exc}")
    except Exception:  # noqa: BLE001 - always answer the owner
        log.exception("Finishing the review failed")
        await reply(message, "❌ Something went wrong moving on to tomorrow. Try /plan.")
    _sync_in_background(deps)


async def replan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/replan [what changed…]: reshuffle the rest of today around what's done."""
    deps = _deps(context)
    today = datetime.now(deps.settings.zone).date()
    what = " ".join(context.args or []) or None
    await reply(update.effective_message, "Replanning the rest of today…")
    await make_plan(update.effective_message, deps, [today], what, replan=True)


async def plan_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 2)[2]
    message = query.message
    draft = planner.load_draft(deps.state)
    await query.edit_message_reply_markup(reply_markup=None)  # buttons work once
    if draft is None:
        await reply(message, "That draft has expired. Send /plan for a fresh one.")
        return
    if choice == "edit":
        await deps.notifier.ask(
            'What should change? e.g. "gym at 07:00, move admin to Friday".',
            planner.EDIT_TOPIC,
            space_for(origin_of(message), deps.settings) or Space.CHAT,
            "What to change",
        )
        return
    if choice == "skip":
        planner.discard(deps.state)
        await reply(message, "Skipped. Nothing was added to your Schedule.")
        return
    approved = planner.approve(deps.state)
    await reply(
        message,
        f"✅ Saved to 📅 Schedule: {approved.blocks} block(s) for "
        + " + ".join(f"{d:%a %d %b}" for d in approved.days)
        + (f", plus {approved.later} for later this week." if approved.later else "."),
    )
    _sync_in_background(deps)


async def watch_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[🔁 Replan Mon] / [Keep as is] under a clash, or a length under "How long?"."""
    deps = _deps(context)
    query = update.callback_query
    kind, _, rest = query.data.split(":", 2)[2].partition(":")
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)  # buttons work once
    message = query.message
    now = datetime.now(deps.settings.zone)
    if kind == "keep":
        await reply(message, "Kept as is. The ⚠️ stays on 📅 Schedule until it's moved.")
        return
    if kind == "replan":
        day = date.fromisoformat(rest)
        if day < now.date():
            await reply(message, "That day has passed.")
            return
        await reply(message, f"Replanning {day:%a} around your fixed schedule…")
        await make_plan(message, deps, [day], None, replan=day == now.date())
        return
    item_id, _, minutes = rest.partition(":")
    if minutes == "other":
        await deps.notifier.ask(
            "How long, travel included? e.g. '1h 50m' or '110'.",
            f"{watch.LENGTH_TOPIC}{item_id}",
            space_for(origin_of(message), deps.settings) or Space.CHAT,
            "e.g. 1h 50m",
        )
        return
    await reply(message, watch.length_reply(deps.state, int(item_id), minutes))
    _sync_in_background(deps)  # the Schedule shows the real length; clashes get checked


async def plan_edit_reply(message: Message, deps: Deps, text: str) -> None:
    draft = planner.load_draft(deps.state)
    if draft is None:
        await reply(message, "That draft has expired. Send /plan for a fresh one.")
        return
    await reply(message, "Redrafting…")
    await make_plan(
        message,
        deps,
        [date.fromisoformat(d) for d in draft.days],
        draft.priorities,
        feedback=[*draft.feedback, text],
        replan=draft.replan_from is not None,
    )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    message = update.effective_message
    space = space_for(origin_of(message), deps.settings) or Space.CHAT
    replied = message.reply_to_message
    topic = topic_for(deps.state, replied.chat_id, replied.message_id) if replied else None
    asked = replied.text if topic and replied else None  # the question being answered
    thread = message.message_thread_id if message.is_topic_message else None
    if topic is None and replied is None and not nightly.is_question(message.text):
        pending = open_question(deps.state, message.chat.id, thread)
        if pending:  # a plain message answers the latest question here
            topic, asked = pending.topic, pending.text
    if topic:
        close_ask(deps.state, message.chat.id, thread, topic)
    if topic == planner.EDIT_TOPIC:
        await plan_edit_reply(message, deps, message.text)
        return
    if topic and topic.startswith(review.WHY_TOPIC):
        block_id = int(topic.removeprefix(review.WHY_TOPIC))
        await reply(message, review.note_why(deps.state, block_id, message.text))
        return
    if topic == review.REFLECT_TOPIC:
        await review_reflect(message, deps, message.text)
        return
    if topic and topic.startswith(suggestions.TWEAK_TOPIC):
        await tweak_reply(message, deps, int(topic.removeprefix(suggestions.TWEAK_TOPIC)))
        return
    if topic and topic.startswith(watch.LENGTH_TOPIC):
        item_id = int(topic.removeprefix(watch.LENGTH_TOPIC))
        await reply(message, watch.length_reply(deps.state, item_id, message.text))
        _sync_in_background(deps)
        return
    asked = asked if topic == watch.NOTE_TOPIC else None
    answering = topic == nightly.TOPIC
    session = nightly.load(deps.state)
    today = datetime.now(deps.settings.zone).date()
    still_open = None  # set when a question is answered in chat while tonight's is open
    if answering or (space is Space.BRIEFS and session is not None and session.open):
        if session is not None and not session.accepts(today):
            if space is Space.BRIEFS:
                await reply(
                    message,
                    f"That question was about {date.fromisoformat(session.day):%a %d %b}, "
                    "which has already started. Send /plan to plan the rest of today.",
                )
                return
        elif session is not None and nightly.is_question(message.text):
            space, still_open = Space.CHAT, nightly.still_open_note(session)
        else:
            await nightly_reply(message, deps, message.text)
            return
    space, reflecting = reflect.route(deps.state, space, datetime.now(deps.settings.zone))
    if space is Space.CHAT and onboarding.active(deps.state) and deps.make_llm:
        await onboarding_reply(message, deps, message.text)
        return
    if space is Space.BRIEFS:
        await reply(message, "This topic is for Ultron's briefs. Chat with me in 💬 Chat.")
        return
    if deps.make_llm is None:
        await reply(message, f"{SPACE_LABELS[space]} got it. (Claude isn't set up yet.)")
        return
    thread = message.message_thread_id if message.is_topic_message else None
    await message.chat.send_action(ChatAction.TYPING, message_thread_id=thread)
    try:
        text = message.text
        if asked:  # an answer to "what's the plan?": chat needs the question too
            text = f"(Replying to your message: {asked})\n{text}"
        answer = await chat(
            deps.settings,
            deps.state,
            deps.guard,
            deps.make_llm,
            text,
            space,
            session_id=reflecting.id if reflecting else None,
        )
    except UserFacingError as exc:
        await reply(message, f"❌ {exc}")
        return
    except Exception:  # noqa: BLE001 - always answer the owner, even when Claude errors
        log.exception("Chat reply failed")
        await reply(message, "❌ Something went wrong reaching Claude. I've logged it; try again.")
        return
    text = answer.text + (f"\n\n({answer.note})" if answer.note else "")
    if still_open:
        text += f"\n\n{still_open}"
    rows = [
        [InlineKeyboardButton("↩️ Undo", callback_data=f"undo_fact:{fid}")]
        for fid in answer.effects.remembered
    ]
    if answer.offer_reflect:
        rows.append([InlineKeyboardButton(REFLECT_BUTTON, callback_data="reflect_open")])
    await reply(message, text, InlineKeyboardMarkup(rows) if rows else None)
    for change_id in answer.effects.suggested:
        await send_card(message, deps, change_id)
    if answer.effects.items_changed:
        _sync_in_background(deps)
    if answer.effects.plan_day:
        days, _ = planner.pick_days(answer.effects.plan_day, datetime.now(deps.settings.zone))
        await make_plan(message, deps, days, None)


async def undo_fact_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    query = update.callback_query
    await query.answer()
    await _drop_tapped_row(query)
    store = ItemStore(deps.state)
    item = store.get(int(query.data.split(":", 1)[1]))
    if item is None or item.kind != "fact":
        return
    store.update(item.id, status="dropped")
    await reply(query.message, f"Forgotten: {item.title}")
    _sync_in_background(deps)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log it, and tell the owner, so a failed command never just goes quiet. Network
    blips while polling come with no update and are only logged."""
    log.error("Unhandled error in handler", exc_info=context.error)
    owner = _deps(context).settings.telegram_owner_id
    if getattr(update, "effective_user", None) is None or not is_owner(update, owner):
        return
    error = type(context.error).__name__
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await query.answer()  # stop the button spinning; fails if already answered
        except Exception:  # noqa: BLE001
            pass
    if update.effective_message is None:
        return
    try:
        await reply(
            update.effective_message,
            f"❌ Something went wrong ({error}). It's logged; try again, or check /status.",
        )
    except Exception:  # noqa: BLE001 - the error handler must never raise
        log.exception("Couldn't tell the owner about the error")


def register(app: Application, deps: Deps) -> None:
    app.bot_data["deps"] = deps
    app.add_handler(TypeHandler(Update, owner_gate), group=-1)
    app.add_handler(CommandHandler(["start", "help"], help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("usage", usage_command))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("inbox", inbox_command))
    app.add_handler(CommandHandler("focus", focus_command))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("replan", replan_command))
    app.add_handler(CommandHandler("review", review_command))
    app.add_handler(CommandHandler("brief", brief_command))
    app.add_handler(CommandHandler("weekly", weekly_command))
    app.add_handler(CommandHandler("easy", easy_command))
    app.add_handler(CommandHandler("reflect", reflect_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CallbackQueryHandler(callout_button, pattern=r"^offer:callout:"))
    app.add_handler(CallbackQueryHandler(checkin_button, pattern=r"^offer:checkin:"))
    app.add_handler(CallbackQueryHandler(weekly_button, pattern=r"^offer:weekly:"))
    app.add_handler(CallbackQueryHandler(suggest_button, pattern=r"^offer:suggest:"))
    app.add_handler(CallbackQueryHandler(brief_button, pattern=r"^offer:brief:"))
    app.add_handler(CallbackQueryHandler(review_button, pattern=r"^offer:review:"))
    app.add_handler(CallbackQueryHandler(plan_button, pattern=r"^offer:plan:"))
    app.add_handler(CallbackQueryHandler(watch_button, pattern=r"^offer:watch:"))
    app.add_handler(CallbackQueryHandler(onboarding_button, pattern=r"^onb:"))
    app.add_handler(CallbackQueryHandler(undo_fact_button, pattern=r"^undo_fact:\d+$"))
    app.add_handler(CallbackQueryHandler(reflect_button, pattern=r"^reflect_open$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.add_error_handler(on_error)
    for group, handlers in app.handlers.items():
        if group >= 0:  # the owner gate logs the update itself
            for handler in handlers:
                handler.callback = activity_log.traced(handler.callback)
