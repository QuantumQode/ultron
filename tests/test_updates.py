"""Progress notes from the Inbox ("spoke to 2 builders, meeting Fri") update existing items."""

from datetime import date

import pytest

from ultron.features.inbox import Jotting
from ultron.features.items import ItemStore, apply_sort, apply_updates, board_context
from ultron.features.sorter import SortedItem, SortedUpdate, SortOutcome, SortResult, sort_jottings
from ultron.features.sync import updates_message
from ultron.state import State

TODAY = date(2026, 10, 21)


@pytest.fixture
def store():
    state = State(":memory:")
    yield ItemStore(state)
    state.close()


def task(store, title, **kw):
    values = dict(details=None, project=None, area="Admin", due=None, question=None)
    values.update(kw)
    return store.add(kind="task", title=title, **values)


def update(
    target,
    note,
    status=None,
    due=None,
    lines=(1,),
    follow_up=None,
    follow_up_due=None,
    when=None,
    on_day=False,
    follow_up_on_day=False,
):
    return SortedUpdate(
        target=target,
        note=note,
        status=status,
        due=due,
        on_day=on_day,
        follow_up=follow_up,
        follow_up_due=follow_up_due,
        follow_up_on_day=follow_up_on_day,
        when=when,
        lines=list(lines),
    )


def outcome(updates=(), items=()):
    return SortOutcome(
        SortResult(projects=[], items=list(items), updates=list(updates), labels=[]),
        {1: "inbox-1", 2: "inbox-2"},
        [],
        0.0,
    )


def test_done_update_ticks_keeps_wording_saves_note_and_logs(store):
    builders = task(store, "Call builders", project="Acme app sales", area="Wealth")
    ctx = board_context(store, TODAY)
    [applied] = apply_updates(
        store, outcome([update(1, "spoke to 2, meeting Fri", "done")]), ctx, TODAY
    )
    item = store.get(builders.id)
    assert item.status == "done" and item.title == "Call builders"  # owner's wording kept
    assert item.details == "21 Oct: spoke to 2, meeting Fri"
    assert item.source_blocks == ("inbox-1",)
    assert applied.changes == ["ticked", "note saved"]
    [log] = [i for i in store.all() if i.kind == "log"]
    assert log.title == "Call builders: spoke to 2, meeting Fri" and log.project == "Acme app sales"


def test_follow_up_from_the_same_note_is_a_new_item(store):
    task(store, "Call builders", project="Acme app sales")
    ctx = board_context(store, TODAY)
    meeting = SortedItem(
        kind="task",
        title="Meeting with builders",
        details=None,
        project="Acme app sales",
        area="Wealth",
        due="2026-10-23",
        on_day=False,
        status="open",
        when=None,
        question=None,
        same_as=None,
        lines=[1],
    )
    out = outcome([update(1, "spoke to 2, meeting Fri", "done")], [meeting])
    apply_sort(store, out, ctx)
    apply_updates(store, out, ctx, TODAY)
    titles = {(i.title, i.status, i.due) for i in store.all() if i.kind == "task"}
    assert titles == {
        ("Call builders", "done", None),
        ("Meeting with builders", "open", "2026-10-23"),
    }


def test_pushing_the_date_counts_as_a_move(store):
    gp = task(store, "Book GP appointment", due="2026-10-21")
    ctx = board_context(store, TODAY)
    [applied] = apply_updates(
        store,
        outcome([update(1, "couldn't get through, try tomorrow", due="2026-10-22")]),
        ctx,
        TODAY,
    )
    item = store.get(gp.id)
    assert item.due == "2026-10-22" and item.times_moved == 1 and item.status == "open"
    assert applied.changes == ["due Thu 22 Oct", "note saved"]


def test_notes_accumulate_in_details(store):
    item = task(store, "Recover Agent Opus", details="Deadline 27 Oct")
    ctx = board_context(store, TODAY)
    apply_updates(store, outcome([update(1, "emailed support")]), ctx, TODAY)
    assert store.get(item.id).details == "Deadline 27 Oct\n21 Oct: emailed support"


