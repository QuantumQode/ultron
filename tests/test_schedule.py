from datetime import date

import pytest
from conftest import CONFIGURED, configure
from fake_notion import FakeNotion

from ultron.features.freetime import shape_of
from ultron.features.items import ItemStore, close_past_appointments
from ultron.features.schedule import (
    DayView,
    PlanStore,
    build_view,
    day_lines,
    fixed_for,
    fmt_minutes,
    rest_lines,
    write_schedule,
)
from ultron.integrations.notion import SCHEDULE_HINT, callout
from ultron.state import State

WED = date(2026, 10, 21)
THU = date(2026, 10, 22)


@pytest.fixture
def state():
    s = State(":memory:")
    configure(s)  # a wind-down note and a 35-min routine
    yield s
    s.close()


def schedule(store, title, days=None, start=None, end=None, details=None, travel=None, due=None):
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
def owner(state):
    """The owner's real fixed schedule."""
    store = ItemStore(state)
    schedule(store, "Work", "thu,sun", "07:00", "18:00", "Commute about 30 minutes.", travel=30)
    schedule(store, "Workout", "mon,tue,fri,sat", details="Morning, about 2 hours total: weights")
    schedule(store, "Sleep", "mon,tue,wed,thu,fri,sat,sun", "22:00", "07:00")
    schedule(store, "Rest day", "sun", details="Sunday is a rest day from workouts.")
    schedule(store, "Workout", "thu", details="After work, from about 18:30 (work ends 18:00)")
    return store


def texts(lines):
    return [line.sig for line in lines]


def view_of(store, day, blocks=()):
    shape = shape_of(day, store.all(), settings=CONFIGURED)
    return DayView(day, fixed_for(shape), list(blocks), shape.free_minutes)


def test_a_free_day(owner):
    assert texts(day_lines(view_of(owner, WED))) == [
        "Free 14h 30m",
        "Nothing planned yet.",
        "🔒 07:00 Wake",
        "🔒 21:30 Wind down · journal or meditate",
        "🔒 22:00 Sleep",
    ]


def test_a_work_day_includes_the_commute_and_an_earlier_wake(owner):
    assert texts(day_lines(view_of(owner, THU))) == [
        "Free 3h",
        "Nothing planned yet.",
        "🔒 06:00 Wake",
        "🔒 06:30–18:30 Work · incl. commute",
        "🔒 ~18:30 Workout · After work, from about 18:30",
        "🔒 21:30 Wind down · journal or meditate",
        "🔒 22:00 Sleep",
    ]


def test_planned_blocks_sit_between_fixed_lines(state, owner):
    plans = PlanStore(state)
    plans.add(WED, "Acme: call builders", start="09:00", end="11:00", item_id=1)
    plans.add(WED, "Pay Amex minimum", start="11:10", end="11:40", minutes=30)
    done = plans.add(WED, "Reply to Dana", start="14:00", end="15:00")
    plans.set_status(done.id, "done")
    lines = day_lines(view_of(owner, WED, plans.for_day(WED)))
    assert texts(lines) == [
        "Free 14h 30m · planned 3h 30m (24%)",
        "🔒 07:00 Wake",
        "09:00–11:00 Acme: call builders (~2h)",
        "11:10–11:40 Pay Amex minimum (~30m)",
        "14:00–15:00 Reply to Dana (~1h) ✓",
        "🔒 21:30 Wind down · journal or meditate",
        "🔒 22:00 Sleep",
    ]
    assert lines[2].block["type"] == "to_do" and not lines[2].block["to_do"]["checked"]
    assert lines[4].block["to_do"]["checked"]
    assert lines[1].block["type"] == "paragraph"


FRI = date(2026, 10, 23)


def friday(state, owner):
    """The owner's real Friday: a gym morning plus two one-off appointments jotted in the
    Inbox with their times only in the title."""
    schedule(owner, "Meeting at Leeds uncle, 9:45am", due="2026-10-23", travel=15)
    schedule(owner, "Court booked for 12pm Friday, after meeting", due="2026-10-23")
    return owner


