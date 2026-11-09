"""The owner's planning settings: the numbers the planner and the day's shape follow.

They live in the owner's profile (kv "profile.settings"), never in code. A new owner gets
DEFAULTS; anything they set is stored and wins, so changing a default never moves them.
The words about the owner (coaching style, planning style, routines) are facts, not settings.
"""

import json
import sqlite3
from dataclasses import dataclass, fields, replace

from ultron.features.items import ItemStore
from ultron.ops import activity
from ultron.state import State

KEY = "profile.settings"
KEY_BOARD_ITEM = "profile.board_item"  # the "Planning settings: …" fact in 🧠 About me


@dataclass(frozen=True)
class PlanSettings:
    fill_cap: float = 0.7  # share of free time the planner may use (a focus mode can override)
    gap: int = 10  # minutes between planned blocks
    wake_prep: int = 30  # wake this long before an early first commitment
    wind_down: int = 30  # minutes before sleep that are never planned
    wind_down_note: str = ""  # what the wind-down is for, shown on the Schedule
    routine_minutes: int = 30  # length of a routine anchored to waking, if it has none
    max_blocks: int = 8  # planned blocks a day
    max_short: int = 3  # blocks shorter than short_block a day
    short_block: int = 25  # minutes: shorter than this counts as short
    sport_gap: int | None = None  # minutes between booked sport and the gym; None: no rule


DEFAULTS = PlanSettings()
NAMES = {f.name for f in fields(PlanSettings)}
OFF = {"off", "none", "no", "null", ""}


def parse_value(name: str, text: str | None):
    """The typed value of a setting written as text: '60%' or '0.6' for fill_cap, minutes
    for the rest, 'off' for no sport rule. Raises ValueError saying what's wrong."""
    if name not in NAMES:
        raise ValueError(f"setting must be one of: {', '.join(sorted(NAMES))}.")
    raw = (text or "").strip().casefold().removesuffix("min").removesuffix("mins").strip()
    if name == "wind_down_note":
        return (text or "").strip()
    if name == "sport_gap" and raw in OFF:
        return None
    if name == "fill_cap":
        try:
            value = float(raw.removesuffix("%"))
        except ValueError:
            raise ValueError("fill_cap is a share of free time, e.g. '60%'.") from None
        value = value / 100 if value > 1 else value
        if not 0.3 <= value <= 0.9:
            raise ValueError("fill_cap must be between 30% and 90%.")
        return round(value, 2)
    if not raw.isdigit() or not 0 <= int(raw) <= 240:
        raise ValueError(f"{name} must be a whole number of minutes (or a count), 0–240.")
    return int(raw)


def load(source: State | sqlite3.Connection) -> PlanSettings:
    """The owner's settings over the defaults. Unknown keys are ignored. `source` is the
    State or its connection (code that only holds an ItemStore passes `store.db`)."""
    return replace(DEFAULTS, **{k: v for k, v in stored(source).items() if k in NAMES})


def stored(source: State | sqlite3.Connection) -> dict:
    """Only what the owner has set (the rest are defaults)."""
    db = getattr(source, "db", source)  # a State (or anything holding .db), or a connection
    row = db.execute("SELECT value FROM kv WHERE key = ?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(state: State, **changes) -> PlanSettings:
    """Store changed settings (only the ones given) and log them."""
    unknown = set(changes) - NAMES
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    current = stored(state)
    current.update(changes)
    state.set(KEY, json.dumps(current))
    activity.record("profile", "Planning settings changed", detail=changes, db=state.db)
    settings = load(state)
    show_on_board(state, ItemStore(state), settings)
    return settings


def show_on_board(state: State, store: ItemStore, settings: PlanSettings | None = None) -> None:
    """Keep one "Planning settings: …" line in 🧠 About me & focus (like "Current focus")."""
    title = describe(settings or load(state))
    raw = state.get(KEY_BOARD_ITEM)
    item = store.get(int(raw)) if raw else None
    if item and item.status == "open":
        if item.title != title:
            store.update(item.id, title=title)
        return
    item = store.add(
        kind="fact", title=title, details=None, project=None, area="Admin", due=None, question=None
    )
    state.set(KEY_BOARD_ITEM, str(item.id))


def describe(s: PlanSettings) -> str:
    """One line for 🧠 About me."""
    parts = [
        f"plan up to {round(s.fill_cap * 100)}% of free time",
        f"{s.gap} min between blocks",
        f"at most {s.max_blocks} blocks a day ({s.max_short} under {s.short_block} min)",
        f"up {s.wake_prep} min before an early start",
        f"{s.wind_down} min wind-down" + (f" ({s.wind_down_note})" if s.wind_down_note else ""),
        f"routine {s.routine_minutes} min if not set",
    ]
    if s.sport_gap:
        parts.append(f"gym at least {s.sport_gap} min after sport")
    return "Planning settings: " + " · ".join(parts)
