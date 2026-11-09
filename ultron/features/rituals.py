"""Ritual times the owner changed from a suggestion card.

`.env` sets the defaults (MORNING_BRIEF_TIME and so on). An applied card stores the new
time here, and it's laid over the settings at startup and straight after the change, so
everything that reads the settings sees it. The front end then reschedules its jobs.
"""

import json
from datetime import time
from typing import Any

from ultron.config import WEEKDAYS
from ultron.state import State

KEY = "rituals.overrides"  # JSON: {settings field: "HH:MM" or weekday}

RITUALS = {
    # ritual name -> (settings field for the time, label)
    "morning_brief": ("morning_brief_time", "morning brief"),
    "midday_checkin": ("midday_checkin_time", "midday check-in"),
    "evening_review": ("nightly_time", "evening review"),
    "weekly_review": ("weekly_review_time", "weekly review"),
}
WEEKLY_DAY = "weekly_review_day"


def overrides(state: State) -> dict[str, str]:
    raw = state.get(KEY)
    return json.loads(raw) if raw else {}


def _apply(settings: Any, field: str, value: str) -> None:
    if field == WEEKLY_DAY:
        setattr(settings, field, value)
    else:
        setattr(settings, field, time.fromisoformat(value))


def load(settings: Any, state: State) -> None:
    """Lay stored changes over the `.env` defaults (at startup)."""
    for field, value in overrides(state).items():
        _apply(settings, field, value)


def current(settings: Any, ritual: str) -> str:
    field, _ = RITUALS[ritual]
    text = f"{getattr(settings, field):%H:%M}"
    if ritual == "weekly_review":
        text = f"{settings.weekly_review_day} {text}"
    return text


def set_time(
    settings: Any, state: State, ritual: str, at: str, day: str | None = None
) -> dict[str, str]:
    """Change a ritual's time (and the weekly review's day). Returns the overrides it
    replaced, for undo: {field: previous value or "" if it was the default}."""
    field, _ = RITUALS[ritual]
    time.fromisoformat(at)  # ValueError if malformed
    changes = {field: at}
    if day:
        if day not in WEEKDAYS:
            raise ValueError(f"Unknown day {day!r}; use one of {', '.join(WEEKDAYS)}")
        if ritual != "weekly_review":
            raise ValueError("Only the weekly review has a day.")
        changes[WEEKLY_DAY] = day
    stored = overrides(state)
    before = {f: stored.get(f, "") for f in changes}
    stored.update(changes)
    state.set(KEY, json.dumps(stored))
    for f, value in changes.items():
        _apply(settings, f, value)
    return before


def restore(settings: Any, state: State, defaults: Any, before: dict[str, str]) -> None:
    """Undo `set_time`: put back the earlier overrides ("" = back to the `.env` default)."""
    stored = overrides(state)
    for field, value in before.items():
        if value:
            stored[field] = value
            _apply(settings, field, value)
        else:
            stored.pop(field, None)
            setattr(settings, field, getattr(defaults, field))
    state.set(KEY, json.dumps(stored))