def test_one_offs_sit_at_their_time_and_untimed_lines_go_before_wind_down(state, owner):
    friday(state, owner)
    plans = PlanStore(state)
    plans.add(FRI, "Spend 30 mins in the guitar course")  # untimed
    plans.add(FRI, "Send 3 job applications", start="14:00", end="15:00")
    lines = day_lines(view_of(owner, FRI, plans.for_day(FRI)))
    assert texts(lines) == [
        "Free 12h · planned 1h (8%)",  # 14h 30m less the two appointments
        "🔒 07:00 Wake",
        "🔒 09:30–11:00 Meeting at Leeds uncle, 9:45am · incl. travel · length guessed",
        "🔒 12:00–13:00 Court booked for 12pm Friday, after meeting · length guessed",
        "14:00–15:00 Send 3 job applications (~1h)",
        "Not timed yet:",
        "🔒 Workout · Morning, about 2 hours total",
        "Spend 30 mins in the guitar course",
        "🔒 21:30 Wind down · journal or meditate",
        "🔒 22:00 Sleep",
    ]
    assert lines[-3].block["type"] == "to_do"  # still tickable inside the group


def test_one_offs_only_show_on_their_own_day(state, owner):
    friday(state, owner)
    assert not any("Leeds" in t for t in texts(day_lines(view_of(owner, THU))))
    days = [
        DayView(d, fixed_for(shape_of(d, owner.all(), settings=CONFIGURED)), [])
        for d in (FRI, date(2026, 10, 24))
    ]
    assert texts(rest_lines(days)) == [
        "Fri 23 · 🔒 09:30 Meeting at Leeds uncle, 9:45am"
        " · 🔒 12:00 Court booked for 12pm Friday, after meeting · 🔒 Workout",
        "Sat 24 · 🔒 Workout",
    ]


def test_past_appointments_tick_themselves_off_with_a_log_line(state, owner):
    friday(state, owner)
    closed = close_past_appointments(owner, date(2026, 10, 24))
    assert [i.title for i in closed] == [
        "Meeting at Leeds uncle, 9:45am",
        "Court booked for 12pm Friday, after meeting",
    ]
    assert all(owner.get(i.id).status == "done" for i in closed)
    assert owner.get(closed[0].id).done_at.startswith("2026-10-23")  # not shown ticked today
    logs = [i.title for i in owner.all() if i.kind == "log"]
    assert "Appointment passed: Meeting at Leeds uncle, 9:45am (Fri 23 Oct)" in logs
    assert close_past_appointments(owner, date(2026, 10, 23)) == []  # not before the day ends
    workout = [i for i in owner.all() if i.title == "Workout"]
    assert all(i.status == "open" for i in workout)  # weekly entries are never closed


def test_first_thing_blocks_follow_waking(state, owner):
    plans = PlanStore(state)
    plans.add(WED, "Pay Amex", start="09:00", end="09:30")
    plans.add(WED, "First thing: call the GP")
    assert texts(day_lines(view_of(owner, WED, plans.for_day(WED))))[1:4] == [
        "🔒 07:00 Wake",
        "First thing: call the GP",
        "09:00–09:30 Pay Amex (~30m)",
    ]


def test_a_flexible_entry_is_hidden_once_a_block_stands_for_it(state, owner):
    workout = next(i for i in owner.all() if i.title == "Workout" and i.days == "thu")
    PlanStore(state).add(THU, "Weights", start="18:30", end="19:30", item_id=workout.id)
    lines = texts(day_lines(build_view(state, WED).tomorrow))
    assert "18:30–19:30 Weights (~1h)" in lines
    assert not any("🔒 ~18:30 Workout" in t for t in lines)


def test_rest_of_week_is_one_line_per_day_without_times(state, owner):
    plans = PlanStore(state)
    fri = date(2026, 10, 23)
    plans.add(fri, "Oak Court leak follow-up")
    days = [
        DayView(d, fixed_for(shape_of(d, owner.all(), settings=CONFIGURED)), plans.for_day(d))
        for d in (fri, date(2026, 10, 25))
    ]
    assert texts(rest_lines(days)) == [
        "Fri 23 · 🔒 Workout · Oak Court leak follow-up",
        "Sun 25 · 🔒 Work · 🔒 Rest day",
    ]


