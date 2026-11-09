from datetime import date

import pytest
from conftest import CONFIGURED

from ultron.features.freetime import Planned, check_plan, clock_in, shape_of, to_hhmm, to_min
from ultron.features.items import ItemStore
from ultron.state import State

WED, THU, SUN = date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 25)


@pytest.fixture
def store():
    s = State(":memory:")
    yield ItemStore(s)
    s.close()


def entry(store, title, days=None, start=None, end=None, details=None, travel=None, due=None):
    return store.add(
        kind="schedule",
        title=title,
        details=details,
        project=None,
        area="Admin",
        due=due,
        question=None,
        days=days,
        start_time=start,
        end_time=end,
        travel_minutes=travel,
    )


@pytest.fixture
def owner(store):
    entry(store, "Work", "thu,sun", "07:00", "18:00", travel=30)
    entry(store, "Workout", "mon,tue,fri,sat", details="Morning, about 2 hours total")
    entry(store, "Sleep", "mon,tue,wed,thu,fri,sat,sun", "22:00", "07:00")
    entry(store, "Workout", "thu", details="After work, from about 18:30")
    return store.all()


def slots(shape):
    return [str(s) for s in shape.free]


def test_free_day_runs_from_waking_to_wind_down(owner):
    shape = shape_of(WED, owner, settings=CONFIGURED)
    assert shape.wake == to_min("07:00") and shape.bedtime == to_min("22:00")
    assert slots(shape) == ["07:00–21:30"]
    assert shape.free_minutes == 870 and shape.budget == 609  # 70%


def test_work_day_blocks_the_commute_and_wakes_earlier(owner):
    shape = shape_of(THU, owner, settings=CONFIGURED)
    assert shape.wake == to_min("06:00")
    [work] = shape.fixed
    assert (work.start, work.end, work.note) == (to_min("06:30"), to_min("18:30"), "incl. commute")
    assert slots(shape) == ["18:30–21:30"]  # getting ready before leaving isn't free time


def test_flexible_entries_are_left_for_the_planner(owner):
    [workout] = shape_of(THU, owner, settings=CONFIGURED).flexible
    assert workout.start is None and workout.preferred == to_min("18:30")
    [morning] = shape_of(date(2026, 10, 23), owner, settings=CONFIGURED).flexible
    assert morning.note == "Morning, about 2 hours total" and morning.preferred is None
    assert slots(shape_of(date(2026, 10, 23), owner, settings=CONFIGURED)) == [
        "07:00–21:30"
    ]  # not blocked out


def test_a_midday_commitment_splits_free_time(store):
    entry(store, "Sleep", None, "23:00", "07:30")
    entry(store, "Dentist", "wed", "13:00", "14:00", travel=15)
    shape = shape_of(WED, store.all(), settings=CONFIGURED)
    assert slots(shape) == ["07:30–12:45", "14:15–22:30"]


def test_no_sleep_entry_uses_defaults(store):
    assert slots(shape_of(WED, store.all(), settings=CONFIGURED)) == ["07:00–22:30"]


def test_today_drops_time_already_past(owner):
    shape = shape_of(WED, owner, not_before=to_min("15:20"), settings=CONFIGURED)
    assert slots(shape) == ["15:20–21:30"]
    assert shape_of(WED, owner, not_before=to_min("21:40"), settings=CONFIGURED).free == []


def test_tiny_gaps_are_ignored(store):
    entry(store, "A", "wed", "09:00", "10:00")
    entry(store, "B", "wed", "10:10", "12:00")
    assert "10:00–10:10" not in slots(shape_of(WED, store.all(), settings=CONFIGURED))


# --- the over-planning guard ---


def block(title, start, end):
    return Planned(title, to_min(start), to_min(end))


def test_a_plan_that_fits(owner):
    shape = shape_of(WED, owner, settings=CONFIGURED)
    plan = [block("Calls", "09:00", "11:00"), block("Admin", "11:10", "12:00")]
    assert check_plan(shape, plan) == []


