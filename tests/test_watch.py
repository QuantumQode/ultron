from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ultron.features import watch
from ultron.features.items import ItemStore
from ultron.features.schedule import PlanStore, build_view, render, title_from
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
SUN_3PM = datetime(2026, 10, 25, 15, 0, tzinfo=LONDON)
MON = date(2026, 10, 26)
PLAN_AT = time(21, 0)


class FakeNotifier:
    def __init__(self):
        self.offers, self.asks = [], []

    async def send(self, text, space=None):
        pass

    async def offer(self, text, topic, choices, space=None):
        self.offers.append(SimpleNamespace(text=text, topic=topic, choices=choices))

    async def ask(self, text, topic, space=None, placeholder=None):
        self.asks.append(SimpleNamespace(text=text, topic=topic))


@pytest.fixture
def env():
    state = State(":memory:")
    store = ItemStore(state)

    def add(kind, title, **kw):
        values = dict(details=None, project=None, area="Admin", due=None, question=None)
        values.update(kw)
        return store.add(kind=kind, title=title, **values)

    add(
        "schedule",
        "Sleep",
        days="mon,tue,wed,thu,fri,sat,sun",
        start_time="22:00",
        end_time="07:00",
    )
    workout = add("schedule", "Workout", days="mon,tue,fri,sat", details="Morning, about 2 hours")
    plans = PlanStore(state)
    gym = plans.add(
        MON, "Workout: jog then weights", start="07:45", end="09:25", item_id=workout.id
    )
    later = plans.add(MON, "Deep work", start="11:00", end="12:30")
    yield SimpleNamespace(state=state, store=store, add=add, plans=plans, gym=gym, later=later)
    state.close()


def paddle(env, **kw):
    fields = dict(due=MON.isoformat(), start_time="07:00", end_time="08:30", area="Health")
    fields.update(kw)
    return env.add("schedule", "Paddle", **fields)


def test_an_appointment_added_after_approval_is_a_clash(env):
    paddle(env)
    found = watch.find_clashes(env.state, SUN_3PM)
    assert [(c.block.title, c.hits) for c in found] == [("Workout: jog then weights", "Paddle")]


def test_the_schedule_marks_clashing_blocks_and_reads_them_back(env):
    paddle(env)
    view = build_view(env.state, SUN_3PM.date())
    assert view.tomorrow.clashes == {env.gym.id: "Paddle"}
    lines = [line.sig for line in render(view)["tomorrow"][1]]
    marked = next(s for s in lines if "Workout" in s)
    assert marked.endswith("· ⚠️ clashes with Paddle")
    assert title_from(marked) == "Workout: jog then weights"


async def test_clashes_are_told_once_per_day_with_replan_buttons(env):
    paddle(env)
    notifier = FakeNotifier()
    assert await watch.check(env.state, notifier, SUN_3PM, PLAN_AT) == 1
    offer = notifier.offers[0]
    assert "Mon 26 Oct" in offer.text and "Paddle" in offer.text
    assert "Tonight's 21:00 planning redoes Mon anyway" in offer.text
    assert offer.choices == [
        ("replan:2026-10-26", "🔁 Replan Mon"),
        ("keep:2026-10-26", "Keep as is"),
    ]
    assert await watch.check(env.state, notifier, SUN_3PM, PLAN_AT) == 0  # told already


async def test_a_new_clash_on_the_same_day_is_told_again_with_the_whole_list(env):
    paddle(env)
    notifier = FakeNotifier()
    await watch.check(env.state, notifier, SUN_3PM, PLAN_AT)
    env.add(
        "schedule",
        "Take Robin to the airport",
        due=MON.isoformat(),
        start_time="10:30",
        end_time="12:20",
    )
    await watch.check(env.state, notifier, SUN_3PM, PLAN_AT)
    assert len(notifier.offers) == 2
    assert "Paddle" in notifier.offers[1].text and "the airport" in notifier.offers[1].text


async def test_back_to_back_appointments_get_a_question(env):
    paddle(env)
    env.add("schedule", "Call GP", due=MON.isoformat(), start_time="08:30", end_time="08:45")
    notifier = FakeNotifier()
    await watch.check(env.state, notifier, SUN_3PM, PLAN_AT)
    [ask] = notifier.asks
    assert ask.topic == watch.NOTE_TOPIC
    assert "Paddle ends 08:30 and 🔒 Call GP starts 08:30" in ask.text


async def test_an_appointment_with_no_end_is_asked_how_long(env):
    item = paddle(env, end_time=None, question=watch.LENGTH_QUESTION)
    notifier = FakeNotifier()
    await watch.check(env.state, notifier, SUN_3PM, PLAN_AT)
    question = next(o for o in notifier.offers if o.text.startswith("⏱"))
    assert "How long is Paddle (Mon 26 Oct, from 07:00)" in question.text
    keys = [k for row in question.choices for k, _ in row]
    assert keys == [f"len:{item.id}:{m}" for m in (30, 60, 90, 120, 180)] + [f"len:{item.id}:other"]
    assert watch.length_reply(env.state, item.id, "90") == "Got it: Paddle 07:00–08:30."
    updated = env.store.get(item.id)
    assert (updated.end_time, updated.question) == ("08:30", None)


def test_a_typed_length_is_read(env):
    item = env.add("schedule", "Take Robin to the airport", due=MON.isoformat(), start_time="10:30")
    assert watch.length_reply(env.state, item.id, "about 1h 50m all in") == (
        "Got it: Take Robin to the airport 10:30–12:20."
    )
    assert "couldn't read" in watch.length_reply(env.state, item.id, "a while")


def test_done_and_past_blocks_are_not_clashes(env):
    paddle(env)
    env.plans.set_status(env.gym.id, "done")
    assert watch.find_clashes(env.state, SUN_3PM) == []
