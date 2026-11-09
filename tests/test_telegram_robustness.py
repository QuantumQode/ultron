"""Telegram edge cases: long and empty messages, errors that must never go quiet."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import OWNER, make_settings

from ultron.bot import handlers
from ultron.bot.handlers import Deps, on_error, sync_command
from ultron.bot.notifier import EMPTY, TELEGRAM_LIMIT, TelegramNotifier, split_message
from ultron.notify import Space
from ultron.ops.status import health_lines
from ultron.state import State

LONDON = ZoneInfo("Europe/London")


def units(text):
    return len(text.encode("utf-16-le")) // 2


def lines_of(text):
    return [line.rstrip() for line in text.splitlines()]


# --- splitting ---


def test_short_text_is_one_piece():
    assert split_message("Hello") == ["Hello"]


def test_empty_text_becomes_a_placeholder():
    # Telegram refuses empty messages (e.g. a chat reply that was only a button marker)
    assert split_message("") == [EMPTY]
    assert split_message("  \n ") == [EMPTY]


def test_long_text_splits_at_line_breaks_within_the_limit():
    lines = [f"Line {i}: " + "word " * 20 for i in range(100)]
    text = "\n".join(lines)
    pieces = split_message(text)
    assert len(pieces) > 1
    assert all(units(p) <= TELEGRAM_LIMIT for p in pieces)
    assert all(p.startswith("Line ") for p in pieces)  # never cut mid-line
    assert lines_of("\n".join(pieces)) == lines_of(text)  # only edge spaces trimmed


def test_emoji_count_double_toward_the_limit():
    pieces = split_message("🔥" * 3000)  # 6,000 UTF-16 units
    assert all(units(p) <= TELEGRAM_LIMIT for p in pieces)
    assert "".join(pieces) == "🔥" * 3000


def test_text_without_spaces_is_cut_hard():
    pieces = split_message("x" * 9000)
    assert [len(p) for p in pieces] == [4096, 4096, 808]


# --- the notifier ---


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(chat_id=kwargs["chat_id"], message_id=len(self.sent))


async def test_long_offer_keeps_all_its_text_and_puts_buttons_last():
    bot = FakeBot()
    notifier = TelegramNotifier(bot, make_settings())
    text = "\n".join(f"Point {i}: " + "detail " * 30 for i in range(40))
    await notifier.offer(text, "weekly", [("skip", "Skip")], Space.BRIEFS)
    assert len(bot.sent) > 1
    assert lines_of("\n".join(m["text"] for m in bot.sent)) == lines_of(text)  # nothing lost
    assert [m["reply_markup"] is not None for m in bot.sent] == [False] * (len(bot.sent) - 1) + [
        True
    ]


async def test_long_question_is_recorded_on_its_last_part():
    state = State(":memory:")
    bot = FakeBot()
    notifier = TelegramNotifier(bot, make_settings(), state)
    await notifier.ask("word " * 2000, "plan_edit")
    from ultron.bot.notifier import topic_for

    last = len(bot.sent)
    assert topic_for(state, OWNER, last) == "plan_edit"
    state.close()


# --- errors reach the owner ---


class FakeMessage:
    def __init__(self):
        self.message_thread_id = None
        self.is_topic_message = False
        self.replies = []

    async def reply_text(self, text, **_):
        self.replies.append(text)


def context_for(deps, error=None):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}), error=error)


@pytest.fixture
def deps():
    state = State(":memory:")
    yield Deps(settings=make_settings(), state=state, guard=None)
    state.close()


async def test_crashed_command_tells_the_owner(deps):
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=OWNER), effective_message=message, callback_query=None
    )
    await on_error(update, context_for(deps, KeyError("x")))
    assert message.replies == [
        "❌ Something went wrong (KeyError). It's logged; try again, or check /status."
    ]


async def test_crashed_button_stops_spinning_and_says_so(deps):
    message = FakeMessage()
    answered = []

    async def answer(*_args, **_kw):
        answered.append(True)
        raise RuntimeError("query is too old")  # the handler had already answered it

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=OWNER),
        effective_message=message,
        callback_query=SimpleNamespace(answer=answer),
    )
    await on_error(update, context_for(deps, ValueError("bad")))
    assert answered and "ValueError" in message.replies[0]


async def test_polling_errors_and_strangers_get_nothing(deps):
    await on_error(None, context_for(deps, TimeoutError()))  # no update: nothing to answer
    message = FakeMessage()
    stranger = SimpleNamespace(
        effective_user=SimpleNamespace(id=OWNER + 1), effective_message=message, callback_query=None
    )
    await on_error(stranger, context_for(deps, KeyError("x")))
    assert message.replies == []


async def test_sync_command_reports_a_failure_instead_of_going_quiet(monkeypatch):
    state = State(":memory:")

    async def broken_sync(*_args, **_kw):
        raise RuntimeError("Notion said no")

    monkeypatch.setattr(handlers, "run_sync", broken_sync)

    async def notify(*_):
        pass

    settings = make_settings(notion_token="secret", notion_root_page_id="root")
    deps = Deps(
        settings=settings,
        state=state,
        guard=None,
        notifier=SimpleNamespace(send=notify),
        make_llm=lambda: None,
    )
    message = FakeMessage()
    update = SimpleNamespace(effective_message=message)
    await sync_command(update, context_for(deps))
    assert message.replies == [
        "Syncing your Inbox and Board…",
        "❌ The Notion sync failed: RuntimeError: Notion said no",
    ]
    [job] = state.job_runs()
    assert job.job == "sync" and job.consecutive_failures == 1
    state.close()


# --- job health for the chat ---


def test_health_lines_explain_failures_and_recoveries():
    state = State(":memory:")
    now = datetime(2026, 10, 22, 9, 0, tzinfo=LONDON)
    assert health_lines(state, now) == [
        "- All scheduled jobs (email check, Notion sync, rituals) are running fine."
    ]

    state.record_job_failure("sync", "APIResponseError: text too long")
    [line] = health_lines(state, now.astimezone(UTC) + timedelta(minutes=1))
    assert line.startswith("- sync: FAILING (1 run(s) in a row)")
    assert "text too long" in line

    state.record_job_success("sync")
    [line] = health_lines(state, datetime.now(LONDON))
    assert line.startswith("- sync: recovered.") and "working again since" in line

    later = datetime.now(LONDON) + timedelta(days=2)
    assert "running fine" in health_lines(state, later)[0]
    state.close()
