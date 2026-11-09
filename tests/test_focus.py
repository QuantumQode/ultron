from datetime import date

import pytest

from ultron.features import focus
from ultron.features.focus import FocusError, current, describe, expire, resolve, set_focus
from ultron.features.items import ItemStore
from ultron.state import State

TODAY = date(2026, 10, 21)


@pytest.fixture
def env():
    state = State(":memory:")
    yield state, ItemStore(state)
    state.close()


def fact(store, title):
    return store.add(
        kind="fact", title=title, details=None, project=None, area="Admin", due=None, question=None
    )


def board_line(state, store):
    return store.get(int(state.get(focus.KEY_FOCUS_ITEM))).title


def test_default_is_balanced_with_equal_weights(env):
    state, store = env
    r = resolve(current(state), store)
    assert r.label == "Balanced" and set(r.weights.values()) == {1.0}
    assert r.must_dos == [] and r.fill_cap == 0.7


def test_switching_mode_updates_weights_and_the_board(env):
    state, store = env
    new = set_focus(state, store, "wealth", TODAY)
    r = resolve(new, store)
    assert r.weights["Wealth"] == 2.5 and r.weights["Career"] == 1.5 and r.weights["Fun"] == 1.0
    assert r.must_dos == ["Deep-work block on income work"]
    assert board_line(state, store) == "Current focus: Wealth"
    set_focus(state, store, "health", TODAY)
    assert board_line(state, store) == "Current focus: Health"  # same line, updated
    assert sum(1 for i in store.all() if i.title.startswith("Current focus")) == 1


def test_time_boxed_focus_returns_to_the_previous_one(env):
    state, store = env
    set_focus(state, store, "career", TODAY)
    boxed = set_focus(state, store, "health 2w", TODAY)
    assert boxed.until == date(2026, 11, 3) and boxed.label == "Health (until Tue 03 Nov)"
    assert expire(state, store, date(2026, 11, 3)) is None  # still the last day
    ended = expire(state, store, date(2026, 11, 4))
    assert ended.label.startswith("Health") and current(state).label == "Career"
    assert board_line(state, store) == "Current focus: Career"


def test_a_new_time_box_keeps_the_original_to_return_to(env):
    state, store = env
    set_focus(state, store, "wealth", TODAY)
    set_focus(state, store, "health 1w", TODAY)
    set_focus(state, store, "recovery 3d", TODAY)  # swapped mid-way
    expire(state, store, date(2026, 10, 30))
    assert current(state).label == "Wealth"


def test_blends(env):
    state, store = env
    new = set_focus(state, store, "health 60 wealth 40", TODAY)
    assert new.blend == {"Health": 0.6, "Wealth": 0.4}
    assert new.label == "Health 60% + Wealth 40%"
    r = resolve(new, store)
    assert r.weights["Health"] == pytest.approx(0.6 * 2.5 + 0.4)
    assert r.must_dos == ["Workout", "In bed on time", "Deep-work block on income work"]


def test_recovery_lowers_how_full_days_get(env):
    state, store = env
    assert resolve(set_focus(state, store, "recovery", TODAY), store).fill_cap == 0.5


def test_custom_mode_from_the_board(env):
    state, store = env
    fact(store, "Focus mode Calm: mindfulness first, light evenings, a walk every day")
    r = resolve(set_focus(state, store, "calm 10d", TODAY), store)
    assert r.label == "Calm (until Fri 30 Oct)"
    assert r.notes == ["Calm: mindfulness first, light evenings, a walk every day"]
    assert set(r.weights.values()) == {1.0}  # ranks like Balanced
    assert "Calm: mindfulness first" in describe(state, store)


def test_custom_mode_cant_replace_a_built_in_one(env):
    state, store = env
    fact(store, "Focus mode Health: eat cake")
    assert resolve(set_focus(state, store, "health", TODAY), store).notes == []


def test_unknown_mode_is_a_readable_error(env):
    state, store = env
    with pytest.raises(FocusError, match="don't know a mode called 'zen'"):
        set_focus(state, store, "zen", TODAY)
    with pytest.raises(FocusError, match="Which mode"):
        set_focus(state, store, "2w", TODAY)


def test_deleted_board_line_is_recreated(env):
    state, store = env
    set_focus(state, store, "wealth", TODAY)
    store.update(int(state.get(focus.KEY_FOCUS_ITEM)), status="dropped")
    set_focus(state, store, "health", TODAY)
    assert board_line(state, store) == "Current focus: Health"


def test_describe_lists_modes_and_how_to_switch(env):
    state, store = env
    text = describe(state, store)
    assert text.startswith("Current focus: Balanced")
    assert "Recovery: Light days, rest first." in text and "/focus wealth 2w" in text


def test_custom_mode_sets_must_dos_and_a_clamped_cap(env):
    state, store = env
    fact(store, "Focus mode Calm: stress first; must: 20-min walk, 15-min meditation; cap 60%")
    fact(store, "Focus mode Sprint: work; cap 99%")
    modes = focus.custom_modes(store)
    assert modes["calm"].must_dos == ("20-min walk", "15-min meditation")
    assert modes["calm"].fill_cap == 0.6
    assert modes["sprint"].fill_cap == focus.CAP_RANGE[1]
