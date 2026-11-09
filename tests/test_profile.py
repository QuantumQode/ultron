"""Planning settings live in the owner's profile, not in code."""

import pathlib
import re
from dataclasses import asdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import CONFIGURED, configure, make_settings

from ultron.features import planner, profile, suggestions
from ultron.features.freetime import shape_of, to_hhmm
from ultron.features.items import ItemStore
from ultron.features.profile import DEFAULTS
from ultron.features.schedule import fixed_for
from ultron.features.suggestions import Proposal
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 21, 19, 0, tzinfo=LONDON)
FRI = date(2026, 10, 23)


@pytest.fixture
def state():
    s = State(":memory:")
    yield s
    s.close()


def add(store, kind, title, **kw):
    values = dict(details=None, project=None, area="Health", due=None, question=None)
    values.update(kw)
    return store.add(kind=kind, title=title, **values)


def test_a_new_owner_gets_the_defaults(state):
    assert profile.load(state) == DEFAULTS
    assert DEFAULTS.sport_gap is None and DEFAULTS.wind_down_note == ""


def test_stored_settings_win_and_unknown_keys_are_ignored(state):
    state.set(profile.KEY, '{"gap": 15, "sport_gap": 60, "gone": 1}')
    s = profile.load(state)
    assert (s.gap, s.sport_gap, s.max_blocks) == (15, 60, DEFAULTS.max_blocks)
    assert profile.load(state.db) == s  # from a bare connection too


def test_save_keeps_earlier_settings_logs_and_shows_one_board_line(state):
    store = ItemStore(state)
    profile.save(state, gap=15)
    profile.save(state, max_blocks=6)
    assert profile.stored(state) == {"gap": 15, "max_blocks": 6}
    lines = [i.title for i in store.all() if i.title.startswith("Planning settings:")]
    assert len(lines) == 1 and "15 min between blocks" in lines[0] and "at most 6" in lines[0]
    with pytest.raises(ValueError, match="Unknown setting"):
        profile.save(state, colour="red")


@pytest.mark.parametrize(
    ("name", "text", "value"),
    [
        ("fill_cap", "60%", 0.6),
        ("fill_cap", "0.55", 0.55),
        ("gap", "15 min", 15),
        ("sport_gap", "off", None),
        ("sport_gap", "90", 90),
        ("wind_down_note", " read ", "read"),
    ],
)
def test_values_are_parsed_from_text(name, text, value):
    assert profile.parse_value(name, text) == value


@pytest.mark.parametrize(
    ("name", "text"), [("fill_cap", "95%"), ("gap", "soon"), ("gap", "500"), ("nope", "1")]
)
def test_bad_values_are_refused(name, text):
    with pytest.raises(ValueError):
        profile.parse_value(name, text)


def test_the_day_follows_the_settings(state):
    store = ItemStore(state)
    add(store, "schedule", "Sleep", days="fri", start_time="23:00", end_time="07:00")
    add(store, "schedule", "Run club", days="fri", start_time="07:00", end_time="08:00")
    add(store, "schedule", "Morning routine", days="fri", anchor="wake")
    default = shape_of(FRI, store.all(), settings=DEFAULTS)
    assert to_hhmm(default.wake) == "06:30" and default.wind_down == 22 * 60 + 30
    mine = profile.PlanSettings(wake_prep=60, wind_down=45, wind_down_note="read")
    shape = shape_of(FRI, store.all(), settings=mine)
    assert to_hhmm(shape.wake) == "06:00" and to_hhmm(shape.wind_down) == "22:15"
    assert fixed_for(shape)[-2].note == "read"
    assert fixed_for(default)[-2].note is None  # no note unless the owner set one


def test_the_routine_length_follows_the_settings(state):
    store = ItemStore(state)
    add(store, "schedule", "Morning routine", days="fri", anchor="wake")
    [routine] = shape_of(FRI, store.all(), settings=DEFAULTS).fixed
    assert routine.end - routine.start == DEFAULTS.routine_minutes
    [routine] = shape_of(FRI, store.all(), settings=CONFIGURED).fixed
    assert routine.end - routine.start == 35


def test_without_a_sport_rule_the_planner_has_none(state):
    configure(state, DEFAULTS)
    store = ItemStore(state)
    add(store, "schedule", "Workout", days="fri")
    add(store, "schedule", "Padel", due=FRI.isoformat(), start_time="07:00", end_time="08:30")
    shapes = {FRI: planner.nightly.day_shape(state, FRI, NOW)}
    check = planner._Check(shapes, {FRI: 600}, {FRI: []}, {})
    check.workout_ids = {i.id for i in store.all() if i.title == "Workout"}
    [workout] = [i for i in store.all() if i.title == "Workout"]
    block = planner.PlannedBlock(
        day=FRI.isoformat(), start="08:40", end="09:40", title="Gym", item_id=workout.id
    )
    assert check.gym_too_soon(block) is None
    configure(state, CONFIGURED)
    check.shapes = {FRI: planner.nightly.day_shape(state, FRI, NOW)}
    assert "from 10:00" in check.gym_too_soon(block)


def test_a_setting_card_shows_and_applies_the_change(state):
    configure(state, CONFIGURED)
    proposal = Proposal(
        kind="setting", action="set", summary="Longer gaps", why="Rushed", setting="gap", text="15"
    )
    change = suggestions.propose(state, proposal, "chat", NOW)
    card = suggestions.card_text(make_settings(), state, change)
    assert "Planning setting gap: 10 → 15" in card
    suggestions.apply(make_settings(), state, change.id, NOW)
    assert profile.load(state).gap == 15 and profile.load(state).sport_gap == 90
    assert suggestions.get(state, change.id).before == {"settings": asdict(CONFIGURED)}
    bad = Proposal(kind="setting", action="set", summary="x", why="y", setting="gap", text="?")
    with pytest.raises(ValueError):
        suggestions.check(state, bad, NOW.date())


def test_no_prompt_hard_codes_a_coaching_style():
    """The tone comes from the owner's Coaching style fact, never from the code."""
    root = pathlib.Path(__file__).parents[1] / "ultron"
    words = re.compile(r"\b(blunt|cheerleading|analytical)\b", re.IGNORECASE)
    found = [
        f"{p.name}: {line.strip()}"
        for p in root.rglob("*.py")
        for line in p.read_text().splitlines()
        if words.search(line)
    ]
    assert found == []
