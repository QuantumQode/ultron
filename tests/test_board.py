from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import CONFIGURED
from fake_notion import FakeNotion

from ultron.features import board
from ultron.features.board import apply_changes, read_changes, snapshot, write_board
from ultron.features.items import ItemStore, apply_sort, board_context
from ultron.features.sorter import SortedItem, SortedProject, SortOutcome, SortResult
from ultron.integrations.notion import BOARD_HINT, ensure_workspace, rich_text
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
TODAY = date(2026, 10, 20)


@pytest.fixture
def env():
    fake = FakeNotion()
    state = State(":memory:")
    ws = ensure_workspace(fake, state, fake.root).workspace
    yield fake, ItemStore(state), ws
    state.close()


def task(store, title, *, project=None, area="Admin", due=None, question=None, **kw):
    return store.add(
        kind="task",
        title=title,
        project=project,
        area=area,
        due=due,
        question=question,
        details=None,
        **kw,
    )


def other(store, kind, title, details=None, area="Learning"):
    return store.add(
        kind=kind, title=title, details=details, area=area, project=None, due=None, question=None
    )


def sync(fake, store, ws, today=TODAY, shown_day=None):
    """One Board sync: read edits back, apply them, then write."""
    snap = snapshot(fake, ws, store)
    changes = read_changes(fake, snap, ws, store, today, shown_day)
    apply_changes(store, changes, today)
    snap = snapshot(fake, ws, store)
    result = write_board(fake, ws, store, snap, today, LONDON)
    return changes, result


def section_children(fake, ws, title):
    [heading] = [b for b in fake.child_blocks(ws.board_id) if _text(b) == title]
    return fake.texts(heading["id"])


def log_weeks(fake, ws):
    """{week heading: its lines} in 📓 Log & journal, top to bottom."""
    [log] = [b for b in fake.child_blocks(ws.board_id) if _text(b) == "📓 Log & journal"]
    return {_text(w): fake.texts(w["id"]) for w in fake.child_blocks(log["id"])}


def _text(block):
    body = block[block["type"]]
    return "".join(t.get("plain_text") or t["text"]["content"] for t in body.get("rich_text", []))


# --- layout ---


def test_first_write_lays_out_sections_and_groups(env):
    fake, store, ws = env
    task(store, "Call builders", project="Acme app sales")
    task(store, "Change bedsheets")
    task(store, "Book marathon", area="Health", due="2026-10-23")
    task(store, "Ask Jo again", project="Jo Tennis")
    other(store, "goal", "Move to Portugal", details="online business £2200/month")
    other(store, "principle", "Focus on output not input")
    other(store, "reference", "SMART", details="Specific, Measurable, Achievable, Relevant, Timed")
    store.upsert_project("Acme app sales", "Wealth", "2 hours each")

    _, result = sync(fake, store, ws)
    page = fake.texts(ws.board_id)
    assert page == [
        BOARD_HINT,
        "✅ To-do",
        "Acme app sales · 2 hours each",
        "[ ] Call builders",
        "Jo Tennis",
        "[ ] Ask Jo again",
        "Health",
        "[ ] Book marathon · due Fri",
        "Admin",
        "[ ] Change bedsheets",
        "🎯 Goals",
        "🧭 Principles & notes",
    ]
    assert section_children(fake, ws, "🎯 Goals") == [
        "Move to Portugal — online business £2200/month"
    ]
    assert section_children(fake, ws, "🧭 Principles & notes") == [
        "Focus on output not input",
        "SMART",
    ]
    goals = next(b for b in fake.child_blocks(ws.board_id) if _text(b) == "🎯 Goals")
    assert goals["heading_2"]["is_toggleable"] is True
    todo = next(b for b in fake.child_blocks(ws.board_id) if _text(b) == "✅ To-do")
    assert "is_toggleable" not in todo["heading_2"]  # always open
    assert result.created == 7


def test_second_sync_changes_nothing(env):
    fake, store, ws = env
    task(store, "Call builders", project="Acme")
    other(store, "idea", "Coffee van in Portugal")
    sync(fake, store, ws)
    fake.calls.clear()
    changes, result = sync(fake, store, ws)
    assert not changes.any
    assert (result.created, result.updated, result.removed) == (0, 0, 0)
    assert all(c == "blocks.children.list" for c in fake.calls)


