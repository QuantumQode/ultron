import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import GROUP, OWNER, make_settings
from telegram.ext import ApplicationHandlerStop

from ultron.bot.format import format_status, format_usage
from ultron.bot.handlers import Deps, owner_gate
from ultron.bot.routing import Destination, Origin, destination_for, space_for
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.ops.status import build_status, build_usage
from ultron.state import State

LONDON = ZoneInfo("Europe/London")

# --- routing ---


def test_private_chat_is_chat(settings, group_settings):
    for s in (settings, group_settings):
        assert space_for(Origin(OWNER, "private", None), s) is Space.CHAT


def test_any_group_is_chat_until_a_group_is_configured(settings):
    assert space_for(Origin(-999, "supergroup", 7), settings) is Space.CHAT


@pytest.mark.parametrize(
    ("thread", "space"),
    [(2, Space.CHAT), (3, Space.REFLECT), (4, Space.BRIEFS), (None, Space.CHAT), (99, Space.CHAT)],
)
def test_configured_group_routes_by_topic(group_settings, thread, space):
    assert space_for(Origin(GROUP, "supergroup", thread), group_settings) is space


def test_other_groups_and_channels_ignored(group_settings):
    assert space_for(Origin(-999, "supergroup", 3), group_settings) is None
    assert space_for(Origin(GROUP, "channel", None), group_settings) is None


def test_outgoing_messages_fall_back_to_owner_dm(settings):
    assert destination_for(Space.BRIEFS, settings) == Destination(OWNER, None)


def test_outgoing_messages_go_to_topic(group_settings):
    assert destination_for(Space.REFLECT, group_settings) == Destination(GROUP, 3)


def test_unset_topic_goes_to_general():
    s = make_settings(telegram_group_id=GROUP)
    assert destination_for(Space.BRIEFS, s) == Destination(GROUP, None)


# --- owner gate ---


def fake_update(user_id, chat_id=OWNER, chat_type="private", thread=None):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        message_thread_id=thread,
        is_topic_message=thread is not None,
    )
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(effective_user=user, effective_message=message)


def fake_context(settings):
    deps = Deps(settings=settings, state=None, guard=None)
    return SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}))


async def test_gate_lets_owner_through(group_settings):
    await owner_gate(fake_update(OWNER), fake_context(group_settings))
    await owner_gate(fake_update(OWNER, GROUP, "supergroup", 3), fake_context(group_settings))


@pytest.mark.parametrize("user_id", [222, None])
async def test_gate_drops_strangers(settings, user_id):
    with pytest.raises(ApplicationHandlerStop):
        await owner_gate(fake_update(user_id), fake_context(settings))


async def test_gate_drops_owner_in_unserved_group(group_settings):
    with pytest.raises(ApplicationHandlerStop):
        await owner_gate(fake_update(OWNER, -999, "supergroup"), fake_context(group_settings))


# --- reports ---


async def test_status_and_usage_reports(settings):
    state = State(":memory:")

    async def notify(_text):
        pass

    guard = CostGuard(state, cap_usd=20, zone=LONDON, notify=notify)
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=0)
    await guard.record(model="claude-sonnet-5-5", purpose="chat", usage=usage)
    state.record_job_failure("email_poll", "TimeoutError: imap")

    status = format_status(
        build_status(settings, state, guard),
        LONDON,
        chat_id=OWNER,
        thread_id=None,
        space=Space.CHAT,
    )
    assert "Notion: ⚪ not set up (phase 2)" in status
    assert "email_poll: ❌ ×1" in status and "TimeoutError: imap" in status
    assert "$2.00 of $20.00" in status
    assert f"chat id: {OWNER}" in status

    usage_text = format_usage(build_usage(state, guard))
    assert "$2.00 of $20.00 (10%)" in usage_text and "chat: $2.00" in usage_text
    state.close()


# --- architecture rule: only ultron/bot may import telegram ---


def test_core_does_not_import_telegram():
    root = Path(__file__).parents[1] / "ultron"
    offenders = []
    for path in root.rglob("*.py"):
        if path.relative_to(root).parts[0] in ("bot", "main.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "telegram" or n.startswith("telegram.") for n in names):
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"Core modules must not import telegram: {offenders}"


# --- replies to the 21:00 question ---


class FakeMessage:
    def __init__(self, text, reply_to=None):
        self.text = text
        self.chat = SimpleNamespace(id=OWNER, type="private")
        self.message_thread_id = None
        self.is_topic_message = False
        self.reply_to_message = reply_to
        self.replies = []

    async def reply_text(self, text, **_):
        self.replies.append(text)


async def test_reply_to_nightly_question_is_taken_as_priorities(settings):
    import json

    from ultron.bot.handlers import text_message
    from ultron.bot.notifier import record_ask
    from ultron.features import nightly

    state = State(":memory:")
    tomorrow = datetime.now(settings.zone).date() + timedelta(days=1)
    state.set(nightly.KEY_SESSION, json.dumps({"day": tomorrow.isoformat(), "sent_at": "x"}))
    record_ask(state, OWNER, 55, nightly.TOPIC)
    deps = Deps(settings=settings, state=state, guard=None)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}))

    question = SimpleNamespace(chat_id=OWNER, message_id=55, text="Your priorities?")
    msg = FakeMessage("Builders, then gym", reply_to=question)
    await text_message(SimpleNamespace(effective_message=msg), context)
    assert nightly.load(state).priorities == "Builders, then gym"
    # the answer goes straight on to drafting a plan (no Claude here, so it says so)
    assert msg.replies == ["Got it. Drafting tomorrow's plan…", "Claude isn't set up yet."]

    # a normal message (not a reply) is ordinary chat, not priorities
    other = FakeMessage("what's my GP number?")
    await text_message(SimpleNamespace(effective_message=other), context)
    assert nightly.load(state).priorities == "Builders, then gym"
    assert "Claude isn't set up yet" in other.replies[0]
    state.close()