def test_blocks_must_be_in_free_time(owner):
    shape = shape_of(THU, owner, settings=CONFIGURED)
    [problem] = check_plan(shape, [block("Calls", "12:00", "13:00")])
    assert "isn't inside free time" in problem
    [problem] = check_plan(shape, [block("Late", "21:00", "21:45")])  # runs into wind-down
    assert "isn't inside free time" in problem


def test_blocks_need_a_gap(owner):
    shape = shape_of(WED, owner, settings=CONFIGURED)
    plan = [block("A", "09:00", "10:00"), block("B", "10:05", "11:00")]
    assert check_plan(shape, plan) == ["'A' and 'B' need 10 minutes between them"]


def test_no_more_than_70_percent(owner):
    shape = shape_of(THU, owner, settings=CONFIGURED)  # 180 free -> 126 allowed
    [problem] = check_plan(shape, [block("Workout", "18:30", "20:45")])
    assert "135 minutes planned, but at most 126 fit" in problem
    assert check_plan(shape, [block("Short", "18:30", "19:00")], budget=20)  # custom cap


def test_backwards_block_is_caught(owner):
    shape = shape_of(WED, owner, settings=CONFIGURED)
    assert check_plan(shape, [block("Oops", "10:00", "09:00")])[0].endswith("before it starts")


def test_leaving_exactly_30_minutes_after_waking_leaves_no_free_morning(store):
    entry(store, "Work", "thu", "07:00", "18:00", travel=30)
    entry(store, "Sleep", "mon,tue,wed,thu,fri,sat,sun", "22:00", "06:00")
    shape = shape_of(THU, store.all(), settings=CONFIGURED)
    assert shape.wake == to_min("06:00") and slots(shape) == ["18:30–21:30"]


@pytest.mark.parametrize(
    ("text", "time"),
    [
        ("Meeting at Leeds uncle, 9:45am", "09:45"),
        ("Court booked for 12pm Friday", "12:00"),
        ("9.45 am", "09:45"),
        ("drinks 9:45 pm", "21:45"),
        ("lunch at noon", "12:00"),
        ("from about 18:30", "18:30"),
        ("Call GP at 8:30am", "08:30"),
    ],
)
def test_clock_in_reads_times(text, time):
    assert to_hhmm(clock_in(text)) == time


@pytest.mark.parametrize(
    "text", ["about 2 hours", "ask for the £1.5k", "Room 101", "I am ok", None]
)
def test_clock_in_ignores_numbers_that_are_not_times(text):
    assert clock_in(text) is None


FRI = date(2026, 10, 23)


def test_a_one_off_applies_only_on_its_date_and_blocks_its_time(owner, store):
    entry(store, "Meeting at Leeds uncle, 9:45am", due="2026-10-23", travel=15)
    entry(store, "Dentist", due="2026-10-23", start="15:00", end="15:30")
    shape = shape_of(FRI, store.all(), settings=CONFIGURED)
    assert [(c.title, to_hhmm(c.start), to_hhmm(c.end), c.note, c.dated) for c in shape.fixed] == [
        (
            "Meeting at Leeds uncle, 9:45am",
            "09:30",
            "11:00",
            "incl. travel · length guessed",
            True,
        ),
        ("Dentist", "15:00", "15:30", None, True),
    ]
    assert slots(shape) == ["07:00–09:30", "11:00–15:00", "15:30–21:30"]
    assert check_plan(shape, [Planned("Deep work", to_min("09:00"), to_min("10:00"))])
    assert (
        shape_of(THU, store.all(), settings=CONFIGURED).fixed[-1].title == "Work"
    )  # not on other days
    assert all(c.title != "Dentist" for c in shape_of(WED, store.all(), settings=CONFIGURED).fixed)


def test_a_weekly_entry_ignores_a_stray_due_date(store):
    entry(store, "Gym", "fri", details="Friday plan", due="2026-10-23")
    assert [
        c.title for c in shape_of(date(2026, 10, 30), store.all(), settings=CONFIGURED).flexible
    ] == ["Gym"]