def test_long_log_line_is_split_and_reads_back_unchanged(env):
    # 21 Oct: an "Also done" line of 2,293 characters made every Board write fail
    fake, store, ws = env
    line = "Also done: " + ", ".join(f"Finished task number {i}" for i in range(120))
    assert len(line) > 2000
    other(store, "log", "Day review: 1 done · Balanced", details=f"Done: Gym\n{line}")
    sync(fake, store, ws)

    [heading] = [b for b in fake.child_blocks(ws.board_id) if _text(b) == "📓 Log & journal"]
    [week] = fake.child_blocks(heading["id"])
    [entry] = fake.child_blocks(week["id"])
    assert fake.texts(entry["id"]) == ["Done: Gym", line]
    long_paragraph = fake.child_blocks(entry["id"])[1]["paragraph"]
    assert len(long_paragraph["rich_text"]) == 2

    changes, result = sync(fake, store, ws)
    assert not changes.any
    assert (result.created, result.updated, result.removed) == (0, 0, 0)


def test_new_task_lands_in_its_group_not_at_the_bottom(env):
    fake, store, ws = env
    task(store, "Call builders", project="Acme")
    task(store, "Change bedsheets")
    other(store, "idea", "Coffee van")
    sync(fake, store, ws)
    task(store, "Create functionalities list", project="Acme")
    task(store, "Get NAS", area="Admin")
    sync(fake, store, ws)
    assert fake.texts(ws.board_id)[1:] == [
        "✅ To-do",
        "Acme",
        "[ ] Call builders",
        "[ ] Create functionalities list",
        "Admin",
        "[ ] Change bedsheets",
        "[ ] Get NAS",
        "💡 Ideas",
    ]


def test_new_group_and_section_slot_into_order(env):
    fake, store, ws = env
    task(store, "Change bedsheets")  # Admin
    other(store, "idea", "Coffee van")
    sync(fake, store, ws)
    task(store, "Read Atomic Habits", area="Learning")  # Learning comes before Admin
    other(store, "goal", "Save money")  # Goals come before Ideas
    sync(fake, store, ws)
    assert fake.texts(ws.board_id)[1:] == [
        "✅ To-do",
        "Learning",
        "[ ] Read Atomic Habits",
        "Admin",
        "[ ] Change bedsheets",
        "🎯 Goals",
        "💡 Ideas",
    ]


def test_more_than_100_items_are_appended_in_chunks(env):
    fake, store, ws = env
    for n in range(130):
        other(store, "idea", f"idea {n}")
    _, result = sync(fake, store, ws)
    assert result.created == 130
    assert len(section_children(fake, ws, "💡 Ideas")) == 130


def test_questions_and_overdue_dates_render(env):
    fake, store, ws = env
    task(store, "Wait for insurer update", due="2026-10-14", question="Still waiting?")
    sync(fake, store, ws)
    assert "[ ] Wait for insurer update · overdue (Wed 14 Oct) (?) Still waiting?" in fake.texts(
        ws.board_id
    )


# --- the owner's edits win ---


def test_ticked_todo_stays_today_then_moves_to_the_log(env):
    fake, store, ws = env
    today = datetime.now(LONDON).date()  # ticks are stamped with the real time
    item = task(store, "Change bedsheets")
    sync(fake, store, ws, today=today)
    fake.objects[store.get(item.id).board_block_id]["to_do"]["checked"] = True

    changes, _ = sync(fake, store, ws, today=today)
    assert changes.ticked == [item.id]
    assert store.get(item.id).status == "done"
    assert "[x] Change bedsheets" in fake.texts(ws.board_id)  # still visible today

    sync(fake, store, ws, today=today + timedelta(days=1))
    # off ✅ To-do (empty headings removed too) and into its week in 📓 Log
    assert fake.texts(ws.board_id) == [BOARD_HINT, "📓 Log & journal"]
    assert log_weeks(fake, ws) == {
        f"Week of {today - timedelta(days=today.weekday()):%a %d %b} · 1 done": [
            f"[x] Change bedsheets · {today:%a %d}"
        ]
    }


def test_unticking_reopens(env):
    fake, store, ws = env
    today = datetime.now(LONDON).date()  # ticks are stamped with the real time
    item = task(store, "Change bedsheets")
    sync(fake, store, ws, today)
    block = fake.objects[store.get(item.id).board_block_id]
    block["to_do"]["checked"] = True
    sync(fake, store, ws, today)
    block["to_do"]["checked"] = False
    changes, _ = sync(fake, store, ws, today)
    assert changes.unticked == [item.id] and store.get(item.id).status == "open"


