import pytest

from ultron.brain.chat import THINK_HARDER
from ultron.brain.tools import MAX_STRICT_NULLABLES, TOOLS
from ultron.features.planner import PlanResult
from ultron.features.sorter import SortResult
from ultron.features.suggestions import Proposal


def nullable_fields(tools):
    return [
        f"{t['name']}.{key}"
        for t in tools
        if t["strict"]
        for key, schema in t["input_schema"]["properties"].items()
        if "anyOf" in schema or isinstance(schema.get("type"), list)
    ]


def test_strict_tools_stay_under_the_api_union_limit():
    # The API 400s every chat request past this limit (it broke chat on 2026-10-21).
    fields = nullable_fields([*TOOLS, THINK_HARDER])
    assert len(fields) <= MAX_STRICT_NULLABLES, fields


def schema_nullables(model):
    schema = model.model_json_schema()
    objects = [schema, *schema.get("$defs", {}).values()]
    return [
        key
        for obj in objects
        for key, prop in obj.get("properties", {}).items()
        if any(option.get("type") == "null" for option in prop.get("anyOf", []))
    ]


def test_structured_outputs_stay_under_the_union_limit():
    # The same cap may apply to output schemas; keep them under it rather than find out live.
    for model in (SortResult, PlanResult, Proposal):
        assert len(schema_nullables(model)) <= MAX_STRICT_NULLABLES, model.__name__


def test_request_plan_asks_the_front_end_for_a_draft():
    from ultron.brain.tools import ToolEffects, run_tool

    effects = ToolEffects()
    out = run_tool(None, effects, "request_plan", {"day": "tomorrow"})
    assert effects.plan_day == "tomorrow"
    assert "don't write the plan" in out


def test_set_time_fixes_a_one_off_but_not_a_weekly_entry():
    from ultron.brain.tools import ToolEffects, run_tool
    from ultron.features.items import ItemStore
    from ultron.state import State

    state = State(":memory:")
    store = ItemStore(state)
    base = dict(details=None, project=None, area="Health", question=None)
    paddle = store.add(
        kind="schedule",
        title="Paddle",
        due="2026-10-26",
        start_time="07:00",
        **{**base, "question": "How long, and travel each way?"},
    )
    work = store.add(
        kind="schedule",
        title="Work",
        days="thu,sun",
        due=None,
        start_time="07:00",
        end_time="18:00",
        **base,
    )
    effects = ToolEffects()
    out = run_tool(store, effects, "set_time", {"id": paddle.id, "when": "07:00-08:30"})
    assert "07:00-08:30" in out and effects.items_changed
    assert (store.get(paddle.id).end_time, store.get(paddle.id).question) == ("08:30", None)
    with pytest.raises(ValueError, match="suggest_change"):
        run_tool(store, effects, "set_time", {"id": work.id, "when": "08:00-18:00"})
    with pytest.raises(ValueError, match="HH:MM-HH:MM"):
        run_tool(store, effects, "set_time", {"id": paddle.id, "when": "7am"})
    state.close()


def _appointment_store():
    from ultron.features.items import ItemStore
    from ultron.state import State

    state = State(":memory:")
    return state, ItemStore(state)


def _days_ahead(n: int) -> str:
    from datetime import datetime, timedelta

    from ultron.config import DEFAULT_ZONE

    return (datetime.now(DEFAULT_ZONE).date() + timedelta(days=n)).isoformat()


def test_add_appointment_adds_a_timed_one_off():
    from ultron.brain.tools import ToolEffects, run_tool
    from ultron.features.freetime import is_one_off

    state, store = _appointment_store()
    effects = ToolEffects()
    day = _days_ahead(3)
    args = {"title": " Dentist ", "day": day, "when": "07:00-08:30", "area": "Health"}
    out = run_tool(store, effects, "add_appointment", args)
    [item] = store.all()
    assert is_one_off(item) and item.status == "open"
    assert (item.title, item.due, item.due_on) == ("Dentist", day, True)
    assert (item.start_time, item.end_time, item.question) == ("07:00", "08:30", None)
    assert "Added to the schedule" in out and effects.items_changed
    state.close()


@pytest.mark.parametrize(
    ("when", "message"),
    [
        ("", "Ask the owner"),  # no time given: Ultron must ask, not guess
        ("07:00", "Ask the owner"),  # a start with no end
        ("7am", "Ask the owner"),
        ("mon 07:00-08:30", "Ask the owner"),  # weekly, not a one-off
        ("09:00-08:00", "end must be after"),
    ],
)
def test_add_appointment_refuses_without_a_proper_start_and_end(when, message):
    from ultron.brain.tools import ToolEffects, run_tool

    state, store = _appointment_store()
    effects = ToolEffects()
    args = {"title": "Dentist", "day": _days_ahead(3), "when": when, "area": "Health"}
    with pytest.raises(ValueError, match=message):
        run_tool(store, effects, "add_appointment", args)
    assert store.all() == [] and not effects.items_changed
    state.close()


@pytest.mark.parametrize(
    ("day", "message"), [("next monday", "Invalid isoformat"), ("PAST", "in the past")]
)
def test_add_appointment_refuses_a_bad_or_past_date(day, message):
    from ultron.brain.tools import ToolEffects, run_tool

    state, store = _appointment_store()
    day = _days_ahead(-1) if day == "PAST" else day
    args = {"title": "Dentist", "day": day, "when": "07:00-08:30", "area": "Health"}
    with pytest.raises(ValueError, match=message):
        run_tool(store, ToolEffects(), "add_appointment", args)
    assert store.all() == []
    state.close()


def test_an_added_appointment_is_a_fixed_line_on_its_day_only():
    from datetime import date

    from conftest import CONFIGURED

    from ultron.brain.tools import ToolEffects, run_tool
    from ultron.features.freetime import shape_of
    from ultron.features.schedule import fixed_for

    state, store = _appointment_store()
    day = _days_ahead(3)
    args = {"title": "Dentist", "day": day, "when": "07:00-08:30", "area": "Health"}
    run_tool(store, ToolEffects(), "add_appointment", args)

    def dentist_on(d: str):
        shape = shape_of(date.fromisoformat(d), store.all(), settings=CONFIGURED)
        return [(f.start, f.end, f.dated) for f in fixed_for(shape) if f.title == "Dentist"]

    assert dentist_on(day) == [("07:00", "08:30", True)]
    assert dentist_on(_days_ahead(4)) == []
    state.close()


def test_chat_prompt_says_to_ask_for_a_missing_time():
    from ultron.brain.prompts import CHAT
    from ultron.brain.tools import TOOLS

    assert "add_appointment" in [t["name"] for t in TOOLS]
    assert "use add_appointment, not add_task" in CHAT
    assert "ask first" in CHAT
