"""The shape of a day: when the owner is up, what's fixed, and what time is free to plan.

The numbers (wake prep, wind-down, routine length, fill cap, gap) are the owner's
planning settings (`profile.PlanSettings`), carried on the DayShape.

- Fixed commitments block their time plus travel each way (work 09:00–17:00 with a
  30-minute commute blocks 08:30–17:30).
- The Sleep entry is the one place the wake time lives. On days that start early, the
  owner wakes `wake_prep` minutes before the first commitment; otherwise at the end of
  the sleep entry.
- A routine anchored to waking (the morning routine) starts whenever the owner wakes. On
  an early day it's squeezed into the time before leaving, and it never counts as an
  early start itself.
- Sport booked as a commitment (padel, tennis…) is marked, so the planner can apply the
  owner's sport rule (`sport_gap`).
- The last `wind_down` minutes before sleep are wind-down: never planned.
- Entries with no set time (workouts) are flexible: the planner places them on the day.
- An entry with no days but a date (`due`) is a one-off appointment: it applies on that
  date only. Its time can come from the stored fields or from the title/details
  ("9:45am", "12pm"); with no end time it blocks ONE_OFF_MINUTES, marked as a guess.
- The planner may fill at most `fill_cap` of free time, with `gap` minutes between blocks.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from ultron.features.items import Item
from ultron.features.profile import DEFAULTS, PlanSettings

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_WAKE, DEFAULT_BED = 7 * 60, 23 * 60
MIN_SLOT = 15  # free gaps shorter than this aren't worth planning into
ONE_OFF_MINUTES = 60  # an appointment with a start but no end blocks this long
LENGTH_GUESSED = "length guessed"
WAKE = "wake"  # Item.anchor: starts when the owner wakes
SPORT = re.compile(
    r"\b(padel|paddle|tennis|squash|badminton|football|5-a-side|basketball|volleyball|"
    r"swim|swimming|climbing|bouldering|yoga|pilates|boxing|spin class|cycling|hike|"
    r"hiking|golf|court)\b",
    re.IGNORECASE,
)


def is_sport(title: str) -> bool:
    """'Paddle', 'Court booked for 12pm', 'Yoga class': exercise booked at a set time.
    The gym itself ("Workout") isn't: that's what sport makes room around."""
    return bool(SPORT.search(title))


def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Slot:
    start: int  # minutes after midnight
    end: int

    @property
    def minutes(self) -> int:
        return self.end - self.start

    def __str__(self) -> str:
        return f"{to_hhmm(self.start)}–{to_hhmm(self.end)}"


@dataclass(frozen=True)
class Commitment:
    key: str
    title: str
    start: int | None  # with travel included; None = flexible (no set time)
    end: int | None
    note: str | None = None
    preferred: int | None = None  # flexible entries: a time named in the note ("18:30")
    dated: bool = False  # a one-off on this date (not a weekly entry)
    sport: bool = False  # exercise booked at a set time (the owner's sport rule applies)
    routine: bool = False  # anchored to waking (the morning routine)


@dataclass
class DayShape:
    day: date
    wake: int
    bedtime: int
    fixed: list[Commitment] = field(default_factory=list)  # timed, in order
    flexible: list[Commitment] = field(default_factory=list)  # for the planner to place
    free: list[Slot] = field(default_factory=list)
    settings: PlanSettings = DEFAULTS

    @property
    def wind_down(self) -> int:
        return self.bedtime - self.settings.wind_down

    @property
    def free_minutes(self) -> int:
        return sum(s.minutes for s in self.free)

    @property
    def budget(self) -> int:
        """Most the planner may schedule today (the over-planning guard)."""
        return round(self.free_minutes * self.settings.fill_cap)


def short_note(details: str | None, limit: int = 40) -> str | None:
    """First clause of a schedule entry's details: 'Morning, about 2 hours total'."""
    if not details:
        return None
    first = re.split(r"[.:;]\s| \(", details, maxsplit=1)[0].strip().rstrip(".")
    return first if 0 < len(first) <= limit else None


CLOCK = re.compile(
    r"\b(?:(?P<h12>1[0-2]|0?[1-9])(?:[:.](?P<m12>[0-5]\d))?\s*(?P<ampm>[ap])\.?m\b\.?"
    r"|(?P<h24>[01]?\d|2[0-3]):(?P<m24>[0-5]\d)\b"
    r"|(?P<noon>noon|midday))",
    re.IGNORECASE,
)


def clock_in(text: str | None) -> int | None:
    """The first clock time written in `text`, in minutes: '18:30', '9:45am', '9.45 am',
    '9:45 pm', '12pm', 'noon'. A bare number ('2 hours', '£1.5k') is never a time."""
    match = CLOCK.search(text or "")
    if not match:
        return None
    if match["noon"]:
        return 12 * 60
    if match["ampm"]:
        hour = int(match["h12"]) % 12 + (12 if match["ampm"].lower() == "p" else 0)
        return hour * 60 + int(match["m12"] or 0)
    return int(match["h24"]) * 60 + int(match["m24"])


DURATION = re.compile(
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b\s*(?:and\s*)?)?"
    r"(?:(?P<m>\d+)\s*(?:m|min|mins|minute|minutes)\b)?",
    re.IGNORECASE,
)


def minutes_in(text: str | None) -> int | None:
    """The first length written in `text`, in minutes: '2 hours', '1h 50m', '1.5h',
    '30mins', '45 min'. A bare number ('110') counts as minutes only if it's all there is."""
    text = (text or "").strip()
    if text.isdigit():
        return int(text) or None
    for match in DURATION.finditer(text):
        if match["h"] or match["m"]:
            return round(float(match["h"] or 0) * 60) + int(match["m"] or 0) or None
    return None


