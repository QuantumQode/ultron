"""Start Ultron: load settings, open state, wire safeguards and run the Telegram bot."""

import functools
import logging
import sys
from datetime import datetime, timedelta

from pydantic import ValidationError
from telegram import Update
from telegram.ext import Application
from telegram.request import HTTPXRequest

from ultron.bot.activity_log import LoggedBot
from ultron.bot.handlers import Deps, register
from ultron.bot.notifier import TelegramNotifier
from ultron.brain.llm import make_client as make_llm_client
from ultron.config import WEEKDAYS, Settings, get_settings
from ultron.features import (
    accountability,
    brief,
    focus,
    journal,
    nightly,
    onboarding,
    planner,
    profile,
    reflect,
    review,
    rituals,
    weekly,
)
from ultron.features.email_report import in_quiet_hours
from ultron.features.items import ItemStore
from ultron.features.mail_sync import run_mail
from ultron.features.sync import run_sync
from ultron.features.workspace import setup_workspace
from ultron.notify import Notifier, Space
from ultron.ops import activity
from ultron.ops.backup import run_backup
from ultron.ops.guard import CostGuard, run_job
from ultron.state import State

log = logging.getLogger("ultron")

SYNC_EVERY_SECONDS = 3600


def scheduled(name: str):
    """A timed job runs "caused by" its name, so what it does is tagged in the activity log."""

    def wrap(job):
        @functools.wraps(job)
        async def run(context) -> None:
            with activity.caused_by(f"scheduled: {name}"):
                await job(context)

        return run

    return wrap


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # the Anthropic SDK's client


async def on_startup(
    settings: Settings, state: State, notifier: Notifier, guard: CostGuard, make_llm
) -> None:
    """One-off jobs when the bot starts. Failures are retried and reported, never fatal."""
    if settings.notion_enabled:
        await run_job(
            "notion_setup",
            lambda: setup_workspace(settings, state, notifier),
            state=state,
            notify=notifier.send,
        )
    if onboarding.should_offer(state, ItemStore(state)):
        await notifier.send(onboarding.start(state), Space.CHAT)
    profile.show_on_board(state, ItemStore(state))  # the "Planning settings: …" line
    now = datetime.now(settings.zone)
    if brief.due_now(state, now, settings.morning_brief_time):
        # The bot wasn't running at brief time: send this morning's brief now
        await run_job(
            "brief",
            lambda: brief.send_brief(settings, state, notifier, guard, make_llm),
            state=state,
            notify=notifier.send,
        )
    if weekly.due_now(
        state,
        now,
        settings.weekly_review_day,
        settings.weekly_review_time,
        settings.quiet_hours_start,
    ):
        await run_job(
            "weekly",
            lambda: weekly.run_weekly(settings, state, guard, notifier, make_llm),
            state=state,
            notify=notifier.send,
        )
    if review.due_now(state, now, settings.nightly_time, settings.quiet_hours_start):
        # The PC was off (or the bot down) at 21:00: catch up tonight's review
        await review.start(state, notifier, now)