def test_fmt_minutes():
    assert fmt_minutes(90) == "1h 30m" and fmt_minutes(60) == "1h" and fmt_minutes(5) == "5m"


def test_dropped_blocks_are_hidden(state):
    plans = PlanStore(state)
    b = plans.add(WED, "Gone", start="09:00", end="10:00")
    plans.set_status(b.id, "dropped")
    assert plans.for_day(WED) == []


# --- writing to Notion ---


@pytest.fixture
def page():
    fake = FakeNotion()
    page_id = fake.add_page(fake.root, "Schedule")
    hint = fake.add_block(page_id, callout(SCHEDULE_HINT, "📅"))
    return fake, page_id, hint


def write(state, page, today=WED):
    fake, page_id, hint = page
    return write_schedule(fake, state, page_id, hint, build_view(state, today))


def test_first_write_builds_the_page(state, owner, page):
    fake, page_id, hint = page
    PlanStore(state).add(WED, "Call builders", start="09:00", end="10:00")
    write(state, page)
    assert fake.texts(page_id) == [
        SCHEDULE_HINT,
        "Today · Wed 21 Oct",
        "Free 14h 30m · planned 1h (7%)",
        "🔒 07:00 Wake",
        "[ ] 09:00–10:00 Call builders (~1h)",
        "🔒 21:30 Wind down · journal or meditate",
        "🔒 22:00 Sleep",
        "Tomorrow · Thu 22 Oct",
        "Free 3h",
        "Nothing planned yet.",
        "🔒 06:00 Wake",
        "🔒 06:30–18:30 Work · incl. commute",
        "🔒 ~18:30 Workout · After work, from about 18:30",
        "🔒 21:30 Wind down · journal or meditate",
        "🔒 22:00 Sleep",
        "Rest of the week",
    ]
    rest = fake.child_blocks(page_id)[-1]
    assert rest["heading_2"]["is_toggleable"]
    assert fake.texts(rest["id"])[0] == "Fri 23 · 🔒 Workout"
    assert len(fake.texts(rest["id"])) == 5  # Fri..Tue


def test_second_write_with_nothing_new_touches_nothing(state, owner, page):
    fake = page[0]
    write(state, page)
    fake.calls.clear()
    result = write(state, page)
    assert not result.changed
    assert fake.calls == ["blocks.children.list"]


def test_ticking_in_the_store_updates_one_block_in_place(state, owner, page):
    fake, page_id, _ = page
    block = PlanStore(state).add(WED, "Call builders", start="09:00", end="10:00")
    write(state, page)
    PlanStore(state).set_status(block.id, "done")
    fake.calls.clear()
    result = write(state, page)
    assert result.updated == 1 and result.created == 0 and result.removed == 0
    assert "[x] 09:00–10:00 Call builders (~1h)" in fake.texts(page_id)


def test_new_block_rebuilds_only_its_section(state, owner, page):
    fake, page_id, _ = page
    write(state, page)
    tomorrow_heading = [b for b in fake.child_blocks(page_id) if "Tomorrow" in str(b)][0]["id"]
    PlanStore(state).add(WED, "New thing", start="12:00", end="12:30")
    write(state, page)
    texts_now = fake.texts(page_id)
    assert "[ ] 12:00–12:30 New thing (~30m)" in texts_now
    assert texts_now.index("[ ] 12:00–12:30 New thing (~30m)") < texts_now.index(
        "Tomorrow · Thu 22 Oct"
    )
    assert tomorrow_heading in [b["id"] for b in fake.child_blocks(page_id)]  # kept


def test_next_day_rolls_forward(state, owner, page):
    fake, page_id, _ = page
    write(state, page)
    write(state, page, today=THU)
    texts_now = fake.texts(page_id)
    assert "Today · Thu 22 Oct" in texts_now and "Tomorrow · Fri 23 Oct" in texts_now
    assert "Today · Wed 21 Oct" not in texts_now
    assert texts_now.count("Rest of the week") == 1