def applies_on(item: Item, day: date) -> bool:
    """Weekly entries apply on their days; a one-off (no days, a date) only on its date;
    an entry with neither applies every day."""
    if item.days:
        return DAY_KEYS[day.weekday()] in item.days.split(",")
    return item.due is None or item.due == day.isoformat()


def is_one_off(item: Item) -> bool:
    return item.kind == "schedule" and not item.days and item.due is not None


def _subtract(slots: list[Slot], busy: Slot) -> list[Slot]:
    out = []
    for s in slots:
        if busy.end <= s.start or busy.start >= s.end:
            out.append(s)
            continue
        if busy.start > s.start:
            out.append(Slot(s.start, busy.start))
        if busy.end < s.end:
            out.append(Slot(busy.end, s.end))
    return out


def shape_of(
    day: date,
    schedule: list[Item],
    *,
    not_before: int | None = None,
    settings: PlanSettings,
) -> DayShape:
    """Work out one day. `not_before` (minutes) drops free time already past (for today)."""
    entries = [
        i for i in schedule if i.kind == "schedule" and i.status == "open" and applies_on(i, day)
    ]
    wake, bedtime = DEFAULT_WAKE, DEFAULT_BED
    fixed: list[Commitment] = []
    flexible: list[Commitment] = []
    routines: list[Item] = []
    for item in entries:
        dated = is_one_off(item)
        if item.anchor == WAKE:
            routines.append(item)
            continue
        start = to_min(item.start_time) if item.start_time else None
        end = to_min(item.end_time) if item.end_time else None
        if start is not None and end is not None and end < start:  # overnight: sleep
            bedtime, wake = start, end
            continue
        if start is None and end is None and dated:  # "Meeting at 9:45am"
            start = clock_in(item.title) or clock_in(item.details)
        if start is None and end is None:
            note = short_note(item.details)
            hint = clock_in(note)
            flexible.append(Commitment(f"fixed:{item.id}", item.title, None, None, note, hint))
            continue
        travel = item.travel_minutes or 0
        notes = []
        if travel:
            notes.append("incl. travel" if dated else "incl. commute")
        if end is None:
            end = start + ONE_OFF_MINUTES
            notes.append(LENGTH_GUESSED)
        start = start if start is not None else end
        note = " · ".join(notes) or short_note(item.details)
        commitment = Commitment(
            f"fixed:{item.id}",
            item.title,
            start - travel,
            end + travel,
            note,
            dated=dated,
            sport=is_sport(item.title),
        )
        fixed.append(commitment)
    fixed.sort(key=lambda c: c.start)

    free_from = wake
    first = fixed[0].start if fixed else None
    prep = settings.wake_prep
    if first is not None and first <= wake + prep:  # an early start: get up in time to leave
        wake = min(wake, first - prep)
        free_from = first  # the time before leaving is for getting ready
    at = wake
    for item in routines:  # the morning routine starts on waking, whenever that is
        length = item.minutes or settings.routine_minutes
        end, note = at + length, f"on waking, {length} min"
        if first is not None and first >= at and end > first:  # an early start: keep it short
            end, note = first, f"short today ({first - at} min): early start"
        if end > at:
            fixed.append(Commitment(f"fixed:{item.id}", item.title, at, end, note, routine=True))
            at = end
    free_from = max(free_from, at)
    fixed.sort(key=lambda c: c.start)
    last = bedtime - settings.wind_down
    free = [Slot(free_from, last)] if last > free_from else []
    for c in fixed:
        free = _subtract(free, Slot(c.start, c.end))
    if not_before is not None:
        free = [Slot(max(s.start, not_before), s.end) for s in free if s.end > not_before]
    free = [s for s in free if s.minutes >= MIN_SLOT]
    return DayShape(day, wake, bedtime, fixed, flexible, free, settings)


def hits(shape: DayShape, start: int, end: int) -> list[str]:
    """What a block from `start` to `end` runs into: fixed commitments (with travel), the
    time before waking, the wind-down. Empty if it sits in the day's open hours."""
    found = [c.title for c in shape.fixed if start < c.end and c.start < end]
    if start < shape.wake:
        found.append("before you're up")
    elif end > shape.wind_down:
        found.append("wind-down")
    elif not found and not any(s.start <= start and end <= s.end for s in shape.free):
        found.append("time to get ready")  # between waking and an early start
    return found


# --- the over-planning guard ---


@dataclass(frozen=True)
class Planned:
    title: str
    start: int
    end: int


def check_plan(shape: DayShape, blocks: list[Planned], budget: int | None = None) -> list[str]:
    """Problems with a timed plan for one day; an empty list means it fits. `budget`
    overrides the owner's fill cap (e.g. a Recovery focus allows less)."""
    budget = shape.budget if budget is None else budget
    problems = []
    ordered = sorted(blocks, key=lambda b: b.start)
    for b in ordered:
        if b.end <= b.start:
            problems.append(f"'{b.title}' ends before it starts")
        elif not any(s.start <= b.start and b.end <= s.end for s in shape.free):
            problems.append(
                f"'{b.title}' ({to_hhmm(b.start)}–{to_hhmm(b.end)}) isn't inside free time"
            )
    for a, b in zip(ordered, ordered[1:], strict=False):
        gap = shape.settings.gap
        if b.start < a.end + gap:
            problems.append(f"'{a.title}' and '{b.title}' need {gap} minutes between them")
    total = sum(max(0, b.end - b.start) for b in blocks)
    if total > budget:
        problems.append(
            f"{total} minutes planned, but at most {budget} fit "
            f"(the cap on {shape.free_minutes} free minutes)"
        )
    return problems