def test_deleting_a_line_drops_the_item_and_empty_headings(env):
    fake, store, ws = env
    keep = task(store, "Change bedsheets")
    gone = task(store, "Call builders", project="Acme")
    sync(fake, store, ws)
    fake.objects[store.get(gone.id).board_block_id]["archived"] = True

    changes, _ = sync(fake, store, ws)
    assert changes.deleted == [gone.id]
    assert store.get(gone.id).status == "dropped"
    assert store.get(keep.id).status == "open"
    assert "Acme" not in fake.texts(ws.board_id)


def test_rewording_keeps_due_date_out_of_the_title(env):
    fake, store, ws = env
    item = task(store, "Book marathon", area="Health", due="2026-11-30")
    sync(fake, store, ws)
    block = fake.objects[store.get(item.id).board_block_id]
    block["to_do"]["rich_text"] = [{"plain_text": "Book Bath marathon · due Mon 30 Nov"}]

    changes, _ = sync(fake, store, ws)
    assert changes.renamed == [(item.id, "Book Bath marathon")]
    assert store.get(item.id).due == "2026-11-30"


def test_deleting_the_question_dismisses_it(env):
    fake, store, ws = env
    item = task(store, "Do meal prep", area="Health", question="Still for today?")
    sync(fake, store, ws)
    block = fake.objects[store.get(item.id).board_block_id]
    block["to_do"]["rich_text"] = [{"plain_text": "Do meal prep"}]

    changes, _ = sync(fake, store, ws)
    assert changes.dismissed == [item.id] and changes.renamed == []
    assert store.get(item.id).question is None
    assert "[ ] Do meal prep" in fake.texts(ws.board_id)


def test_lines_typed_on_the_board_come_back_as_new_notes(env):
    fake, store, ws = env
    other(store, "idea", "Coffee van")
    sync(fake, store, ws)
    typed = fake.add_text(ws.board_id, "paragraph", "call dentist tomorrow")
    ideas = next(b for b in fake.child_blocks(ws.board_id) if _text(b) == "💡 Ideas")
    inside = fake.add_text(ideas["id"], "bulleted_list_item", "laundry van in Lisbon")

    changes, _ = sync(fake, store, ws)
    assert {(j.block_id, j.text) for j in changes.new_lines} == {
        (typed, "call dentist tomorrow"),
        (inside, "- laundry van in Lisbon"),
    }


def test_lines_typed_under_a_heading_say_which(env):
    fake, store, ws = env
    task(store, "Create brand logo", project="Robin")
    task(store, "Change bedsheets")
    other(store, "idea", "Coffee van")
    sync(fake, store, ws)
    keys = store.block_keys()
    robin, admin = keys["group:todo:project:Robin"], keys["group:todo:area:Admin"]
    # Put a line straight after the Robin heading, as the owner would
    page = fake.children[ws.board_id]
    typed = fake.add_text(ws.board_id, "to_do", "Come up with pricing for him")
    page.remove(typed)
    page.insert(page.index(robin) + 1, typed)
    mine = fake.add_text(ws.board_id, "heading_3", "Mum")
    under_mine = fake.add_text(ws.board_id, "to_do", "call her Sunday")
    ideas = next(b for b in fake.child_blocks(ws.board_id) if _text(b) == "💡 Ideas")
    in_ideas = fake.add_text(ideas["id"], "bulleted_list_item", "laundry van")

    changes, _ = sync(fake, store, ws)
    found = {j.block_id: (j.heading, j.group) for j in changes.new_lines}
    assert found[typed] == ("Robin", "project:Robin")
    assert found[under_mine] == ("Mum", None)  # the owner's own heading: a hint only
    assert found[mine] == (None, None)
    assert found[in_ideas] == (None, None)  # a new section starts afresh
    assert admin not in found


def test_changed_due_date_updates_in_place(env):
    fake, store, ws = env
    item = task(store, "Book marathon", due="2026-11-30")
    sync(fake, store, ws)
    block_id = store.get(item.id).board_block_id
    store.update(item.id, due="2026-12-05")
    fake.calls.clear()
    _, result = sync(fake, store, ws)
    assert result.updated == 1 and result.created == 0
    assert store.get(item.id).board_block_id == block_id
    assert "blocks.update" in fake.calls


