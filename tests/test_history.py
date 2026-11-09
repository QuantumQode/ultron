from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from ultron.features import history
from ultron.features.items import ItemStore
from ultron.features.schedule import PlanStore
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 25, 15, 0, tzinfo=LONDON)


@pytest.fixture
def store():
    state = State(":memory:")
    yield ItemStore(state)
    state.close()


def add(store, kind, title, **kw):
    values = dict(details=None, project=None, area="Admin", due=None, question=None)
    values.update(kw)
    return store.add(kind=kind, title=title, **values)


def state_of(store):
    return type("S", (), {"db": store.db})()


def test_track_record_counts_outcomes_by_time_length_and_item(store):
    spanish = add(store, "habit", "Learn Spanish 30mins every day")
    plans = PlanStore.sharing(store)
    for day, status, start in [
        (20, "done", "08:00"),
        (21, "partial", "19:00"),
        (22, "done", "19:00"),
    ]:
        b = plans.add(
            date(2026, 10, day), "Spanish", start=start, end=start[:3] + "30", item_id=spanish.id
        )
        plans.set_status(b.id, status)
    plans.add(date(2026, 10, 23), "Never ticked", start="10:00", end="11:30")
    lines = history.track_record(state_of(store), NOW.date())
    assert lines[0] == "Last 21 days: 4 timed blocks: 2 done, 1 partly, 0 missed, 1 never ticked."
    assert "mornings 50% done (2)" in lines[1] and "evenings 50% done (2)" in lines[1]
    assert "≤30 min 67% done, 1 partly (3)" in lines[2]
    assert (
        lines[3] == f"[#{spanish.id}] Learn Spanish 30mins every day: 3 blocks of ~30m: "
        "2 done, 1 partly, 0 missed"
    )
    assert history.track_record(state_of(store), date(2026, 10, 1)) == []


def test_week_capacity_counts_what_is_spoken_for(store):
    add(
        store,
        "schedule",
        "Sleep",
        days="mon,tue,wed,thu,fri,sat,sun",
        start_time="22:00",
        end_time="07:00",
    )
    add(store, "schedule", "Workout", days="mon,fri", details="Morning, about 2 hours total")
    add(store, "habit", "Learn Spanish 30mins every day")
    add(store, "habit", "Morning routine · water")  # inside the routine: not counted
    add(store, "task", "Call GP", due="2026-10-27")
    cap = history.week_capacity(state_of(store), NOW, 0.7)
    assert (cap.workouts, cap.habits, cap.due) == (240, 7 * 30, 1)
    assert cap.plannable > cap.left > 0
    assert "Left for to-dos" in cap.text() and "OVERLOADED" not in cap.text()