def test_an_entry_with_a_start_but_no_end_blocks_a_guessed_hour(store):
    entry(store, "Class", "wed", start="19:00")
    [c] = shape_of(WED, store.all(), settings=CONFIGURED).fixed
    assert (to_hhmm(c.start), to_hhmm(c.end), c.note) == ("19:00", "20:00", "length guessed")


# --- one wake time, the morning routine, sport (2026-10-25) ---


def routine(store, days="mon,tue,wed,fri,sat", minutes=35):
    item = entry(store, "Morning routine", days)
    return store.update(item.id, anchor="wake", minutes=minutes)


MON = date(2026, 10, 26)


def test_the_morning_routine_starts_on_waking_and_never_moves_the_wake_earlier(store, owner):
    routine(store)
    shape = shape_of(MON, store.all(), settings=CONFIGURED)
    assert to_hhmm(shape.wake) == "07:00"  # was 05:30: the routine read as an early start
    first = shape.fixed[0]
    assert (first.title, to_hhmm(first.start), to_hhmm(first.end)) == (
        "Morning routine",
        "07:00",
        "07:35",
    )
    assert first.routine and first.note == "on waking, 35 min"
    assert to_hhmm(shape.free[0].start) == "07:35"


def test_the_routine_follows_the_sleep_entry(store, owner):
    routine(store)
    sleep = next(i for i in store.all() if i.title == "Sleep")
    store.update(sleep.id, end_time="07:30")
    shape = shape_of(MON, store.all(), settings=CONFIGURED)
    assert (to_hhmm(shape.wake), to_hhmm(shape.fixed[0].start)) == ("07:30", "07:30")


def test_a_7am_booking_wakes_the_owner_at_0630_with_a_short_routine(store, owner):
    routine(store)
    entry(store, "Paddle", start="07:00", end="08:30", due=MON.isoformat())
    shape = shape_of(MON, store.all(), settings=CONFIGURED)
    assert to_hhmm(shape.wake) == "06:30"
    morning, paddle = shape.fixed[:2]
    assert (to_hhmm(morning.start), to_hhmm(morning.end)) == ("06:30", "07:00")
    assert morning.note.startswith("short today (30 min)")
    assert paddle.sport and not morning.sport
    assert to_hhmm(shape.free[0].start) == "08:30"


def test_work_days_still_wake_at_six_for_the_commute(store, owner):
    assert to_hhmm(shape_of(THU, store.all(), settings=CONFIGURED).wake) == "06:00"


def test_sport_is_spotted_but_the_gym_is_not():
    from ultron.features.freetime import is_sport

    assert is_sport("Paddle") and is_sport("Court booked for 12pm") and is_sport("Yoga class")
    assert not is_sport("Workout") and not is_sport("Gym") and not is_sport("Call GP")


def test_lengths_are_read_from_text():
    from ultron.features.freetime import minutes_in

    assert minutes_in("about 2 hours total") == 120
    assert minutes_in("1h 50m") == 110
    assert minutes_in("110") == 110
    assert minutes_in("Learn Spanish 30mins every day") == 30
    assert minutes_in("£1.5k") is None


def test_hits_names_what_a_block_runs_into(store, owner):
    from ultron.features.freetime import hits

    entry(store, "Paddle", start="07:00", end="08:30", due=MON.isoformat())
    shape = shape_of(MON, store.all(), settings=CONFIGURED)
    assert hits(shape, to_min("06:45"), to_min("08:25")) == ["Paddle"]
    assert hits(shape, to_min("06:00"), to_min("06:20")) == ["before you're up"]
    assert hits(shape, to_min("06:35"), to_min("06:55")) == ["time to get ready"]
    assert hits(shape, to_min("09:00"), to_min("10:00")) == []
    assert hits(shape, to_min("21:00"), to_min("21:45")) == ["wind-down"]