def test_moving_to_another_project_moves_the_block(env):
    fake, store, ws = env
    item = task(store, "Send business card", project="Robin")
    task(store, "Ask Jo again", project="Jo Tennis")
    sync(fake, store, ws)
    store.update(item.id, project="Jo Tennis")
    sync(fake, store, ws)
    assert fake.texts(ws.board_id)[1:] == [
        "✅ To-do",
        "Jo Tennis",
        "[ ] Ask Jo again",
        "[ ] Send business card",
    ]


def test_hint_deleted_still_works(env):
    fake, store, ws = env
    fake.objects[ws.board_hint_id]["archived"] = True
    task(store, "Change bedsheets")
    sync(fake, store, ws)
    assert fake.texts(ws.board_id) == ["✅ To-do", "Admin", "[ ] Change bedsheets"]


# --- items from a sort ---


def outcome(items, projects=(), line_blocks=None):
    line_blocks = line_blocks or {1: "inbox-1", 2: "inbox-2"}
    return SortOutcome(
        SortResult(projects=list(projects), items=items, updates=[], labels=[]),
        line_blocks,
        [],
        0.0,
    )


def sorted_item(title, **kw):
    values = dict(
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
        same_as=None,
        lines=[1],
    )
    values.update(kw)
    return SortedItem(**values)


def test_apply_sort_adds_items_and_projects(env):
    _, store, _ = env
    apply_sort(
        store,
        outcome(
            [
                sorted_item("Call builders", project="Acme"),
                sorted_item("Make list", status="done"),
            ],
            projects=[SortedProject(name="Acme", area="Wealth", note="2 hours each")],
        ),
    )
    items = store.all()
    assert [(i.title, i.status, i.source_blocks) for i in items] == [
        ("Call builders", "open", ("inbox-1",)),
        ("Make list", "done", ("inbox-1",)),
    ]
    assert store.projects()[0].note == "2 hours each"


def test_a_set_day_is_saved_only_with_a_date(env):
    _, store, _ = env
    apply_sort(
        store,
        outcome(
            [
                sorted_item("Call GP", due="2026-10-26", on_day=True),
                sorted_item("Someday", on_day=True),  # no date: nothing to pin
            ]
        ),
    )
    assert [(i.title, i.due_on) for i in store.all()] == [("Call GP", True), ("Someday", False)]


def test_done_from_notes_goes_straight_to_the_log(env):
    fake, store, ws = env
    apply_sort(store, outcome([sorted_item("Make builders list", status="done")]))
    sync(fake, store, ws)
    assert fake.texts(ws.board_id) == [BOARD_HINT, "📓 Log & journal"]
    [lines] = log_weeks(fake, ws).values()
    assert lines[0].startswith("[x] Make builders list · ")
    assert store.all()[0].done_at is not None


def test_same_as_updates_existing_item_and_counts_moves(env):
    _, store, _ = env
    first = task(store, "Book marathon", due="2026-11-01")
    ctx = board_context(store)
    assert ctx.lines == ["task: Book marathon · due 2026-11-01"]
    apply_sort(
        store, outcome([sorted_item("Book marathon", due="2026-11-15", same_as=1, lines=[2])]), ctx
    )
    item = store.get(first.id)
    assert len(store.all()) == 1
    assert item.due == "2026-11-15" and item.times_moved == 1
    assert item.source_blocks == ("inbox-2",)


def test_due_suffix_formats():
    assert board.due_suffix("2026-10-22", TODAY, on_day=True) == " · on Thu"
    assert board.due_suffix("2026-10-20", TODAY, on_day=True) == " · today"
    assert board.due_suffix("2026-10-20", TODAY) == " · due today"
    assert board.due_suffix("2026-10-22", TODAY) == " · due Thu"
    assert board.due_suffix("2026-11-12", TODAY) == " · due Thu 12 Nov"
    assert board.due_suffix("2027-01-05", TODAY) == " · due Tue 05 Jan 2027"
    assert board.due_suffix("2026-10-01", TODAY) == " · overdue (Thu 01 Oct)"


# --- ☑️ Today's habits ---


def habit(store, title, days=None):
    return store.add(
        kind="habit",
        title=title,
        details=None,
        project=None,
        area="Health",
        due=None,
        question=None,
        days=days,
    )


def test_habits_due_today_are_tick_boxes_at_the_top(env):
    fake, store, ws = env
    habit(store, "Learn Spanish 30 mins")
    habit(store, "Long run", days="sat")  # not due on Tue 20 Oct
    task(store, "Change bedsheets")
    sync(fake, store, ws)
    texts = fake.texts(ws.board_id)
    assert texts[1:4] == ["☑️ Today's habits", "[ ] Learn Spanish 30 mins", "✅ To-do"]
    assert "🔁 Habits" in texts  # the other habit sits in the collapsed section
    assert section_children(fake, ws, "🔁 Habits") == ["Long run"]


