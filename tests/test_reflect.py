from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import make_settings
from test_chat import FakeLLM, response, text

from ultron.brain.chat import History, chat
from ultron.features import reflect
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 21, 19, 0, tzinfo=LONDON)


@pytest.fixture
def state():
    s = State(":memory:")
    yield s
    s.close()


def test_start_is_idempotent_while_open(state):
    first, new = reflect.start(state, NOW)
    assert new
    again, new = reflect.start(state, NOW + timedelta(minutes=5))
    assert again.id == first.id and not new


def test_dm_messages_join_an_open_session_only(state):
    assert reflect.route(state, Space.CHAT, NOW) == (Space.CHAT, None)
    session, _ = reflect.start(state, NOW)
    space, joined = reflect.route(state, Space.CHAT, NOW + timedelta(minutes=20))
    assert space is Space.REFLECT and joined.id == session.id
    # each message keeps it alive: 20 + 25 minutes is fine, 25 after the last one
    space, _ = reflect.route(state, Space.CHAT, NOW + timedelta(minutes=45))
    assert space is Space.REFLECT
    assert reflect.route(state, Space.CHAT, NOW + timedelta(minutes=80)) == (Space.CHAT, None)
    assert reflect.route(state, Space.BRIEFS, NOW) == (Space.BRIEFS, None)


def test_the_reflect_topic_opens_a_session_by_itself(state):
    space, session = reflect.route(state, Space.REFLECT, NOW)
    assert space is Space.REFLECT and session is not None
    assert reflect.current(state, NOW).id == session.id


def test_done_and_idle_close(state):
    assert reflect.finish(state, NOW) is None
    reflect.start(state, NOW)
    assert reflect.finish(state, NOW).end_reason == "done"
    assert reflect.current(state, NOW) is None

    session, _ = reflect.start(state, NOW)
    assert reflect.close_idle(state, NOW + timedelta(minutes=29)) is None
    closed = reflect.close_idle(state, NOW + timedelta(minutes=30))
    assert closed.id == session.id and closed.end_reason == "idle"
    assert reflect.close_idle(state, NOW + timedelta(minutes=40)) is None  # only once


def test_a_stale_session_is_closed_before_a_new_one(state):
    old, _ = reflect.start(state, NOW)
    new, fresh = reflect.start(state, NOW + timedelta(hours=2))
    assert fresh and new.id != old.id
    assert reflect.Sessions(state).get(old.id).end_reason == "idle"


def test_conversations_are_deleted_after_90_days(state):
    history = History(state)
    old, _ = reflect.start(state, NOW - timedelta(days=100))
    history.add("reflect", "user", "an old worry", old.id)
    reflect.finish(state, NOW - timedelta(days=100))
    recent, _ = reflect.start(state, NOW - timedelta(days=10))
    history.add("reflect", "user", "a recent thought", recent.id)
    reflect.finish(state, NOW - timedelta(days=10))
    history.add("chat", "user", "ordinary chat")
    assert reflect.Sessions(state).prune(NOW) == 1
    left = [r["text"] for r in state.db.execute("SELECT text FROM chat_messages")]
    assert left == ["a recent thought", "ordinary chat"]
    assert reflect.Sessions(state).get(old.id) is None


# --- the coach sees only this session ---


@pytest.fixture
def env(state):
    async def notify(_):
        pass

    guard = CostGuard(state, cap_usd=20, zone=LONDON, notify=notify)
    return SimpleNamespace(state=state, guard=guard, settings=make_settings())


async def ask(env, llm, message, session_id):
    @asynccontextmanager
    async def make_llm():
        yield llm

    return await chat(
        env.settings,
        env.state,
        env.guard,
        make_llm,
        message,
        Space.REFLECT,
        now=NOW,
        session_id=session_id,
    )


def sonnet(t):
    return response(text(t), model="claude-sonnet-5-5")


