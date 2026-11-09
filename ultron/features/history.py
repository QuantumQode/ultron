"""What past plans say about the owner, for the planner and the evening suggestions.

- Track record: how planned blocks actually went over the last few weeks (✅ / ◐ / ❌
  from the evening review), by time of day, by length, and per to-do or habit. The
  planner uses it to size blocks from real outcomes instead of guessing every time (a
  30-minute block that keeps ending "partly" needs longer).
- Week capacity: the plannable time in the next 7 days against what's already spoken for
  (roadmap time, workouts, habits), so an overloaded week gets called out and something
  is dropped or pushed on purpose.

Pure reads, no Claude calls.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ultron.features import habits, roadmap
from ultron.features.freetime import minutes_in, to_min
from ultron.features.items import ItemStore
from ultron.features.schedule import WEEK_DAYS, Block, PlanStore, entry_id, fmt_minutes
from ultron.state import State

LOOK_BACK = 21  # days of reviewed blocks in the track record
PER_ITEM = 12  # to-dos and habits listed by name, most planned first
MIN_SAMPLES = 2  # an item needs this many past blocks to be worth a line
WORKOUT_MINUTES = 120  # a workout entry with no length in its note
OUTCOMES = ("done", "partial", "skipped")
PARTS = (("mornings", 0, 12 * 60), ("afternoons", 12 * 60, 17 * 60), ("evenings", 17 * 60, 24 * 60))
LENGTHS = (("≤30 min", 0, 30), ("31–60 min", 31, 60), ("over 60 min", 61, 24 * 60))


@dataclass
class Tally:
    done: int = 0
    partial: int = 0
    skipped: int = 0
    unticked: int = 0  # never reviewed

    def add(self, status: str) -> None:
        if status in OUTCOMES:
            setattr(self, status, getattr(self, status) + 1)
        else:
            self.unticked += 1

    @property
    def total(self) -> int:
        return self.done + self.partial + self.skipped + self.unticked

    def text(self) -> str:
        parts = [f"{self.done} done", f"{self.partial} partly", f"{self.skipped} missed"]
        if self.unticked:
            parts.append(f"{self.unticked} never ticked")
        return ", ".join(parts)

    def rate(self) -> str:
        return f"{round(100 * self.done / self.total)}% done" if self.total else "no data"


def _past_blocks(state: State, today: date, days: int) -> list[Block]:
    plans = PlanStore(state)
    out: list[Block] = []
    for n in range(days, 0, -1):
        out += [b for b in plans.for_day(today - timedelta(days=n)) if b.start and b.end]
    return out


def _length(b: Block) -> int:
    return to_min(b.end) - to_min(b.start)


def track_record(state: State, today: date, days: int = LOOK_BACK) -> list[str]:
    """Lines for the planner: how planned blocks went, overall and in detail."""
    blocks = _past_blocks(state, today, days)
    if not blocks:
        return []
    overall = Tally()
    by_part = {name: Tally() for name, *_ in PARTS}
    by_length = {name: Tally() for name, *_ in LENGTHS}
    by_item: dict[int, Tally] = defaultdict(Tally)
    lengths: dict[int, list[int]] = defaultdict(list)
    for b in blocks:
        overall.add(b.status)
        start, length = to_min(b.start), _length(b)
        for name, lo, hi in PARTS:
            if lo <= start < hi:
                by_part[name].add(b.status)
        for name, lo, hi in LENGTHS:
            if lo <= length <= hi:
                by_length[name].add(b.status)
        if b.item_id:
            by_item[b.item_id].add(b.status)
            lengths[b.item_id].append(length)
    lines = [f"Last {days} days: {overall.total} timed blocks: {overall.text()}."]
    lines.append(
        "By time of day: "
        + "; ".join(f"{name} {t.rate()} ({t.total})" for name, t in by_part.items() if t.total)
    )
    lines.append(
        "By length: "
        + "; ".join(
            f"{name} {t.rate()}, {t.partial} partly ({t.total})"
            for name, t in by_length.items()
            if t.total
        )
    )
    store = ItemStore(state)
    ranked = sorted(
        (i for i, t in by_item.items() if t.total >= MIN_SAMPLES), key=lambda i: -by_item[i].total
    )
    for item_id in ranked[:PER_ITEM]:
        item = store.get(item_id)
        if item is None:
            continue
        typical = sorted(lengths[item_id])[len(lengths[item_id]) // 2]
        lines.append(
            f"[#{item_id}] {item.title}: {by_item[item_id].total} blocks of ~"
            f"{fmt_minutes(typical)}: {by_item[item_id].text()}"
        )
    return lines


# --- week capacity ---


@dataclass(frozen=True)
class Capacity:
    plannable: int  # minutes the planner may fill in the next 7 days (fill cap applied)
    roadmap: int
    workouts: int
    habits: int
    due: int  # open to-dos due or ON a day in the window

    @property
    def left(self) -> int:
        return self.plannable - self.roadmap - self.workouts - self.habits

    def text(self) -> str:
        line = (
            f"Next {WEEK_DAYS} days: {fmt_minutes(self.plannable)} plannable. Spoken for: "
            f"roadmap {fmt_minutes(self.roadmap)}, workouts {fmt_minutes(self.workouts)}, "
            f"habits {fmt_minutes(self.habits)}. Left for to-dos: {fmt_minutes(self.left)}"
            f" ({self.due} to-dos are due or ON a day this week)."
        )
        if self.left < 0:
            line += " OVERLOADED: something has to give."
        return line


def habit_minutes(title: str, details: str | None) -> int:
    """Only a habit that says how long it takes ("Learn Spanish 30mins") needs planned
    time; "2,000 calories/day" or "take ashwagandha" don't."""
    return minutes_in(title) or minutes_in(details) or 0


def week_capacity(state: State, now: datetime, fill_cap: float) -> Capacity:
    """What the next 7 days can hold, and what's already spoken for."""
    from ultron.features.nightly import day_shape  # nightly imports this module

    store = ItemStore(state)
    items = [i for i in store.all() if i.status == "open"]
    road = roadmap.view(state, now.date())
    plannable = roadmap_need = workouts = habit_time = 0
    end = now.date() + timedelta(days=WEEK_DAYS - 1)
    for n in range(WEEK_DAYS):
        day = now.date() + timedelta(days=n)
        shape = day_shape(state, day, now)
        budget = round(shape.free_minutes * fill_cap)
        plannable += budget
        if road:
            roadmap_need += min(roadmap.need_on(state, road, day, now), budget)
        for c in shape.flexible:
            entry = store.get(entry_id(c.key) or 0)
            if entry and "workout" in entry.title.casefold():
                workouts += minutes_in(entry.details) or WORKOUT_MINUTES
        habit_time += sum(
            habit_minutes(i.title, i.details)
            for i in items
            if habits.due_on(i, day)
            and not i.title.startswith("Morning routine")  # inside the 🔒 routine
            and "wind-down" not in i.title.casefold()  # inside the wind-down
            and not (road and i.id in road.linked_ids())  # counted in roadmap time
        )
    due = sum(
        1
        for i in items
        if i.kind == "task" and i.due and i.due <= end.isoformat() and not i.is_step
    )
    return Capacity(plannable, roadmap_need, workouts, habit_time, due)