def test_ticking_a_habit_keeps_it_for_today_and_builds_a_streak(env):
    from datetime import timedelta

    from ultron.features import habits

    fake, store, ws = env
    h = habit(store, "Learn Spanish 30 mins")
    yesterday = TODAY - timedelta(days=1)
    habits.keep(store, h.id, yesterday)
    sync(fake, store, ws)
    block = fake.objects[store.get(h.id).board_block_id]
    block["to_do"]["checked"] = True
    changes, _ = sync(fake, store, ws)
    assert changes.habits_kept == [(h.id, TODAY)] and changes.ticked == []
    item = store.get(h.id)
    assert item.status == "open" and item.kept_on == TODAY.isoformat() and item.streak == 2
    assert "[x] Learn Spanish 30 mins · 🔥 2" in fake.texts(ws.board_id)

    # next morning the box is clear again; the streak still shows (it can still be kept)
    changes, _ = sync(fake, store, ws, today=TODAY + timedelta(days=1), shown_day=TODAY)
    assert changes.habits_kept == []  # yesterday's tick isn't read as today's
    assert "[ ] Learn Spanish 30 mins · 🔥 2" in fake.texts(ws.board_id)


def test_unticking_a_habit_takes_back_today(env):
    from ultron.features import habits

    fake, store, ws = env
    h = habit(store, "Read 5 mins")
    habits.keep(store, h.id, TODAY)
    sync(fake, store, ws)
    fake.objects[store.get(h.id).board_block_id]["to_do"]["checked"] = False
    changes, _ = sync(fake, store, ws)
    assert changes.habits_unkept == [(h.id, TODAY)] and store.get(h.id).kept_on is None


def test_late_tick_after_the_last_sync_counts_for_that_day(env):
    from datetime import timedelta

    fake, store, ws = env
    h = habit(store, "Read 5 mins")
    sync(fake, store, ws)
    fake.objects[store.get(h.id).board_block_id]["to_do"]["checked"] = True  # ticked at 23:50
    tomorrow = TODAY + timedelta(days=1)
    changes, _ = sync(fake, store, ws, today=tomorrow, shown_day=TODAY)
    assert changes.habits_kept == [(h.id, TODAY)]
    assert "[ ] Read 5 mins" in fake.texts(ws.board_id)  # fresh box for the new day


# --- editing a 📌 line changes its times (2026-10-25) ---


def test_a_schedule_line_edit_changes_its_times():
    from ultron.features.board import schedule_edit
    from ultron.features.items import Item

    base = dict(
        details=None,
        project=None,
        area="Health",
        question=None,
        source_blocks=(),
        created_at="",
        updated_at="",
        done_at=None,
        times_moved=0,
        board_block_id=None,
        board_place=None,
        board_text=None,
        status="open",
    )
    sleep = Item(
        id=1,
        kind="schedule",
        title="Sleep",
        due=None,
        days="mon,tue,wed,thu,fri,sat,sun",
        start_time="22:00",
        end_time="06:00",
        **base,
    )
    assert schedule_edit(sleep, "Every day · 22:00–07:00 · Sleep") == {
        "end_time": "07:00",
        "title": "Sleep",
    }
    assert schedule_edit(sleep, "Weekdays · 22:00–06:00 · Sleep")["days"] == "mon,tue,wed,thu,fri"
    assert schedule_edit(sleep, "Mon–Wed · 22:00–06:00 · Sleep")["days"] == "mon,tue,wed"
    assert schedule_edit(sleep, "Just words") is None  # a plain rename
    paddle = Item(
        id=2,
        kind="schedule",
        title="Paddle",
        due="2026-10-26",
        start_time="07:00",
        **{**base, "question": "How long, and travel each way?"},
    )
    assert schedule_edit(paddle, "07:00–08:30 · Paddle") == {
        "end_time": "08:30",
        "question": None,
        "title": "Paddle",
    }


