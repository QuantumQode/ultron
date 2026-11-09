from contextlib import asynccontextmanager
from datetime import UTC, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ultron.features import brief
from ultron.features.items import ItemStore
from ultron.features.schedule import PlanStore
from ultron.features.triage import EmailStore, Triaged
from ultron.integrations.mail import Email
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
THU_7AM = datetime(2026, 10, 22, 7, 0, tzinfo=LONDON)


class FakeNotifier:
    def __init__(self):
        self.offers = []

    async def send(self, text, space=Space.BRIEFS):
        pass

    async def offer(self, text, topic, choices, space=Space.BRIEFS):
        self.offers.append((text, topic, choices, space))


class LineLLM:
    def __init__(
        self, text="Work day: 3 hours after 18:30. Gym first, then the GP call.", fail=False
    ):
        self.messages = self
        self.text, self.fail, self.requests = text, fail, []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail:
            raise RuntimeError("API down")
        usage = SimpleNamespace(input_tokens=800, output_tokens=30)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], usage=usage)


@pytest.fixture
def env():
    state = State(":memory:")
    store = ItemStore(state)

    async def notify(_):
        pass

    def add(kind, title, **kw):
        values = dict(details=None, project=None, area="Admin", due=None, question=None)
        values.update(kw)
        return store.add(kind=kind, title=title, **values)

    add("schedule", "Work", days="thu,sun", start_time="07:00", end_time="18:00", travel_minutes=30)
    add(
        "schedule",
        "Sleep",
        days="mon,tue,wed,thu,fri,sat,sun",
        start_time="22:00",
        end_time="06:00",
    )
    add("task", "Book GP appointment", due="2026-10-21", area="Health")
    add("task", "Pay council tax", due="2026-10-22", area="Wealth")
    add("task", "Old email thing", due="2026-10-01", question="Still needed?")
    PlanStore(state).add(THU_7AM.date(), "Workout", start="18:30", end="19:45")
    mum = Email(
        1, "<1>", "mum@x.com", "Mum", "Sunday", datetime(2026, 10, 21, 22, 15, tzinfo=UTC), ""
    )
    old = Email(
        2, "<2>", "bank@x.com", "Bank", "Statement", datetime(2026, 10, 20, 9, 0, tzinfo=UTC), ""
    )
    EmailStore(state).save(
        [
            Triaged(mum, "important", "person", "Asked about Sunday lunch", True),
            Triaged(old, "important", "money", "Old statement", False),
        ]
    )
    yield SimpleNamespace(
        state=state,
        notifier=FakeNotifier(),
        guard=CostGuard(state, cap_usd=20, zone=LONDON, notify=notify),
        settings=SimpleNamespace(zone=LONDON, model_fast="claude-haiku-5-5"),
    )
    state.close()


def test_brief_has_the_day_plan_top3_overdue_and_overnight_email(env):
    text = brief.build(env.state, THU_7AM).text
    lines = text.splitlines()
    assert lines[0] == "☀️ Good morning · Thu 22 Oct · focus: Balanced"
    assert lines[1] == "Work 06:30–18:30 · 3h free"
    assert "Today's plan:" in lines and "• 18:30–19:45 Workout (~1h 15m)" in lines
    assert "1. Book GP appointment (overdue 1 day)" in lines
    assert "2. Pay council tax (due today)" in lines
    assert "Old email thing" not in text  # unconfirmed "Still needed?" items stay out
    assert "⚠️ Overdue: Book GP appointment" in lines
    assert "👤 Mum: Asked about Sunday lunch" in lines
    assert "Old statement" not in text  # not overnight


def test_no_plan_points_to_plan(env):
    for b in PlanStore(env.state).for_day(THU_7AM.date()):
        PlanStore(env.state).set_status(b.id, "dropped")
    assert (
        "No plan for today yet. Tap 📝 Plan today for a draft."
        in brief.build(env.state, THU_7AM).text
    )


async def test_no_plan_offers_a_plan_button(env):
    for b in PlanStore(env.state).for_day(THU_7AM.date()):
        PlanStore(env.state).set_status(b.id, "dropped")
    await send(env, LineLLM())
    [(_, _, choices, _)] = env.notifier.offers
    assert choices == brief.PLAN_CHOICES  # [📝 Plan today] instead of [🔁 Replan today]


async def send(env, llm, **kw):
    @asynccontextmanager
    async def make_llm():
        yield llm

    return await brief.send_brief(
        env.settings, env.state, env.notifier, env.guard, make_llm, now=THU_7AM, **kw
    )


async def test_send_adds_the_coach_line_and_the_replan_button_once_a_day(env):
    llm = LineLLM()
    sent = await send(env, llm)
    [(text, topic, choices, space)] = env.notifier.offers
    assert text.splitlines()[1] == "💬 Work day: 3 hours after 18:30. Gym first, then the GP call."
    assert topic == brief.TOPIC and choices == brief.CHOICES and space is Space.BRIEFS
    assert llm.requests[0]["model"] == "claude-haiku-5-5"
    assert "<context>" in llm.requests[0]["messages"][0]["content"] and sent
    assert await send(env, LineLLM()) is None  # not twice
    assert await send(env, LineLLM(), force=True)  # /brief sends it again


async def test_brief_still_goes_out_if_the_model_fails(env):
    await send(env, LineLLM(fail=True))
    [(text, *_)] = env.notifier.offers
    assert text.splitlines()[1] == "Work 06:30–18:30 · 3h free"


async def test_over_budget_skips_only_the_line(env):
    env.state.record_usage(
        model="m",
        purpose="x",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_usd=25.0,
    )
    llm = LineLLM()
    await send(env, llm)
    assert llm.requests == [] and len(env.notifier.offers) == 1


def test_catch_up_window(env):
    at = time(7, 0)
    assert brief.due_now(env.state, THU_7AM.replace(hour=9), at)
    assert not brief.due_now(env.state, THU_7AM.replace(hour=6, minute=59), at)
    assert not brief.due_now(env.state, THU_7AM.replace(hour=12, minute=30), at)
    env.state.set(brief.KEY_LAST, "2026-10-22")
    assert not brief.due_now(env.state, THU_7AM.replace(hour=9), at)
