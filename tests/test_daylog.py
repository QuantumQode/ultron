from datetime import date
from zoneinfo import ZoneInfo

import pytest
from conftest import CONFIGURED
from fake_notion import FakeNotion

from ultron.brain.tools import ToolEffects, run_tool
from ultron.features import daylog
from ultron.features.board import render_item, snapshot, write_board
from ultron.features.items import ItemStore
from ultron.features.schedule import PlanStore, block_text, title_from
from ultron.integrations.notion import ensure_workspace
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
WED = date(2026, 10, 21)


@pytest.fixture
def state():
    s = State(":memory:")
    yield s
    s.close()


def plan_day(state):
    plans = PlanStore(state)
    a = plans.add(WED, "Workout", start="07:00", end="08:30")
    b = plans.add(WED, "Call builders", start="09:00", end="09:45")
    c = plans.add(WED, "Admin", start="10:00", end="10:30")
    d = plans.add(WED, "Read Seneca")
    plans.set_status(a.id, "done")
    plans.set_status(b.id, "partial")
    plans.set_status(c.id, "skipped")
    return a, b, c, d


def test_tally_counts_each_outcome(state):
    plan_day(state)
    t = daylog.tally(state, WED)
    assert t.total == 4 and t.headline == "1 of 4 done, 1 partly, 1 skipped, 1 not reached"
    assert daylog.DayTally(WED).headline == "nothing was planned"


def test_close_day_writes_one_dated_entry_and_rewrites_it(state):
    plan_day(state)
    item = daylog.close_day(state, WED, "Balanced")
    assert item.kind == "log" and item.created_at.startswith("2026-10-21")
    assert item.title == "Day review: 1 of 4 done, 1 partly, 1 skipped, 1 not reached · Balanced"
    assert item.details.splitlines() == [
        "Done: Workout",
        "Partly done: Call builders",
        "Skipped: Admin",
        "Not reached: Read Seneca",
    ]
    again = daylog.close_day(state, WED, "Balanced", reflection="Tired after a bad night")
    assert again.id == item.id and again.title.endswith("· reflection noted")
    assert daylog.reflection_of(state, WED) == "Tired after a bad night"
    assert sum(1 for i in ItemStore(state).all() if i.kind == "log") == 1


def test_unplanned_ticks_are_listed_as_also_done(state):
    store, plans = ItemStore(state), PlanStore(state)

    def task(title, done_at=None):
        values = dict(details=None, project=None, area="Admin", due=None, question=None)
        item = store.add(kind="task", title=title, **values)
        return store.update(item.id, status="done", done_at=done_at) if done_at else item

    planned = task("Call Nina", "2026-10-21T10:00:00+00:00")
    plans.set_status(plans.add(WED, "Call Nina", item_id=planned.id).id, "done")
    task("Changed the bedsheets", "2026-10-20T23:30:00+00:00")  # 00:30 on Wed in London
    task("Paid council tax", "2026-10-21T15:00:00+00:00")
    task("Yesterday's thing", "2026-10-20T12:00:00+00:00")
    task("Still open")
    t = daylog.tally(state, WED, LONDON)
    assert t.also_done == ["Changed the bedsheets", "Paid council tax"]
    assert t.headline == "1 of 1 done"  # the headline counts the plan only
    item = daylog.close_day(state, WED, "Balanced", zone=LONDON)
    assert item.details.splitlines() == [
        "Done: Call Nina",
        "Also done: Changed the bedsheets, Paid council tax",
    ]


def test_day_review_shows_as_a_toggle_with_the_details_inside(state):
    plan_day(state)
    item = daylog.close_day(state, WED, "Balanced", reflection="ok day")
    block, text = render_item(item, WED, LONDON, routine_minutes=CONFIGURED.routine_minutes)
    assert block["type"] == "toggle" and text.startswith("21 Oct · Day review: 1 of 4 done")
    inside = [
        c["paragraph"]["rich_text"][0]["text"]["content"] for c in block["toggle"]["children"]
    ]
    assert inside[0] == "Done: Workout" and inside[-1] == "Reflection: ok day"


def test_a_changed_day_review_is_replaced_on_the_board(state):
    fake = FakeNotion()
    ws = ensure_workspace(fake, state, fake.root).workspace
    store = ItemStore(state)
    plan_day(state)
    daylog.close_day(state, WED, "Balanced")

    def write():
        write_board(fake, ws, store, snapshot(fake, ws, store), WED, LONDON)

    write()
    first = store.all()[0].board_block_id
    daylog.close_day(state, WED, "Balanced", reflection="better than expected")
    write()
    item = [i for i in store.all() if i.kind == "log"][0]
    assert item.board_block_id != first  # replaced, so the nested lines are fresh
    assert fake.objects[first]["archived"]
    toggle = item.board_block_id
    assert "Reflection: better than expected" in fake.texts(toggle)


def test_commit_creates_a_todo_and_a_commitment(state):
    item, block = daylog.commit(state, "Call Nina", date(2026, 10, 22), area="Wealth")
    assert item.kind == "task" and item.due == "2026-10-22"
    assert block.item_id == item.id and block.start is None
    assert [b.title for b in daylog.commitments(state, date(2026, 10, 22))] == ["Call Nina"]


def test_commit_chat_tool(state):
    store = ItemStore(state)
    effects = ToolEffects()
    out = run_tool(
        store,
        effects,
        "commit",
        {"title": "Finish the deck", "day": "2026-10-23", "area": "Career", "project": None},
    )
    assert out.startswith("Committed for Fri 23 Oct: Finish the deck") and effects.items_changed
    assert [b.title for b in PlanStore(state).for_day(date(2026, 10, 23))] == ["Finish the deck"]
    with pytest.raises(ValueError):
        run_tool(store, effects, "commit", {"title": "x", "day": "Friday", "area": "Admin"})


def test_partial_and_skipped_marks_on_the_schedule(state):
    _, b, c, _ = plan_day(state)
    plans = PlanStore(state)
    assert block_text(plans.get(b.id)) == "09:00–09:45 Call builders (~45m) · ◐ partly done"
    assert block_text(plans.get(c.id)) == "10:00–10:30 Admin (~30m) · ❌ skipped"
    assert title_from("09:00–09:45 Call builders (~45m) · ◐ partly done") == "Call builders"
