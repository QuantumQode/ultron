from datetime import date, timedelta

import pytest

from ultron.features import habits
from ultron.features.items import ItemStore
from ultron.state import State

WED = date(2026, 10, 21)


@pytest.fixture
def store():
    s = State(":memory:")
    yield ItemStore(s)
    s.close()


def habit(store, title="Learn Spanish", days=None):
    return store.add(
        kind="habit",
        title=title,
        details=None,
        project=None,
        area="Learning",
        due=None,
        question=None,
        days=days,
    )


def test_streak_counts_days_in_a_row(store):
    h = habit(store)
    for n in range(3):
        habits.keep(store, h.id, WED - timedelta(days=n))
    assert store.get(h.id).streak == 3 and store.get(h.id).kept_on == WED.isoformat()


def test_streak_survives_until_today_is_over(store):
    h = habit(store)
    habits.keep(store, h.id, WED - timedelta(days=1))
    habits.keep(store, h.id, WED - timedelta(days=2))
    habits.refresh(store, WED)  # not ticked yet today
    assert store.get(h.id).streak == 2


def test_a_missed_day_breaks_the_streak(store):
    h = habit(store)
    habits.keep(store, h.id, WED - timedelta(days=3))
    habits.keep(store, h.id, WED - timedelta(days=2))
    habits.refresh(store, WED)  # yesterday was missed
    assert store.get(h.id).streak == 0


def test_days_off_dont_break_a_weekly_habit(store):
    h = habit(store, "Long run", days="sat")
    habits.keep(store, h.id, date(2026, 10, 17))
    habits.keep(store, h.id, date(2026, 10, 10))
    habits.refresh(store, WED)
    assert store.get(h.id).streak == 2
    assert not habits.due_on(store.get(h.id), WED)


def test_today_status_for_the_review(store):
    a, b = habit(store, "a"), habit(store, "b")
    habit(store, "c", days="sat")
    habits.keep(store, a.id, WED)
    kept, missing = habits.today_status(store, WED)
    assert [i.id for i in kept] == [a.id] and [i.id for i in missing] == [b.id]


def test_unkeep_and_double_keep(store):
    h = habit(store)
    habits.keep(store, h.id, WED)
    habits.keep(store, h.id, WED)  # ticking twice is harmless
    habits.unkeep(store, h.id, WED)
    assert store.get(h.id).kept_on is None and store.get(h.id).streak == 0