def test_recently_ticked_items_can_be_reopened(store):
    item = task(store, "Pay Amex")
    store.set_status(item.id, "done")
    ctx = board_context(store, TODAY)
    assert ctx.lines == ["task: Pay Amex · done"]
    [applied] = apply_updates(store, outcome([update(1, "payment bounced", "open")]), ctx, TODAY)
    assert store.get(item.id).status == "open" and applied.changes[0] == "reopened"


def test_unknown_targets_are_ignored(store):
    task(store, "x")
    assert apply_updates(store, outcome([update(9, "?")]), board_context(store, TODAY), TODAY) == []


def test_message_says_what_changed(store):
    task(store, "Call builders")
    ctx = board_context(store, TODAY)
    applied = apply_updates(store, outcome([update(1, "spoke to 2", "done")]), ctx, TODAY)
    assert updates_message(applied) == (
        "📥 Updates from your notes:\n"
        "• Call builders: ticked, note saved (spoke to 2)\n"
        "Anything wrong? Fix it on the Board, or tell me here."
    )


async def test_lines_cited_by_updates_count_as_covered():
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from ultron.ops.guard import CostGuard

    state = State(":memory:")

    async def notify(_):
        pass

    guard = CostGuard(state, cap_usd=20, zone=ZoneInfo("Europe/London"), notify=notify)

    class LLM:
        messages = None

        async def parse(self, **kw):
            result = SortResult(projects=[], items=[], updates=[update(1, "booked")], labels=[])
            usage = SimpleNamespace(input_tokens=100, output_tokens=50)
            return SimpleNamespace(stop_reason="end_turn", parsed_output=result, usage=usage)

    llm = LLM()
    llm.messages = llm
    out = await sort_jottings(
        llm,
        guard,
        [Jotting("b1", "GP booked")],
        model="m",
        today=TODAY,
        tz="Europe/London",
        existing=["task: Book GP appointment"],
    )
    assert out.uncovered == []  # the update accounts for the line, so it can be tidied away
    state.close()


def test_follow_up_in_the_update_creates_a_new_todo(store):
    raj = task(store, "Collect £1k from Raj", area="Wealth", project="Raj")
    ctx = board_context(store, TODAY)
    out = outcome(
        [
            update(
                1,
                "received £1k; lent him another £1.5k",
                "done",
                follow_up="Collect £1.5k from Raj",
                follow_up_due="2026-10-28",
            )
        ]
    )
    [applied] = apply_updates(store, out, ctx, TODAY)
    assert store.get(raj.id).status == "done"
    [new] = [i for i in store.all() if i.kind == "task" and i.status == "open"]
    assert (new.title, new.due, new.area, new.project) == (
        "Collect £1.5k from Raj",
        "2026-10-28",
        "Wealth",
        "Raj",
    )
    assert applied.changes == [
        "ticked",
        "note saved",
        'added follow-up "Collect £1.5k from Raj", due Wed 28 Oct',
    ]
    # the same note processed again doesn't add a second copy
    apply_updates(store, out, board_context(store, TODAY), TODAY)
    assert sum(1 for i in store.all() if i.title == "Collect £1.5k from Raj") == 1


def test_bad_follow_up_date_still_adds_the_todo(store):
    task(store, "x")
    out = outcome([update(1, "n", follow_up="Do y", follow_up_due="next wed")])
    apply_updates(store, out, board_context(store, TODAY), TODAY)
    assert [i.due for i in store.all() if i.title == "Do y"] == [None]


# --- schedule entries: times from notes, and weekly entries kept safe ---


def schedule_item(title, when=None, due=None, same_as=None, lines=(1,)):
    return SortedItem(
        kind="schedule",
        title=title,
        details=None,
        project=None,
        area="Admin",
        due=due,
        on_day=False,
        status="open",
        when=when,
        question=None,
        same_as=same_as,
        lines=list(lines),
    )


def weekly_workout(store):
    return store.add(
        kind="schedule",
        title="Workout",
        details="Morning, about 2 hours total",
        project=None,
        area="Health",
        due=None,
        question=None,
        days="mon,tue,fri,sat",
    )


