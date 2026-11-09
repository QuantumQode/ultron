"""Focus modes: what the planner should favour right now.

- Built-in modes set area weights (used to rank to-dos), must-dos (blocks the planner
  should include) and how full a day may get.
- Custom modes are written in plain text on the Board as a fact starting
  "Focus mode <Name>:"; they rank like Balanced and their text goes to the planner.
  "must: a, b" in the text makes must-dos, and "cap 60%" sets how full a day may get:
  "Focus mode Exams: revision first; must: 2-hour study block, 20-min walk; cap 60%".
- `/focus health`, `/focus wealth 2w` (time-boxed, then back to the previous mode),
  `/focus health 60 wealth 40` (a blend).
- The current focus is shown on the Board as a fact line in 🧠 About me & focus.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from ultron.errors import UserFacingError
from ultron.features import profile
from ultron.features.items import ItemStore
from ultron.ops import activity
from ultron.state import State

AREAS = ("Health", "Wealth", "Career", "Relationships", "Learning", "Admin", "Fun")
KEY_FOCUS = "focus.current"  # JSON: {"blend": {name: share}, "until": iso|None, "previous": {...}}
KEY_FOCUS_ITEM = "focus.board_item"  # id of the "Current focus: …" fact on the Board
CUSTOM_PREFIX = re.compile(r"^focus mode\s+([\w' -]{1,30}?)\s*[:\-–]\s*(.+)$", re.IGNORECASE)
DEFAULT = "Balanced"
MUST = re.compile(r"\bmust(?:-dos?)?\s*:\s*([^;.]+)", re.IGNORECASE)
CAP = re.compile(r"\bcap\s*(\d{1,3})\s*%", re.IGNORECASE)
CAP_RANGE = (0.3, 0.9)  # a custom cap outside this is clamped


@dataclass(frozen=True)
class Mode:
    name: str
    summary: str
    weights: dict[str, float] = field(default_factory=dict)  # area -> multiplier (default 1)
    must_dos: tuple[str, ...] = ()
    fill_cap: float | None = None  # share of free time to plan; None: the owner's setting
    custom_text: str | None = None  # the owner's own description, for custom modes

    def weight(self, area: str) -> float:
        return self.weights.get(area, 1.0)


BUILT_IN = {
    m.name.casefold(): m
    for m in (
        Mode("Balanced", "Every area matters equally."),
        Mode(
            "Health",
            "Body first: training, sleep, food.",
            {"Health": 2.5, "Fun": 1.2},
            ("Workout", "In bed on time"),
        ),
        Mode(
            "Wealth",
            "Income first: deep work on money-making projects.",
            {"Wealth": 2.5, "Career": 1.5},
            ("Deep-work block on income work",),
        ),
        Mode(
            "Career",
            "Career first: deep work and skills.",
            {"Career": 2.5, "Learning": 1.5, "Wealth": 1.5},
            ("Deep-work block on career",),
        ),
        Mode(
            "Relationships",
            "People first: family, friends, partner.",
            {"Relationships": 2.5, "Fun": 1.5},
            ("Time with someone who matters",),
        ),
        Mode(
            "Learning",
            "Learning first: courses, books, practice.",
            {"Learning": 2.5, "Career": 1.5},
            ("Learning block (45 min+)",),
        ),
        Mode(
            "Recovery",
            "Light days, rest first. Only what's due.",
            {"Health": 1.5, "Fun": 1.5, "Career": 0.7, "Wealth": 0.7, "Admin": 0.8},
            ("Rest or a walk",),
            fill_cap=0.5,
        ),
    )
}


def custom_modes(store: ItemStore) -> dict[str, Mode]:
    """Modes the owner wrote on the Board: "Focus mode Exams: revision first, …"."""
    modes = {}
    for item in store.all():
        if item.kind != "fact" or item.status != "open":
            continue
        match = CUSTOM_PREFIX.match(item.title.strip())
        if match:
            name, text = match.group(1).strip(), match.group(2).strip()
            must = MUST.search(text)
            cap = CAP.search(text)
            modes[name.casefold()] = Mode(
                name,
                text,
                must_dos=tuple(m.strip() for m in must.group(1).split(",") if m.strip())
                if must
                else (),
                fill_cap=min(max(int(cap.group(1)) / 100, CAP_RANGE[0]), CAP_RANGE[1])
                if cap
                else None,
                custom_text=text,
            )
    return modes


def all_modes(store: ItemStore) -> dict[str, Mode]:
    return {**custom_modes(store), **BUILT_IN}  # a custom mode can't replace a built-in one


# --- the current focus ---


@dataclass(frozen=True)
class Focus:
    blend: dict[str, float]  # mode name -> share (sums to 1)
    until: date | None = None
    previous: dict | None = None  # the focus to return to when `until` passes

    @property
    def label(self) -> str:
        if len(self.blend) == 1:
            text = next(iter(self.blend))
        else:
            text = " + ".join(f"{n} {round(s * 100)}%" for n, s in self.blend.items())
        return text + (f" (until {self.until:%a %d %b})" if self.until else "")

    def as_json(self) -> dict:
        return {
            "blend": self.blend,
            "until": self.until.isoformat() if self.until else None,
            "previous": self.previous,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Focus":
        until = data.get("until")
        return cls(
            data["blend"], date.fromisoformat(until) if until else None, data.get("previous")
        )


@dataclass(frozen=True)
class Resolved:
    """The current focus turned into numbers the planner can use."""

    label: str
    weights: dict[str, float]
    must_dos: list[str]
    fill_cap: float
    notes: list[str]  # custom-mode text for the planner


def current(state: State) -> Focus:
    raw = state.get(KEY_FOCUS)
    return Focus.from_json(json.loads(raw)) if raw else Focus({DEFAULT: 1.0})


def resolve(focus: Focus, store: ItemStore) -> Resolved:
    modes = all_modes(store)
    weights = dict.fromkeys(AREAS, 0.0)
    must: list[str] = []
    notes: list[str] = []
    cap = 0.0
    own_cap = profile.load(store.db).fill_cap
    for name, share in focus.blend.items():
        mode = modes.get(name.casefold(), BUILT_IN[DEFAULT.casefold()])
        for area in AREAS:
            weights[area] += share * mode.weight(area)
        must += [m for m in mode.must_dos if m not in must]
        cap += share * (own_cap if mode.fill_cap is None else mode.fill_cap)
        if mode.custom_text:
            notes.append(f"{mode.name}: {mode.custom_text}")
    return Resolved(focus.label, weights, must, round(cap, 2), notes)


def expire(state: State, store: ItemStore, today: date) -> Focus | None:
    """If a time-boxed focus has ended, go back to the previous one. Returns the ended
    focus (so the owner can be told), else None."""
    focus = current(state)
    if not focus.until or today <= focus.until:
        return None
    back = Focus.from_json(focus.previous) if focus.previous else Focus({DEFAULT: 1.0})
    _save(state, store, back)
    return focus


def _save(state: State, store: ItemStore, focus: Focus) -> None:
    state.set(KEY_FOCUS, json.dumps(focus.as_json()))
    activity.record("focus", f"Focus is now {focus.label}", detail=focus.as_json(), db=state.db)
    _show_on_board(state, store, focus)


def _show_on_board(state: State, store: ItemStore, focus: Focus) -> None:
    """Keep one "Current focus: …" line in 🧠 About me & focus."""
    title = f"Current focus: {focus.label}"
    raw = state.get(KEY_FOCUS_ITEM)
    item = store.get(int(raw)) if raw else None
    if item and item.status == "open":
        if item.title != title:
            store.update(item.id, title=title)
        return
    item = store.add(
        kind="fact", title=title, details=None, project=None, area="Admin", due=None, question=None
    )
    state.set(KEY_FOCUS_ITEM, str(item.id))


# --- /focus ---

DURATION = re.compile(r"^(\d{1,3})\s*(d|day|days|w|wk|week|weeks)$", re.IGNORECASE)


class FocusError(UserFacingError):
    pass


def parse(text: str, store: ItemStore, today: date) -> Focus:
    """'health' · 'wealth 2w' · 'health 60 wealth 40' · 'calm 10d' → a Focus."""
    modes = all_modes(store)
    words = text.split()
    until = None
    if words and DURATION.match(words[-1]):
        n, unit = DURATION.match(words.pop()).groups()
        until = today + timedelta(days=int(n) * (7 if unit.lower().startswith("w") else 1) - 1)
    if not words:
        raise FocusError("Which mode? e.g. /focus health, /focus wealth 2w")
    blend: dict[str, float] = {}
    i = 0
    while i < len(words):
        # mode names can be two words ("deep work"): try the longest match first
        for size in (3, 2, 1):
            name = " ".join(words[i : i + size]).casefold()
            if name in modes:
                break
        else:
            known = ", ".join(m.name for m in modes.values())
            raise FocusError(f"I don't know a mode called '{words[i]}'. Modes: {known}.")
        i += size
        share = 1.0
        if i < len(words) and words[i].rstrip("%").isdigit():
            share = float(words[i].rstrip("%"))
            i += 1
        blend[modes[name].name] = blend.get(modes[name].name, 0) + share
    total = sum(blend.values())
    return Focus({n: round(s / total, 3) for n, s in blend.items()}, until)


def set_focus(state: State, store: ItemStore, text: str, today: date) -> Focus:
    new = parse(text, store, today)
    old = current(state)
    if new.until:
        # time-boxed: remember what to go back to (not another time-boxed focus)
        base = Focus.from_json(old.previous) if old.until and old.previous else old
        new = Focus(new.blend, new.until, Focus(base.blend).as_json())
    _save(state, store, new)
    return new


def describe(state: State, store: ItemStore) -> str:
    focus = current(state)
    r = resolve(focus, store)
    lines = [f"Current focus: {focus.label}"]
    favoured = [a for a, w in sorted(r.weights.items(), key=lambda kv: -kv[1]) if w > 1.0]
    if favoured:
        lines.append("Favours: " + ", ".join(favoured))
    if r.must_dos:
        lines.append("Must-dos each day: " + ", ".join(r.must_dos))
    lines += ["", "Modes:"]
    for mode in all_modes(store).values():
        lines.append(f"  {mode.name}: {mode.summary}")
    lines += [
        "",
        "Switch with /focus health, /focus wealth 2w (two weeks, then back),",
        "or /focus health 60 wealth 40. Make your own on the Board in 🧠 About me & focus:",
        '"Focus mode Exams: revision first, light evenings".',
    ]
    return "\n".join(lines)


def focus_lines(state: State, store: ItemStore) -> list[str]:
    """For chat and planner context."""
    r = resolve(current(state), store)
    lines = [f"Focus: {r.label}"]
    if r.must_dos:
        lines.append("Must-dos: " + ", ".join(r.must_dos))
    lines += r.notes
    return lines