def test_a_typed_line_knows_its_section_and_the_to_do_above_it(env):
    fake, store, ws = env
    task(store, "Talk with boys about Halloween", area="Fun")
    other(store, "idea", "Coffee van")
    sync(fake, store, ws)
    page = fake.children[ws.board_id]
    above = store.get(next(i.id for i in store.all() if i.title.startswith("Talk"))).board_block_id
    first = fake.add_text(ws.board_id, "numbered_list_item", "Skyline rooftop")
    second = fake.add_text(ws.board_id, "numbered_list_item", "Neon club party")
    for n, block in enumerate((first, second), 1):
        page.remove(block)
        page.insert(page.index(above) + n, block)
    ideas = next(b for b in fake.child_blocks(ws.board_id) if _text(b) == "💡 Ideas")
    in_ideas = fake.add_text(ideas["id"], "bulleted_list_item", "laundry van")
    changes, _ = sync(fake, store, ws)
    found = {j.block_id: j for j in changes.new_lines}
    assert all(j.on_board for j in found.values())
    assert (found[first].above, found[first].kinds) == (
        "Talk with boys about Halloween",
        ("task", "schedule"),
    )
    assert found[second].above == "Talk with boys about Halloween"  # past the line above it
    assert (found[in_ideas].kinds, found[in_ideas].above) == (("idea",), "Coffee van")


def test_a_to_dos_notes_show_as_a_grey_line_under_it_and_follow_changes(env):
    fake, store, ws = env
    item = task(store, "Talk with boys about Halloween", area="Fun")
    store.update(item.id, details="Options: Skyline rooftop, Neon club party")
    sync(fake, store, ws)
    block_id = store.get(item.id).board_block_id
    [note] = fake.child_blocks(block_id)
    assert _text(note) == "Options: Skyline rooftop, Neon club party"
    assert note["paragraph"]["rich_text"][0]["annotations"]["color"] == "gray"
    changes, _ = sync(fake, store, ws)
    assert changes.new_lines == [] and not changes.renamed  # the note isn't a typed line
    store.update(item.id, details="Options: Skyline rooftop, Neon club party\n25 Oct: booked Neon")
    sync(fake, store, ws)
    assert store.get(item.id).board_block_id == block_id  # redrawn in place
    assert [_text(b) for b in fake.child_blocks(block_id)] == [
        "Options: Skyline rooftop, Neon club party\n25 Oct: booked Neon"
    ]
    store.update(item.id, details=None)
    sync(fake, store, ws)
    assert fake.child_blocks(block_id) == []


def test_a_one_off_shows_its_date_and_editing_its_time_keeps_it():
    from ultron.features.board import render_item, schedule_edit
    from ultron.state import State

    state = State(":memory:")
    store = ItemStore(state)
    item = store.add(
        kind="schedule",
        title="Physio session",
        details="Online",
        project=None,
        area="Health",
        due="2026-11-25",
        question=None,
        start_time="10:00",
        end_time="11:00",
    )
    text = render_item(
        item,
        date(2026, 10, 25),
        ZoneInfo("Europe/London"),
        routine_minutes=CONFIGURED.routine_minutes,
    )[1]
    assert text == "Wed 25 Nov · 10:00–11:00 · Physio session (Online)"
    assert schedule_edit(item, "Wed 25 Nov · 10:30–11:30 · Physio session (Online)") == {
        "start_time": "10:30",
        "end_time": "11:30",
        "title": "Physio session",
    }
    state.close()


# --- 📓 Log by week ---

LOG_TODAY = date(2026, 10, 27)  # a Tuesday


def done_on(store, title, stamp, **kw):
    item = task(store, title, **kw)
    return store.update(item.id, status="done", done_at=stamp)


def dropped_on(store, title, stamp):
    item = task(store, title)
    return store.update(item.id, status="dropped", dropped_at=stamp)


def entry_on(store, title, day, details="Done: Gym"):
    item = other(store, "log", title, details=details, area="Admin")
    return store.update(item.id, created_at=f"{day}T12:00:00+00:00")