def test_the_sorter_sees_schedule_times(store):
    store.add(
        kind="schedule",
        title="Work",
        details=None,
        project=None,
        area="Admin",
        due=None,
        question=None,
        days="thu,sun",
        start_time="07:00",
        end_time="18:00",
        travel_minutes=30,
    )
    weekly_workout(store)
    assert board_context(store, TODAY).lines == [
        "schedule: Work · thu,sun 07:00-18:00 +30m",
        "schedule: Workout · mon,tue,fri,sat",
    ]


def test_a_one_off_appointment_keeps_its_date_and_time(store):
    out = outcome(items=[schedule_item("Meeting at uncle's", "09:45-10:45 +15m", "2026-10-23")])
    [saved] = apply_sort(store, out)
    assert (saved.days, saved.due, saved.start_time, saved.end_time, saved.travel_minutes) == (
        None,
        "2026-10-23",
        "09:45",
        "10:45",
        15,
    )


def test_a_one_off_note_never_overwrites_the_weekly_entry(store):
    workout = weekly_workout(store)
    ctx = board_context(store, TODAY)
    merges = []
    out = outcome(items=[schedule_item("Gym in the morning", due="2026-10-23", same_as=1)])
    [new] = apply_sort(store, out, ctx, merges, TODAY)
    assert new.id != workout.id and new.due == "2026-10-23"
    assert store.get(workout.id) == workout  # untouched
    assert merges == []


def test_a_repeat_of_a_weekly_entry_only_adds_a_note(store):
    workout = weekly_workout(store)
    ctx = board_context(store, TODAY)
    merges = []
    apply_sort(store, outcome(items=[schedule_item("Gym", same_as=1)]), ctx, merges, TODAY)
    after = store.get(workout.id)
    assert (after.title, after.days, after.due) == ("Workout", "mon,tue,fri,sat", None)
    assert after.details == "Morning, about 2 hours total\n21 Oct: Gym"
    assert updates_message(merges).splitlines()[1] == "• Workout: note saved (Gym)"


def test_merges_are_reported(store):
    task(store, "Call builders")
    ctx = board_context(store, TODAY)
    merges = []
    out = outcome(items=[item_like("Ring the builders", same_as=1)])
    apply_sort(store, out, ctx, merges, TODAY)
    [m] = merges
    assert m.changes == ["merged a repeat", "renamed from 'Call builders'"]


def test_an_update_can_set_a_schedule_time(store):
    meeting = store.add(
        kind="schedule",
        title="Meeting at uncle's, 9:45am",
        details=None,
        project=None,
        area="Admin",
        due="2026-10-23",
        question="How long, and travel each way?",
    )
    ctx = board_context(store, TODAY)
    [applied] = apply_updates(
        store,
        outcome([update(1, "about an hour, 15 min each way", when="09:45-10:45 +15m")]),
        ctx,
        TODAY,
    )
    after = store.get(meeting.id)
    assert (after.start_time, after.end_time, after.travel_minutes) == ("09:45", "10:45", 15)
    assert applied.changes[0] == "time now 09:45-10:45 +15m"


def item_like(title, same_as=None):
    return SortedItem(
        kind="task",
        title=title,
        details=None,
        project=None,
        area="Admin",
        due=None,
        on_day=False,
        status="open",
        when=None,
        question=None,
        same_as=same_as,
        lines=[1],
    )


def test_a_date_it_happens_on_is_kept_apart_from_a_deadline(store):
    gp = task(store, "Call GP")
    ctx = board_context(store, TODAY)
    out = outcome(
        [
            update(
                1,
                "call GP Monday, then a meeting with the builders Fri",
                due="2026-10-26",
                on_day=True,
                follow_up="Meeting with builders",
                follow_up_due="2026-10-23",
                follow_up_on_day=True,
            )
        ]
    )
    [applied] = apply_updates(store, out, ctx, TODAY)
    assert (store.get(gp.id).due, store.get(gp.id).due_on) == ("2026-10-26", True)
    [meeting] = [i for i in store.all() if i.title == "Meeting with builders"]
    assert meeting.due_on
    assert applied.changes[0] == "on Mon 26 Oct"
    assert applied.changes[-1] == 'added follow-up "Meeting with builders", on Fri 23 Oct'
