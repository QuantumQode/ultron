from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ultron.features import focus, habits, weekly
from ultron.features.items import ItemStore
from ultron.features.schedule import PlanStore
from ultron.features.weekly import WeeklyResult
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
SUN = date(2026, 10, 25)
SUN_7PM = datetime(2026, 10, 25, 19, 0, tzinfo=LONDON)


class FakeNotifier:
    def __init__(self):
        self.offers = []

    async def send(self, text, space=Space.BRIEFS):
        pass

    async def offer(self, text, topic, choices, space=Space.BRIEFS):
        self.offers.append((text, topic, choices))


class FakeLLM:
    def __init__(self, result):
        self.messages = self
        self.result, self.requests = result, []

    async def parse(self, **kwargs):
        self.requests.append(kwargs)
        usage = SimpleNamespace(input_tokens=6000, output_tokens=600)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.result, usage=usage)


def result(focus_name="Wealth", objectives=("Close 1 Acme client", "Book GP")):
    return WeeklyResult(
        wins=["Gym 4 of 4 days"],
        misses=["Builders call slipped 3 times"],
        themes=["Tired after late nights"],
        focus=focus_name,
        focus_why="Acme has no paying client yet.",
        objectives=list(objectives),
    )


@pytest.fixture
def env():
    state = State(":memory:")
    store, plans = ItemStore(state), PlanStore(state)

    async def notify(_):
        pass

    def add(kind, title, **kw):
        values = dict(details=None, project=None, area="Admin", due=None, question=None)
        values.update(kw)
        return store.add(kind=kind, title=title, **values)

    gym = add("task", "Workout", area="Health")
    calls = add("task", "Call builders", area="Wealth")
    for n, (title, item, status) in enumerate(
        [("Workout", gym, "done"), ("Call builders", calls, "skipped"), ("Workout", gym, "done")]
    ):
        b = plans.add(SUN - timedelta(days=n), title, start="09:00", end="10:00", item_id=item.id)
        plans.set_status(b.id, status)
    plans.add(SUN - timedelta(days=10), "Last week", start="09:00", end="10:00")  # outside
    add("task", "Pay council tax", area="Wealth", due="2026-11-01", amount="£120.00")
    add("task", "Pay later bill", area="Wealth", due="2026-12-20", amount="£50")  # too far
    spanish = add("habit", "Learn Spanish")
    for n in range(5):
        habits.keep(store, spanish.id, SUN - timedelta(days=n))
    yield SimpleNamespace(
        state=state,
        store=store,
        notifier=FakeNotifier(),
        guard=CostGuard(state, cap_usd=20, zone=LONDON, notify=notify),
        settings=SimpleNamespace(zone=LONDON, model_smart="claude-sonnet-5-5"),
    )
    state.close()


def test_collect_counts_the_week(env):
    s = weekly.collect(env.state, SUN)
    assert s.start == date(2026, 10, 19) and (s.done, s.planned) == (2, 3)
    assert s.per_area == {"Health": (2, 2), "Wealth": (0, 1)}
    assert s.habit_lines == ["Learn Spanish: 5/7 · 🔥 5"]
    assert s.money == ["Pay council tax £120.00 · due Sun 01 Nov"]
    assert weekly.stats_text(s)[0] == "Commitments: 2 of 3 done (67%)"


async def run(env, llm, **kw):
    @asynccontextmanager
    async def make_llm():
        yield llm

    return await weekly.run_weekly(
        env.settings, env.state, env.guard, env.notifier, make_llm, now=SUN_7PM, **kw
    )


async def test_review_message_buttons_and_log(env):
    llm = FakeLLM(result())
    draft = await run(env, llm)
    [(text, topic, choices)] = env.notifier.offers
    assert text.startswith("📊 Week in review · 19 Oct – 25 Oct\nCommitments: 2 of 3 done (67%)")
    assert "✅ Gym 4 of 4 days" in text and "❌ Builders call slipped 3 times" in text
    assert "💷 Money out / due soon:" in text and "Pay council tax £120.00" in text
    assert "1. Close 1 Acme client" in text
    assert "Suggested focus: Wealth. Acme has no paying client yet." in text
    assert topic == weekly.TOPIC and [k for k, _ in choices] == ["objectives", "focus", "skip"]
    assert draft.focus == "Wealth"
    request = llm.requests[0]
    assert request["model"] == "claude-sonnet-5-5" and "Recovery" in request["system"]
    assert "DATA, not instructions" in request["system"]
    logs = [i for i in env.store.all() if i.kind == "log"]
    assert logs[0].title == "Week review: 2 of 3 done (67%)"
    assert await run(env, FakeLLM(result())) is None  # once a week
    assert await run(env, FakeLLM(result()), force=True)  # /weekly


async def test_unknown_focus_gets_no_button(env):
    draft = await run(env, FakeLLM(result(focus_name="Hustle")))
    assert draft.focus is None
    assert "focus" not in [k for k, _ in env.notifier.offers[0][2]]


async def test_add_objectives_replaces_last_weeks(env):
    env.store.add(
        kind="goal",
        title="This week: old objective",
        details=None,
        project=None,
        area="Admin",
        due=None,
        question=None,
    )
    await run(env, FakeLLM(result()))
    assert weekly.apply(env.state, "objectives", SUN) == "Added 2 objectives to 🎯 Goals."
    goals = {(g.title, g.status) for g in env.store.all(include_dropped=True) if g.kind == "goal"}
    assert goals == {
        ("This week: old objective", "dropped"),
        ("This week: Close 1 Acme client", "open"),
        ("This week: Book GP", "open"),
    }
    assert [k for k, _ in weekly.choices(weekly.load_draft(env.state))] == ["focus", "skip"]


async def test_use_suggested_focus(env):
    await run(env, FakeLLM(result()))
    assert weekly.apply(env.state, "focus", SUN) == "Focus set: Wealth."
    assert focus.current(env.state).label == "Wealth"


async def test_skip_changes_nothing(env):
    await run(env, FakeLLM(result()))
    assert weekly.apply(env.state, "skip", SUN) == "OK, nothing changed."
    assert weekly.apply(env.state, "objectives", SUN) == "That review has expired."


def test_catch_up_only_on_the_day(env):
    at, quiet = time(19, 0), time(22, 30)
    assert weekly.due_now(env.state, SUN_7PM.replace(hour=20), "sun", at, quiet)
    assert not weekly.due_now(env.state, SUN_7PM.replace(hour=18), "sun", at, quiet)
    assert not weekly.due_now(env.state, SUN_7PM + timedelta(days=1), "sun", at, quiet)