async def test_each_session_starts_fresh(env):
    history = History(env.state)
    env.state.set("chat.summary.reflect", "Talked about the move to Portugal")
    first, _ = reflect.start(env.state, NOW)
    llm = FakeLLM(sonnet("What's behind that?"), sonnet("And then?"))
    await ask(env, llm, "feeling stuck", first.id)
    await ask(env, llm, "work, mostly", first.id)
    assert [m["content"] for m in llm.requests[1]["messages"]] == [
        "feeling stuck",
        "What's behind that?",
        "work, mostly",
    ]
    assert "Portugal" not in llm.requests[0]["system"][1]["text"]  # no old running summary

    reflect.finish(env.state, NOW)
    second, _ = reflect.start(env.state, NOW + timedelta(hours=1))
    llm = FakeLLM(sonnet("Tell me more."))
    await ask(env, llm, "slept badly", second.id)
    assert [m["content"] for m in llm.requests[0]["messages"]] == ["slept badly"]
    assert len(history.recent("reflect", 60, first.id)) == 4  # kept, tagged to its session


async def test_a_long_session_is_never_folded(env, monkeypatch):
    session, _ = reflect.start(env.state, NOW)
    history = History(env.state)
    for n in range(40):
        history.add("reflect", "user" if n % 2 == 0 else "assistant", f"m{n}", session.id)
    llm = FakeLLM(sonnet("Mm."))
    await ask(env, llm, "still here", session.id)
    assert len(llm.requests) == 1  # no summary call
    assert history.to_fold("reflect") == []


# --- in the bot ---


class FakeMessage:
    def __init__(self, body):
        self.text = body
        self.chat = SimpleNamespace(id=1, type="private", send_action=self._typing)
        self.message_thread_id = None
        self.is_topic_message = False
        self.reply_to_message = None
        self.replies = []

    async def reply_text(self, body, **_):
        self.replies.append(body)

    async def _typing(self, *_, **__):
        pass


async def test_reflect_then_done_in_the_dm(state):
    from ultron.bot.handlers import Deps, done_command, reflect_command, text_message

    settings = make_settings(telegram_owner_id=1)
    deps = Deps(settings=settings, state=state, guard=None)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}))

    async def send(body):
        msg = FakeMessage(body)
        handler = {"/reflect": reflect_command, "/done": done_command}.get(body, text_message)
        await handler(SimpleNamespace(effective_message=msg), context)
        return msg.replies

    assert await send("/reflect") == [reflect.OPENING]
    assert await send("/reflect") == [reflect.ALREADY_OPEN]
    assert (await send("been anxious about money"))[0].startswith("🧠 Reflect got it.")
    # Claude isn't set up here, so nothing was said to the coach: no journal entry
    assert await send("/done") == [reflect.CLOSED]
    assert (await send("what's due today?"))[0].startswith("💬 Chat got it.")
    assert await send("/done") == [reflect.NONE_OPEN]


# --- step 2: what the coach knows, and the offer from normal chat ---


def log(store, title, details, day):
    values = dict(project=None, area="Admin", due=None, question=None)
    item = store.add(kind="log", title=title, details=details, **values)
    return store.update(item.id, created_at=f"{day}T12:00:00+00:00")


def test_coach_context_holds_two_weeks_of_history(state):
    from ultron.features.items import ItemStore

    store = ItemStore(state)
    log(
        store,
        "Day review: 1 of 3 done · Balanced",
        "In the way: Admin: skipped (late work)",
        "2026-10-20",
    )
    log(store, "Feeling drained after work", None, "2026-10-15")
    log(store, "Day review: old", None, "2026-10-01")  # outside the 14 days
    slip = store.add(
        kind="task",
        title="Call the accountant",
        details=None,
        project=None,
        area="Admin",
        due=None,
        question=None,
    )
    store.update(slip.id, times_moved=4)
    state.set(f"accountability.called.{slip.id}", "2026-10-19")
    summary = log(store, "Reflect: work and tiredness", "Mood 2/5 · themes: work", "2026-09-01")
    session, _ = reflect.start(state, NOW)
    state.db.execute(
        "UPDATE reflect_sessions SET log_item_id = ? WHERE id = ?", (summary.id, session.id)
    )
    lines = reflect.coach_context(state, NOW.date(), LONDON)
    body = "\n".join(lines)
    assert "- Tue 20 Oct: Day review: 1 of 3 done · Balanced | In the way: Admin: skipped" in body
    assert "- Thu 15 Oct: Feeling drained after work" in body
    assert "Day review: old" not in body
    assert "- 2026-10-19: Call the accountant (pushed back 4 times; now open)" in body
    # an old session summary still shows, under its own heading and only once
    assert body.count("Reflect: work and tiredness") == 1
    assert body.index("Earlier Reflect sessions") < body.index("Reflect: work and tiredness")