def test_deleted_heading_rebuilds_the_page(state, owner, page):
    fake, page_id, hint = page
    write(state, page)
    fake.trash(fake.child_blocks(page_id)[1]["id"])  # owner deletes "Today"
    write(state, page)
    texts_now = fake.texts(page_id)
    assert texts_now[0] == SCHEDULE_HINT and texts_now[1] == "Today · Wed 21 Oct"
    assert texts_now.count("Tomorrow · Thu 22 Oct") == 1
    assert texts_now.count("🔒 22:00 Sleep") == 2  # once today, once tomorrow


def test_deleted_line_rebuilds_its_section(state, owner, page):
    fake, page_id, _ = page
    write(state, page)
    wake = [b for b in fake.child_blocks(page_id) if "Wake" in str(b)][0]["id"]
    fake.trash(wake)
    PlanStore(state).add(WED, "x", start="09:00", end="09:30")  # forces a check of the section
    write(state, page)
    assert fake.texts(page_id)[3] == "🔒 07:00 Wake"


# --- two-way: the owner's edits on the page ---


def page_block(fake, page_id, text):
    return next(b for b in fake.child_blocks(page_id) if text in str(b))


def test_ticking_on_the_page_ticks_the_block_and_its_board_todo(state, owner, page):
    from ultron.features.schedule import apply_edits, read_edits

    fake, page_id, _ = page
    todo = owner.add(
        kind="task",
        title="Call builders",
        details=None,
        project=None,
        area="Wealth",
        due=None,
        question=None,
    )
    block = PlanStore(state).add(WED, "Call builders", start="09:00", end="10:00", item_id=todo.id)
    write(state, page)
    page_block(fake, page_id, "Call builders")["to_do"]["checked"] = True
    edits = read_edits(fake, state, page_id)
    assert edits.ticked == [block.id]
    apply_edits(state, edits)
    assert PlanStore(state).get(block.id).status == "done"
    assert owner.get(todo.id).status == "done"
    # unticking undoes both
    write(state, page)
    page_block(fake, page_id, "Call builders")["to_do"]["checked"] = False
    apply_edits(state, read_edits(fake, state, page_id))
    assert (
        PlanStore(state).get(block.id).status == "planned" and owner.get(todo.id).status == "open"
    )


def test_deleting_and_rewording_blocks_on_the_page(state, owner, page):
    from ultron.features.schedule import apply_edits, read_edits

    fake, page_id, _ = page
    gone = PlanStore(state).add(WED, "Admin", start="09:00", end="09:30")
    keep = PlanStore(state).add(WED, "Call builders", start="10:00", end="11:00")
    write(state, page)
    fake.trash(page_block(fake, page_id, "Admin")["id"])
    renamed = page_block(fake, page_id, "Call builders")
    renamed["to_do"]["rich_text"] = [{"plain_text": "10:00–11:00 Call 3 builders (~1h)"}]
    edits = read_edits(fake, state, page_id)
    assert edits.deleted == [gone.id] and edits.renamed == [(keep.id, "Call 3 builders")]
    apply_edits(state, edits)
    assert PlanStore(state).get(gone.id).status == "dropped"
    assert PlanStore(state).get(keep.id).title == "Call 3 builders"


def test_a_missing_heading_is_a_rebuild_not_mass_deletion(state, owner, page):
    from ultron.features.schedule import read_edits

    fake, page_id, _ = page
    PlanStore(state).add(WED, "Admin", start="09:00", end="09:30")
    write(state, page)
    fake.trash(fake.child_blocks(page_id)[1]["id"])  # "Today" heading
    assert not read_edits(fake, state, page_id).changed


def test_ticking_on_the_board_ticks_the_planned_block(state, owner):
    from ultron.features.schedule import follow_board

    todo = owner.add(
        kind="task",
        title="Pay Amex",
        details=None,
        project=None,
        area="Wealth",
        due=None,
        question=None,
    )
    block = PlanStore(state).add(WED, "Pay Amex", start="09:00", end="09:10", item_id=todo.id)
    owner.set_status(todo.id, "done")
    assert follow_board(state, WED) == 1 and PlanStore(state).get(block.id).status == "done"


def test_title_from_rendered_text():
    from ultron.features.schedule import title_from

    assert title_from("09:00–10:00 Call builders (~1h)") == "Call builders"
    assert title_from("Call builders") == "Call builders"
    assert title_from("09:00-10:00   Gym (~1h 30m) ") == "Gym"