def test_log_holds_a_folded_week_each_newest_first(env):
    fake, store, ws = env
    entry_on(store, "Day review: 1 of 2 done · Balanced", "2026-10-21")
    dropped_on(store, "Play tennis Court 2", "2026-10-22T09:00:00+00:00")
    done_on(store, "Pay Amex", "2026-10-20T10:00:00+00:00", question="Still needed?")
    done_on(store, "Call builders", "2026-10-26T18:00:00+00:00", project="Acme")
    task(store, "Change bedsheets")
    sync(fake, store, ws, LOG_TODAY)

    assert fake.texts(ws.board_id) == [
        BOARD_HINT,
        "✅ To-do",
        "Admin",
        "[ ] Change bedsheets",
        "📓 Log & journal",
    ]
    assert list(log_weeks(fake, ws).items()) == [
        ("Week of Mon 26 Oct · 1 done", ["[x] Call builders · Mon 26"]),
        (
            "Week of Mon 19 Oct · 1 done",
            [
                "[x] Pay Amex · Tue 20",  # no stale question; done, dropped, then entries
                "Play tennis Court 2 · dropped Thu 22",
                "21 Oct · Day review: 1 of 2 done · Balanced",
            ],
        ),
    ]
    [log] = [b for b in fake.child_blocks(ws.board_id) if _text(b) == "📓 Log & journal"]
    weeks = fake.child_blocks(log["id"])
    assert all(w["heading_3"]["is_toggleable"] for w in weeks)
    struck = fake.child_blocks(weeks[1]["id"])[1]["bulleted_list_item"]["rich_text"]
    assert struck[0]["annotations"] == {"strikethrough": True}
    assert "annotations" not in struck[1]  # " · dropped Thu 22" isn't struck

    fake.calls.clear()
    changes, result = sync(fake, store, ws, LOG_TODAY)
    assert not changes.any
    assert (result.created, result.updated, result.removed) == (0, 0, 0)
    assert all(c == "blocks.children.list" for c in fake.calls)


def test_new_lines_slot_into_their_week_in_date_order(env):
    fake, store, ws = env
    done_on(store, "Pay Amex", "2026-10-20T10:00:00+00:00")
    done_on(store, "Buy milk", "2026-10-23T10:00:00+00:00")
    sync(fake, store, ws, LOG_TODAY)
    done_on(store, "Order coffee", "2026-10-21T10:00:00+00:00")
    done_on(store, "Call builders", "2026-10-26T10:00:00+00:00")  # a new, newer week
    _, result = sync(fake, store, ws, LOG_TODAY)

    assert result.created == 2
    assert list(log_weeks(fake, ws).items()) == [
        ("Week of Mon 26 Oct · 1 done", ["[x] Call builders · Mon 26"]),
        (
            "Week of Mon 19 Oct · 3 done",  # the count follows
            ["[x] Pay Amex · Tue 20", "[x] Order coffee · Wed 21", "[x] Buy milk · Fri 23"],
        ),
    ]


def test_dropping_a_to_do_moves_it_to_the_log_struck_through(env):
    fake, store, ws = env
    item = task(store, "Call builders")
    sync(fake, store, ws, LOG_TODAY)
    fake.objects[store.get(item.id).board_block_id]["archived"] = True  # deleted on the Board

    sync(fake, store, ws, LOG_TODAY)
    assert store.get(item.id).status == "dropped"
    assert store.get(item.id).dropped_at is not None
    [lines] = log_weeks(fake, ws).values()
    assert lines == [f"Call builders · dropped {datetime.now(LONDON):%a %d}"]
    assert "✅ To-do" not in fake.texts(ws.board_id)


def test_unticking_a_line_in_the_log_reopens_it(env):
    fake, store, ws = env
    item = done_on(store, "Pay Amex", "2026-10-20T10:00:00+00:00")
    sync(fake, store, ws, LOG_TODAY)
    fake.objects[store.get(item.id).board_block_id]["to_do"]["checked"] = False

    changes, _ = sync(fake, store, ws, LOG_TODAY)
    assert changes.unticked == [item.id]
    assert store.get(item.id).status == "open"
    assert "[ ] Pay Amex" in fake.texts(ws.board_id)
    assert "📓 Log & journal" not in fake.texts(ws.board_id)  # its only line left


def test_deleting_a_line_in_the_log_only_hides_it(env):
    fake, store, ws = env
    keep = done_on(store, "Pay Amex", "2026-10-20T10:00:00+00:00")
    gone = done_on(store, "Buy milk", "2026-10-21T10:00:00+00:00")
    struck = dropped_on(store, "Play tennis", "2026-10-21T10:00:00+00:00")
    sync(fake, store, ws, LOG_TODAY)
    fake.objects[store.get(gone.id).board_block_id]["archived"] = True
    fake.objects[store.get(struck.id).board_block_id]["archived"] = True

    changes, _ = sync(fake, store, ws, LOG_TODAY)
    assert changes.hidden == [gone.id, struck.id] and not changes.deleted
    assert (store.get(gone.id).status, store.get(gone.id).log_hidden) == ("done", True)
    assert store.get(struck.id).status == "dropped"
    assert log_weeks(fake, ws) == {"Week of Mon 19 Oct · 1 done": ["[x] Pay Amex · Tue 20"]}
    assert store.get(keep.id).log_hidden is False