def test_empty_history_says_so(state):
    body = "\n".join(reflect.coach_context(state, NOW.date(), LONDON))
    assert body.count("(nothing yet)") == 1 and "(none yet)" in body


async def test_the_coach_gets_the_history(env):
    from ultron.features.items import ItemStore

    log(ItemStore(env.state), "Day review: 2 of 2 done · Balanced", None, "2026-10-20")
    session, _ = reflect.start(env.state, NOW)
    llm = FakeLLM(sonnet("How did that feel?"))
    await ask(env, llm, "good couple of days", session.id)
    assert "Day review: 2 of 2 done" in llm.requests[0]["system"][1]["text"]


async def chat_ask(env, llm, message, space=Space.CHAT):
    @asynccontextmanager
    async def make_llm():
        yield llm

    return await chat(env.settings, env.state, env.guard, make_llm, message, space, now=NOW)


async def test_a_personal_chat_reply_offers_reflect(env):
    llm = FakeLLM(response(text("That sounds heavy. What's weighing most?\n[[reflect]]")))
    answer = await chat_ask(env, llm, "honestly I'm burnt out")
    assert answer.offer_reflect and answer.text == "That sounds heavy. What's weighing most?"
    assert History(env.state).recent("chat")[-1]["content"] == answer.text
    assert "[[reflect]]" in llm.requests[0]["system"][0]["text"]  # the rule is in the prompt

    plain = await chat_ask(env, FakeLLM(response(text("Done."))), "tick off bins")
    assert not plain.offer_reflect


async def test_no_offer_inside_reflect(env):
    session, _ = reflect.start(env.state, NOW)
    answer = await ask(env, FakeLLM(sonnet("Go on.\n[[reflect]]")), "hmm", session.id)
    assert not answer.offer_reflect and answer.text == "Go on."


def test_start_from_chat_carries_the_last_exchange(state):
    history = History(state)
    history.add("chat", "user", "what's due?")
    history.add("chat", "assistant", "Council tax.")
    history.add("chat", "user", "I'm burnt out")
    history.add("chat", "assistant", "What's weighing most?")
    session, new = reflect.start_from_chat(state, NOW)
    assert new
    assert [m["content"] for m in history.recent("reflect", 60, session.id)] == [
        "I'm burnt out",
        "What's weighing most?",
    ]
    _, new = reflect.start_from_chat(state, NOW)
    assert not new and len(history.recent("reflect", 60, session.id)) == 2  # not copied twice


async def test_the_button_opens_a_session_and_keeps_other_buttons(state):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from ultron.bot.handlers import REFLECT_BUTTON, Deps, reflect_button

    History(state).add("chat", "user", "I'm burnt out")
    History(state).add("chat", "assistant", "What's weighing most?")
    deps = Deps(settings=make_settings(telegram_owner_id=1), state=state, guard=None)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}))
    message = FakeMessage("What's weighing most?")
    message.reply_markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("↩️ Undo", callback_data="undo_fact:7")],
            [InlineKeyboardButton(REFLECT_BUTTON, callback_data="reflect_open")],
        ]
    )
    edits = []

    async def answer():
        pass

    async def edit(reply_markup):
        edits.append(reply_markup)

    query = SimpleNamespace(
        data="reflect_open", message=message, answer=answer, edit_message_reply_markup=edit
    )
    await reflect_button(SimpleNamespace(callback_query=query), context)
    assert message.replies == [reflect.FROM_CHAT]
    [kept] = edits
    assert [[b.text for b in row] for row in kept.inline_keyboard] == [["↩️ Undo"]]
    assert reflect.current(state, datetime.now(LONDON)) is not None
