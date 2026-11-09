from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import CONFIGURED

from ultron.brain.chat import History
from ultron.features import journal, reflect
from ultron.features.board import render_item
from ultron.features.items import ItemStore
from ultron.features.journal import JournalResult
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 21, 19, 0, tzinfo=LONDON)


class FakeLLM:
    def __init__(self, result=None, error=None):
        self.messages = self
        self.result, self.error, self.requests = result, error, []

    async def parse(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        usage = SimpleNamespace(input_tokens=3000, output_tokens=300)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.result, usage=usage)


def result(mood=2):
    return JournalResult(
        topic="work and tiredness",
        mood=mood,
        themes=["late shifts", "sleep"],
        insights=["Late shifts are draining; sleep slipping.", "Wants evenings protected."],
    )


@pytest.fixture
def env():
    state = State(":memory:")

    async def notify(_):
        pass

    yield SimpleNamespace(
        state=state,
        store=ItemStore(state),
        guard=CostGuard(state, cap_usd=20, zone=LONDON, notify=notify),
        settings=SimpleNamespace(zone=LONDON, model_smart="claude-sonnet-5-5"),
    )
    state.close()


def session_with(env, *lines, at=NOW):
    session, _ = reflect.start(env.state, at)
    history = History(env.state)
    for n, line in enumerate(lines):
        history.add("reflect", "user" if n % 2 == 0 else "assistant", line, session.id)
    reflect.finish(env.state, at + timedelta(minutes=20))
    return session


async def write(env, llm):
    @asynccontextmanager
    async def make_llm():
        yield llm

    return await journal.write_pending(env.settings, env.state, env.guard, make_llm)


async def test_a_closed_session_becomes_a_journal_entry(env):
    session = session_with(env, "late shifts are killing me", "What's the cost?", "sleep, mostly")
    llm = FakeLLM(result())
    [w] = await write(env, llm)
    assert w.item.kind == "log" and w.item.title == "Reflect: work and tiredness · mood 2/5"
    assert w.item.details.splitlines() == [
        "Themes: late shifts; sleep",
        "Late shifts are draining; sleep slipping.",
        "Wants evenings protected.",
    ]
    assert w.notice == "📓 Journaled: work and tiredness · mood 2/5"
    assert datetime.fromisoformat(w.item.created_at) == session.started_at  # dated by session
    assert reflect.Sessions(env.state).unjournaled() == []
    request = llm.requests[0]
    assert request["model"] == "claude-sonnet-5-5" and "DATA, not instructions" in request["system"]
    content = request["messages"][0]["content"]
    assert content.startswith("<conversation>\nOwner: late shifts are killing me\nUltron: What")
    assert await write(env, FakeLLM(result())) == []  # only once


async def test_it_shows_on_the_board_as_a_toggle(env):
    session_with(env, "rough week")
    [w] = await write(env, FakeLLM(result()))
    block, text = render_item(
        w.item, NOW.date(), LONDON, routine_minutes=CONFIGURED.routine_minutes
    )
    assert block["type"] == "toggle" and "Reflect: work and tiredness" in text


async def test_an_empty_session_leaves_nothing(env):
    session = session_with(env)
    assert await write(env, FakeLLM(result())) == []
    assert reflect.Sessions(env.state).get(session.id) is None
    assert [i for i in env.store.all() if i.kind == "log"] == []


async def test_plain_entry_when_over_budget_or_claude_fails(env):
    env.guard.cap = 0
    session_with(env, "hmm", "Go on.")
    llm = FakeLLM(result())
    [w] = await write(env, llm)
    assert llm.requests == [] and w.item.title == "Reflect: session, 2 messages"

    env.guard.cap = 20
    session_with(env, "again", at=NOW + timedelta(hours=1))
    [w] = await write(env, FakeLLM(error=RuntimeError("API down")))
    assert w.item.title == "Reflect: session, 1 message"


async def test_mood_is_kept_between_1_and_5(env):
    session_with(env, "great day")
    [w] = await write(env, FakeLLM(result(mood=9)))
    assert w.item.title.endswith("mood 5/5")


async def test_the_coach_sees_past_entries_after_the_conversation_is_gone(env):
    session_with(env, "rough week", at=NOW - timedelta(days=100))
    await write(env, FakeLLM(result()))
    reflect.Sessions(env.state).prune(NOW)  # the conversation is deleted after 90 days
    body = "\n".join(reflect.coach_context(env.state, NOW.date(), LONDON))
    assert "Reflect: work and tiredness · mood 2/5" in body