def test_a_deleted_week_is_drawn_again(env):
    fake, store, ws = env
    item = done_on(store, "Pay Amex", "2026-10-20T10:00:00+00:00")
    entry = entry_on(store, "Day review: 1 of 1 done · Balanced", "2026-10-20")
    sync(fake, store, ws, LOG_TODAY)
    week = store.block_keys()["group:log:week:2026-10-19"]
    fake.objects[week]["archived"] = True  # takes its lines with it

    changes, _ = sync(fake, store, ws, LOG_TODAY)
    assert not changes.deleted and not changes.hidden
    assert store.get(item.id).status == "done" and store.get(entry.id).status == "open"
    assert log_weeks(fake, ws) == {
        "Week of Mon 19 Oct · 1 done": [
            "[x] Pay Amex · Tue 20",
            "20 Oct · Day review: 1 of 1 done · Balanced",
        ]
    }


def test_a_line_typed_in_a_week_is_a_new_note(env):
    fake, store, ws = env
    done_on(store, "Pay Amex", "2026-10-20T10:00:00+00:00")
    sync(fake, store, ws, LOG_TODAY)
    week = store.block_keys()["group:log:week:2026-10-19"]
    typed = fake.add_text(week, "paragraph", "Great week overall")

    changes, _ = sync(fake, store, ws, LOG_TODAY)
    [jotting] = changes.new_lines
    assert (jotting.block_id, jotting.text, jotting.group) == (typed, "Great week overall", None)


def test_old_flat_log_entries_move_into_their_week(env):
    # Before 25 Oct, entries sat straight under 📓 Log & journal
    fake, store, ws = env
    entry = entry_on(store, "Day review: 1 of 1 done · Balanced", "2026-10-20")
    log = fake.add_text(ws.board_id, "heading_2", "📓 Log & journal", is_toggleable=True)
    store.set_block_key("section:log", log)
    old = fake.add_block(log, {"type": "toggle", "toggle": {"rich_text": rich_text("old")}})
    store.update(entry.id, board_block_id=old, board_place="log/", board_text="old")

    changes, _ = sync(fake, store, ws, LOG_TODAY)
    assert not changes.any  # the old line is redrawn in its week, not dropped
    assert store.get(entry.id).status == "open"
    assert log_weeks(fake, ws) == {
        "Week of Mon 19 Oct": ["20 Oct · Day review: 1 of 1 done · Balanced"]
    }


def test_hidden_to_dos_and_dropped_ideas_stay_out_of_the_log(env):
    fake, store, ws = env
    hidden = done_on(store, "Old email to-do", "2026-10-20T10:00:00+00:00")
    store.update(hidden.id, log_hidden=True)
    idea = other(store, "idea", "Coffee van")
    store.update(idea.id, status="dropped")  # only to-dos are logged
    sync(fake, store, ws, LOG_TODAY)
    assert "📓 Log & journal" not in fake.texts(ws.board_id)


def test_dropped_at_follows_the_status():
    state = State(":memory:")
    store = ItemStore(state)
    item = task(store, "Call builders")
    assert store.update(item.id, status="dropped").dropped_at is not None
    assert store.update(item.id, status="open").dropped_at is None
    assert store.add(**_task_fields("Gone", status="dropped")).dropped_at is not None
    state.close()


def _task_fields(title, **kw):
    return dict(
        kind="task",
        title=title,
        details=None,
        project=None,
        area="Admin",
        due=None,
        question=None,
        **kw,
    )


# --- lines the owner typed ---


def test_lines_indented_under_a_to_do_become_its_notes(env):
    fake, store, ws = env
    item = task(store, "Talk with boys about Halloween")
    store.update(item.id, details="Options: Skyline rooftop")
    sync(fake, store, ws)
    block = store.get(item.id).board_block_id
    fake.add_text(block, "to_do", "Neon club party")
    fake.add_text(block, "bulleted_list_item", "Jazz night event")

    changes, _ = sync(fake, store, ws)
    notes = "Options: Skyline rooftop\nNeon club party\nJazz night event"
    assert changes.noted == [(item.id, "Neon club party\nJazz night event")]
    assert store.get(item.id).details == notes
    assert fake.texts(block) == [notes]  # one grey note line, the typed ones folded into it

    changes, _ = sync(fake, store, ws)
    assert not changes.any
