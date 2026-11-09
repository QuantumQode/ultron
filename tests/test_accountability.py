from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ultron.features import accountability as acc
from ultron.features import review
from ultron.features.items import ItemStore
from ultron.features.schedule import PlanStore
from ultron.notify import Space
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
WED = date(2026, 10, 21)


class FakeNotifier:
    def __init__(self):
        self.offers, self.asks = [], []

    async def send(self, text, space=Space.BRIEFS):
        pass

    async def ask(self, text, topic, space=Space.BRIEFS, placeholder=None):
        self.asks.append(text)

    async def offer(self, text, topic, choices, space=Space.BRIEFS):
        self.offers.append((text, topic, choices))


@pytest.fixture
def env():
    state = State(":memory:")
    yield SimpleNamespace(state=state, store=ItemStore(state), notifier=FakeNotifier())
    state.close()


def add(store, kind, title, **kw):
    values = dict(details=None, project=None, area="Admin", due=None, question=None)
    values.update(kw)
    return store.add(kind=kind, title=title, **values)


def age(state, item, days):
    stamp = (datetime(2026, 10, 21, 9, 0) - timedelta(days=days)).isoformat() + "+00:00"
    state.db.execute(
        "UPDATE items SET created_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, item.id)
    )


def test_slipping_todo_and_neglected_goal_are_called_out(env):
    slip = add(env.store, "task", "Call the accountant")
    env.store.update(slip.id, times_moved=4)
    goal = add(env.store, "goal", "Move to Portugal")
    age(env.state, goal, 10)
    add(env.store, "task", "Fine task")
    out = acc.callouts(env.state, WED)
    assert [c.item.title for c in out] == ["Call the accountant", "Move to Portugal"]
    assert out[0].text.startswith('You\'ve pushed back "Call the accountant" 4 times.')


def test_left_alone_recent_paused_questioned_and_objectives(env):
    fresh = add(env.store, "goal", "Learn piano")  # created today
    paused = add(env.store, "goal", "12-hour work days (paused until 3 Nov)")
    week = add(env.store, "goal", "This week: Book GP")
    asked = add(env.store, "task", "Old email thing", question="Still needed?")
    env.store.update(asked.id, times_moved=5)
    for g in (paused, week):
        age(env.state, g, 10)
    assert acc.callouts(env.state, WED) == [] and fresh


def test_a_done_step_counts_as_touching_the_goal(env):
    goal = add(env.store, "goal", "Move to Portugal")
    age(env.state, goal, 10)
    b = PlanStore(env.state).add(WED - timedelta(days=2), "Research visas", item_id=goal.id)
    PlanStore(env.state).set_status(b.id, "done")
    assert acc.callouts(env.state, WED) == []


async def test_cooldown_and_limit(env):
    for n in range(3):
        t = add(env.store, "task", f"Slip {n}")
        env.store.update(t.id, times_moved=3 + n)
    assert await acc.send_callouts(env.state, env.notifier, WED) == 2  # at most 2 an evening
    assert [o[0] for o in env.notifier.offers][0].startswith('⚠️ You\'ve pushed back "Slip 2"')
    later = acc.callouts(env.state, WED + timedelta(days=1))
    assert [c.item.title for c in later] == ["Slip 0"]  # the other two are cooling down
    assert len(acc.callouts(env.state, WED + timedelta(days=3))) == 2


def test_answers(env):
    t = add(env.store, "task", "Call the accountant", due="2026-10-15")
    assert acc.answer_callout(env.state, f"{t.id}:tomorrow", WED) == (
        "Tomorrow, first thing: Call the accountant"
    )
    [block] = PlanStore(env.state).for_day(WED + timedelta(days=1))
    assert block.title == "First thing: Call the accountant" and block.item_id == t.id
    assert env.store.get(t.id).due == "2026-10-22"
    g = add(env.store, "goal", "Move to Portugal")
    assert acc.answer_callout(env.state, f"{g.id}:drop", WED) == "Dropped: Move to Portugal"
    assert env.store.get(g.id).status == "dropped"
    assert acc.answer_callout(env.state, f"{g.id}:keep", WED) == "Already sorted."


def test_easy_day_silences_callouts_and_checkin(env):
    t = add(env.store, "task", "Slip")
    env.store.update(t.id, times_moved=5)
    acc.easy(env.state, WED)
    assert acc.callouts(env.state, WED) == []
    assert acc.callouts(env.state, WED + timedelta(days=1))  # only for that day


async def test_midday_checkin(env):
    plans = PlanStore(env.state)
    gym = plans.add(WED, "Workout", start="07:00", end="08:30")
    plans.set_status(gym.id, "done")
    plans.add(WED, "Admin", start="10:00", end="10:30")
    plans.add(WED, "Call builders", start="15:00", end="15:45")
    now = datetime(2026, 10, 21, 13, 0, tzinfo=LONDON)
    assert await acc.send_checkin(env.state, env.notifier, now)
    [(text, topic, choices)] = env.notifier.offers
    assert text.splitlines() == [
        "🕐 How's the day going?",
        "Done: Workout",
        "Not ticked yet: Admin",
        "Still to come: 15:00–15:45 Call builders (~45m)",
    ]
    assert topic == acc.CHECKIN_TOPIC and [k for k, _ in choices] == ["ok", "replan"]
    assert not await acc.send_checkin(env.state, env.notifier, now)  # once a day


async def test_checkin_with_no_plan_offers_to_plan(env):
    now = datetime(2026, 10, 21, 13, 0, tzinfo=LONDON)
    text, choices = acc.checkin_text(env.state, now)
    assert "No plan for today." in text and choices[0] == ("replan", "🔁 Plan the rest of today")


async def test_evening_review_includes_callouts_before_the_reflection(env):
    t = add(env.store, "task", "Call the accountant")
    env.store.update(t.id, times_moved=3)
    await review.start(env.state, env.notifier, datetime(2026, 10, 21, 21, 0, tzinfo=LONDON))
    assert any("pushed back" in o[0] for o in env.notifier.offers)
    assert env.notifier.asks[-1].startswith("One line on today?")


def test_weekly_patterns(env):
    t = add(env.store, "task", "Call the accountant")
    env.store.update(t.id, times_moved=3)
    assert acc.weekly_lines(env.state, WED) == ["Call the accountant: pushed back 3×"]