def main() -> None:
    setup_logging()
    try:
        settings = get_settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        sys.exit(f"Ultron can't start. Fix these settings in .env: {missing}")

    state = State(settings.db_path)
    activity.attach(state.db, settings.secret_values())
    activity.record("system", "Ultron started")
    rituals.load(settings, state)  # ritual times changed from suggestion cards
    app = (
        Application.builder()
        .bot(
            LoggedBot(  # the same connection pools ApplicationBuilder would set up
                settings.telegram_token.get_secret_value(),
                request=HTTPXRequest(connection_pool_size=256),
                get_updates_request=HTTPXRequest(connection_pool_size=1),
            )
        )
        .post_init(lambda app: on_startup(settings, state, notifier, guard, make_llm))
        .build()
    )
    notifier = TelegramNotifier(app.bot, settings, state)
    guard = CostGuard(
        state,
        cap_usd=settings.monthly_cost_cap_usd,
        zone=settings.zone,
        notify=notifier.send,
    )
    make_llm = lambda: make_llm_client(settings)  # noqa: E731
    deps = Deps(settings=settings, state=state, guard=guard, notifier=notifier, make_llm=make_llm)
    register(app, deps)

    @scheduled("brief")
    async def brief_job(_context) -> None:
        await run_job(
            "brief",
            lambda: brief.send_brief(settings, state, notifier, guard, make_llm),
            state=state,
            notify=notifier.send,
        )

    @scheduled("weekly")
    async def weekly_job(_context) -> None:
        await run_job(
            "weekly",
            lambda: weekly.run_weekly(settings, state, guard, notifier, make_llm),
            state=state,
            notify=notifier.send,
        )

    @scheduled("checkin")
    async def checkin_job(_context) -> None:
        await accountability.send_checkin(state, notifier, datetime.now(settings.zone))

    @scheduled("nightly")
    async def nightly_job(_context) -> None:
        """21:00: the evening review. Planning tomorrow follows when it's finished."""
        await review.start(state, notifier, datetime.now(settings.zone))

    @scheduled("review_check")
    async def review_check(_context) -> None:
        """A review left unfinished is closed after 30 minutes, so planning still happens."""
        if review.overdue(state, datetime.now(settings.zone)):
            await run_job(
                "nightly",
                lambda: review.finish(settings, state, guard, notifier, make_llm, reflection=None),
                state=state,
                notify=notifier.send,
            )

    @scheduled("reflect_check")
    async def reflect_check(_context) -> None:
        """A 🧠 Reflect session left quiet for 30 minutes is closed and journaled, and any
        ended session still without its 📓 entry gets one. Silent in quiet hours."""
        now = datetime.now(settings.zone)
        closed = reflect.close_idle(state, now)
        written = await journal.write_pending(settings, state, guard, make_llm)
        if in_quiet_hours(now, settings.quiet_hours_start, settings.quiet_hours_end):
            return
        if closed:
            await notifier.send(reflect.IDLE_CLOSED, Space.REFLECT)
        for w in written:
            await notifier.send(w.notice, Space.REFLECT)

    @scheduled("nightly_nudge")
    async def nightly_nudge(_context) -> None:
        """No reply to tonight's question: send a ready draft instead of a reminder."""

        async def draft(days) -> None:
            await planner.plan_and_offer(
                settings,
                state,
                guard,
                notifier,
                make_llm,
                days=days,
                priorities=None,
                intro=nightly.AUTO_DRAFT_INTRO,
            )

        await nightly.nudge(state, notifier, datetime.now(settings.zone), draft=draft)

    @scheduled("backup")
    async def backup_job(_context) -> None:
        async def job() -> None:
            report = await run_backup(settings, state)
            log.info(report.summary)

        await run_job("backup", job, state=state, notify=notifier.send)

    def schedule_rituals() -> None:
        """(Re)schedule the timed rituals from the current settings. Run at startup and
        again when a suggestion card changes a ritual's time."""
        for name in ("nightly", "nightly_nudge", "brief", "checkin", "weekly"):
            for job in app.job_queue.get_jobs_by_name(name):
                job.schedule_removal()
        at = settings.nightly_time.replace(tzinfo=settings.zone)
        nudge_at = (
            datetime.combine(datetime.now(settings.zone).date(), settings.nightly_time)
            + timedelta(minutes=settings.nightly_nudge_minutes)
        ).time()
        app.job_queue.run_daily(nightly_job, time=at, name="nightly")
        app.job_queue.run_daily(
            nightly_nudge, time=nudge_at.replace(tzinfo=settings.zone), name="nightly_nudge"
        )
        app.job_queue.run_daily(
            brief_job, time=settings.morning_brief_time.replace(tzinfo=settings.zone), name="brief"
        )
        if settings.midday_checkin:
            app.job_queue.run_daily(
                checkin_job,
                time=settings.midday_checkin_time.replace(tzinfo=settings.zone),
                name="checkin",
            )
        # The job queue numbers days 0-6 from Sunday
        weekly_day = (WEEKDAYS.index(settings.weekly_review_day) + 1) % 7
        app.job_queue.run_daily(
            weekly_job,
            time=settings.weekly_review_time.replace(tzinfo=settings.zone),
            days=(weekly_day,),
            name="weekly",
        )

    schedule_rituals()
    deps.reschedule = schedule_rituals
    app.job_queue.run_repeating(review_check, interval=600, first=60, name="review_check")
    app.job_queue.run_repeating(reflect_check, interval=600, first=90, name="reflect_check")
    app.job_queue.run_daily(
        backup_job, time=settings.backup_time.replace(tzinfo=settings.zone), name="backup"
    )

    if settings.notion_enabled or settings.mail_enabled:

        @scheduled("hourly_sync")
        async def hourly_sync(_context) -> None:
            store = ItemStore(state)
            ended = focus.expire(state, store, datetime.now(settings.zone).date())
            if ended:
                now_focus = focus.current(state).label
                await notifier.send(
                    f"Your {ended.label} focus has ended. Back to {now_focus}. "
                    "Change it any time with /focus.",
                    Space.CHAT,
                )
            # Mail first, so to-dos found in email reach the Board in the same sync
            if settings.mail_enabled:
                await run_job(
                    "mail",
                    lambda: run_mail(settings, state, guard, notifier, make_llm),
                    state=state,
                    notify=notifier.send,
                )
            if settings.notion_enabled:
                await run_job(
                    "sync",
                    lambda: run_sync(settings, state, guard, notifier, make_llm),
                    state=state,
                    notify=notifier.send,
                )

        app.job_queue.run_repeating(hourly_sync, interval=SYNC_EVERY_SECONDS, first=120)

    log.info("Ultron is running. Press Ctrl+C to stop.")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        state.close()


if __name__ == "__main__":
    main()
