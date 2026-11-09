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