async def nightly_setup(settings, day):
    import json

    from ultron.bot.notifier import record_ask
    from ultron.features import nightly

    state = State(":memory:")
    state.set(nightly.KEY_SESSION, json.dumps({"day": day.isoformat(), "sent_at": "x"}))
    record_ask(state, OWNER, 55, nightly.TOPIC)
    deps = Deps(settings=settings, state=state, guard=None)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}))
    return state, context, SimpleNamespace(chat_id=OWNER, message_id=55, text="Your priorities?")


async def test_a_question_in_reply_is_answered_and_the_question_stays_open(settings):
    from ultron.bot.handlers import text_message
    from ultron.features import nightly

    tomorrow = datetime.now(settings.zone).date() + timedelta(days=1)
    state, context, question = await nightly_setup(settings, tomorrow)
    msg = FakeMessage("What day is tomorrow now", reply_to=question)
    await text_message(SimpleNamespace(effective_message=msg), context)
    assert nightly.load(state).priorities is None  # not taken as priorities
    assert "Claude isn't set up yet" in msg.replies[0]  # went to chat instead
    state.close()


async def test_a_reply_after_the_day_has_started_never_plans_it(settings):
    from ultron.bot.handlers import text_message
    from ultron.features import nightly

    today = datetime.now(settings.zone).date()
    state, context, question = await nightly_setup(settings, today)
    msg = FakeMessage("Builders, then gym", reply_to=question)
    await text_message(SimpleNamespace(effective_message=msg), context)
    assert nightly.load(state).priorities is None
    assert "Drafting" not in " ".join(msg.replies)
    state.close()


# --- questions without ForceReply (2026-10-25) ---


async def test_a_question_has_no_forced_reply_box_and_stays_open_for_30_minutes():
    from datetime import UTC

    from test_telegram_robustness import FakeBot

    from ultron.bot.notifier import OPEN_FOR, TelegramNotifier, open_question

    state = State(":memory:")
    bot = FakeBot()
    await TelegramNotifier(bot, make_settings(), state).ask("What got in the way?", "review_why:7")
    assert bot.sent[-1]["reply_markup"] is None  # nothing pinned in the message box
    asked = open_question(state, OWNER, None)
    assert (asked.topic, asked.text) == ("review_why:7", "What got in the way?")
    later = datetime.now(UTC) + OPEN_FOR + timedelta(minutes=1)
    assert open_question(state, OWNER, None, later) is None
    state.close()


async def test_a_plain_message_answers_the_open_question_once(settings):
    from ultron.bot.handlers import text_message
    from ultron.bot.notifier import open_ask, open_question
    from ultron.features import nightly

    tomorrow = datetime.now(settings.zone).date() + timedelta(days=1)
    state, context, _ = await nightly_setup(settings, tomorrow)
    open_ask(state, OWNER, None, 55, nightly.TOPIC, "Your priorities for tomorrow?")
    question = FakeMessage("what's on tomorrow?")  # a question: goes to chat, stays open
    await text_message(SimpleNamespace(effective_message=question), context)
    assert nightly.load(state).priorities is None and open_question(state, OWNER, None)
    msg = FakeMessage("Builders, then gym")  # no swipe-reply needed
    await text_message(SimpleNamespace(effective_message=msg), context)
    assert nightly.load(state).priorities == "Builders, then gym"
    assert open_question(state, OWNER, None) is None  # answered: closed
    state.close()


async def test_a_reply_to_an_older_question_leaves_the_newest_open(settings):
    from ultron.bot.handlers import text_message
    from ultron.bot.notifier import open_ask, open_question
    from ultron.features import nightly

    tomorrow = datetime.now(settings.zone).date() + timedelta(days=1)
    state, context, question = await nightly_setup(settings, tomorrow)
    open_ask(state, OWNER, None, 56, "plan_edit", "What should change?")
    msg = FakeMessage("Gym first", reply_to=question)  # swipe-reply to the 21:00 question
    await text_message(SimpleNamespace(effective_message=msg), context)
    assert nightly.load(state).priorities == "Gym first"
    assert open_question(state, OWNER, None).topic == "plan_edit"
    state.close()
