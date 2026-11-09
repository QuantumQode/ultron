from datetime import date

import pytest

from ultron.features.focus import Focus, current, resolve
from ultron.features.items import ItemStore
from ultron.features.ranking import rank, score, urgency
from ultron.state import State

TODAY = date(2026, 10, 21)


@pytest.fixture
def env():
    state = State(":memory:")
    yield state, ItemStore(state)
    state.close()


def task(store, title, *, due=None, area="Admin", question=None, moved=0, created=None):
    item = store.add(
        kind="task",
        title=title,
        details=None,
        project=None,
        area=area,
        due=due,
        question=question,
    )
    fields = {}
    if moved:
        fields["times_moved"] = moved
    if created:
        fields["created_at"] = created
    return store.update(item.id, **fields) if fields else item


def titles(ranked):
    return [r.item.title for r in ranked]


def balanced(state, store):
    return resolve(current(state), store)


def test_urgency_steps():
    assert urgency("2026-10-18", TODAY) == (10.9, "overdue 3 days")
    assert urgency("2026-10-21", TODAY)[1] == "due today"
    assert urgency("2026-10-22", TODAY)[1] == "due tomorrow"
    assert urgency("2026-10-24", TODAY)[1] == "due in 3 days"
    assert urgency("2026-10-27", TODAY)[1] == "due this week"
    assert urgency(None, TODAY) == (1.0, None)
    assert urgency("2027-03-01", TODAY)[0] < urgency(None, TODAY)[0]  # far off < undated


def test_order_overdue_then_today_then_soon(env):
    state, store = env
    task(store, "Someday thing")
    task(store, "Due Friday", due="2026-10-23")
    task(store, "Overdue bill", due="2026-10-19")
    task(store, "Due today", due="2026-10-21")
    assert titles(rank(store, balanced(state, store), TODAY)) == [
        "Overdue bill",
        "Due today",
        "Due Friday",
        "Someday thing",
    ]


def test_pushed_back_items_climb_and_say_why(env):
    state, store = env
    task(store, "Fresh", due="2026-10-27")
    slipping = task(store, "Call the accountant", due="2026-10-27", moved=3)
    top = rank(store, balanced(state, store), TODAY)[0]
    assert top.item.id == slipping.id and "pushed back 3×" in top.reasons


def test_focus_weights_lift_their_area(env):
    state, store = env
    task(store, "Admin chore", area="Admin")
    task(store, "Acme sales calls", area="Wealth")
    wealth = resolve(Focus({"Wealth": 1.0}), store)
    top = rank(store, wealth, TODAY)[0]
    assert top.item.title == "Acme sales calls" and "Wealth focus" in top.reasons


def test_open_questions_sink_to_the_bottom(env):
    state, store = env
    task(
        store,
        "Old email thing",
        due="2026-10-19",
        question="From an email on 20 Sep. Still needed?",
    )
    task(store, "Undated chore")
    ranked = rank(store, balanced(state, store), TODAY)
    assert titles(ranked) == ["Undated chore", "Old email thing"]  # even though it's overdue
    assert "has an open question" in ranked[-1].reasons


def test_old_undated_items_rise_slowly(env):
    state, store = env
    task(store, "New idea task")
    old = task(store, "Ancient task", created="2026-09-01T09:00:00+00:00")
    top = rank(store, balanced(state, store), TODAY)[0]
    assert top.item.id == old.id and "waiting 50 days" in top.reasons
    assert score(old, TODAY, balanced(state, store)).score == 3.0  # 1 + capped 2


def test_only_open_tasks_and_limit(env):
    state, store = env
    done = task(store, "Done", due="2026-10-19")
    store.set_status(done.id, "done")
    store.add(
        kind="habit",
        title="Meditate",
        details=None,
        project=None,
        area="Health",
        due=None,
        question=None,
    )
    for n in range(5):
        task(store, f"T{n}")
    assert len(rank(store, balanced(state, store), TODAY)) == 5
    assert len(rank(store, balanced(state, store), TODAY, limit=3)) == 3


def test_ties_go_to_what_has_not_been_planned_for_longest(env):
    from ultron.features.schedule import PlanStore

    state, store = env
    made = "2026-10-20T09:00:00+00:00"
    a = task(store, "A", created=made)
    b = task(store, "B", created=made)
    c = task(store, "C", created=made)
    plans = PlanStore(state)
    plans.add(date(2026, 10, 20), "A", item_id=a.id)
    plans.add(date(2026, 10, 18), "B", item_id=b.id)
    assert titles(rank(store, balanced(state, store), TODAY)) == ["C", "B", "A"]
    assert c.id > a.id  # never planned beats being older


def test_a_set_day_waits_for_its_day(env):
    state, store = env
    gp = store.update(task(store, "Call GP", due="2026-10-26").id, due_on=True)
    task(store, "Someday thing")
    ranked = rank(store, balanced(state, store), TODAY)
    assert titles(ranked) == ["Someday thing", "Call GP"]  # not urgent before Monday
    assert ranked[1].reasons == ["set for Mon 26 Oct, not before"]
    assert score(gp, date(2026, 10, 26), balanced(state, store)).reasons == ["due today"]
