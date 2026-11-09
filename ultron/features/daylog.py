"""📓 Log: one entry per day, written when the day is reviewed.

The entry is a Board item (kind "log") so it shows in 📓 Log & journal as a collapsed
line: "21 Oct · Day review: 3 of 5 done, 1 partly, 1 skipped · Balanced". Inside it: what
was done, partly done, skipped or not reached, anything else ticked off that day, and the
owner's reflection.

Commitments are the day's plan blocks: approved plan blocks, and anything the owner says
they'll do in chat (which also becomes a to-do). The review checks each one by name.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ultron.features.items import Item, ItemStore
from ultron.features.schedule import Block, PlanStore
from ultron.state import State

KEY_PREFIX = "daylog."  # daylog.<YYYY-MM-DD> -> item id of that day's entry
ZONE = ZoneInfo("Europe/London")


@dataclass
class DayTally:
    day: date
    done: list[Block] = field(default_factory=list)
    partial: list[Block] = field(default_factory=list)
    skipped: list[Block] = field(default_factory=list)
    open: list[Block] = field(default_factory=list)  # not marked either way
    also_done: list[str] = field(default_factory=list)  # to-dos ticked that weren't planned

    @property
    def total(self) -> int:
        return len(self.done) + len(self.partial) + len(self.skipped) + len(self.open)

    @property
    def headline(self) -> str:
        if not self.total:
            return "nothing was planned"
        parts = [f"{len(self.done)} of {self.total} done"]
        if self.partial:
            parts.append(f"{len(self.partial)} partly")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.open:
            parts.append(f"{len(self.open)} not reached")
        return ", ".join(parts)


def commitments(state: State, day: date) -> list[Block]:
    """Everything committed to for `day`: its plan blocks, timed or not."""
    return PlanStore(state).for_day(day)


def tally(state: State, day: date, zone: ZoneInfo = ZONE) -> DayTally:
    t = DayTally(day)
    blocks = commitments(state, day)
    for b in blocks:
        {"done": t.done, "partial": t.partial, "skipped": t.skipped}.get(b.status, t.open).append(b)
    planned = {b.item_id for b in blocks}
    t.also_done = [
        i.title
        for i in ItemStore(state).all()
        if i.kind == "task"
        and i.status == "done"
        and i.done_at
        and datetime.fromisoformat(i.done_at).astimezone(zone).date() == day
        and i.id not in planned
    ]
    return t


def entry_text(
    t: DayTally, focus_label: str, reflection: str | None, notes: list[str] | None = None
) -> tuple[str, str]:
    """(title, details) for the Board entry."""
    title = f"Day review: {t.headline} · {focus_label}"
    if reflection:
        title += " · reflection noted"
    lines = []
    for label, blocks in (
        ("Done", t.done),
        ("Partly done", t.partial),
        ("Skipped", t.skipped),
        ("Not reached", t.open),
    ):
        if blocks:
            lines.append(f"{label}: " + ", ".join(b.title for b in blocks))
    if t.also_done:
        lines.append("Also done: " + ", ".join(t.also_done))
    lines += notes or []
    if reflection:
        lines.append(f"Reflection: {reflection.strip()}")
    return title, "\n".join(lines) or "Nothing was planned."


def close_day(
    state: State,
    day: date,
    focus_label: str,
    reflection: str | None = None,
    notes: list[str] | None = None,
    zone: ZoneInfo = ZONE,
) -> Item:
    """Write (or rewrite) the day's 📓 Log entry from its commitments. `notes` are extra
    lines, e.g. what got in the way of something skipped."""
    store = ItemStore(state)
    title, details = entry_text(tally(state, day, zone), focus_label, reflection, notes)
    key = f"{KEY_PREFIX}{day.isoformat()}"
    raw = state.get(key)
    existing = store.get(int(raw)) if raw else None
    if existing and existing.status != "dropped":
        return store.update(existing.id, title=title, details=details)
    item = store.add(
        kind="log",
        title=title,
        details=details,
        project=None,
        area="Admin",
        due=None,
        question=None,
    )
    # Date the entry by the day it's about (the review may run after midnight)
    stamp = datetime.combine(day, time(12, 0)).isoformat() + "+00:00"
    item = store.update(item.id, created_at=stamp)
    state.set(key, str(item.id))
    return item


SLIPPED = ("Partly done:", "Skipped:", "Not reached:")
NOT_SLIPS = ("Done:", "Also done:", "Reflection:")


def recent_slips(state: State, before: date, days: int = 3) -> list[str]:
    """What slipped on the last few days, and why, from their 📓 Log entries:
    ['Wed 21: Skipped: Send applications', 'Wed 21: Send applications: skipped (too
    tired)']. Reflections and what got done are left out."""
    store = ItemStore(state)
    lines = []
    for n in range(days, 0, -1):
        day = before - timedelta(days=n)
        raw = state.get(f"{KEY_PREFIX}{day.isoformat()}")
        item = store.get(int(raw)) if raw else None
        if not item or not item.details:
            continue
        for line in item.details.splitlines():
            why = "(" in line and not line.startswith(NOT_SLIPS)  # "X: skipped (too tired)"
            if line.startswith(SLIPPED) or why:
                lines.append(f"{day:%a %d}: {line}")
    return lines


def reflection_of(state: State, day: date) -> str | None:
    raw = state.get(f"{KEY_PREFIX}{day.isoformat()}")
    item = ItemStore(state).get(int(raw)) if raw else None
    if not item or not item.details:
        return None
    for line in item.details.splitlines():
        if line.startswith("Reflection: "):
            return line.removeprefix("Reflection: ")
    return None


def commit(
    state: State,
    title: str,
    day: date,
    *,
    area: str = "Admin",
    project: str | None = None,
) -> tuple[Item, Block]:
    """The owner said "I'll do X" (on `day`): a to-do due that day, plus a commitment
    (an untimed block on that day's Schedule) that the evening review will check."""
    return commit_to(ItemStore(state), PlanStore(state), title, day, area=area, project=project)


def commit_to(
    store: ItemStore,
    plans: PlanStore,
    title: str,
    day: date,
    *,
    area: str = "Admin",
    project: str | None = None,
) -> tuple[Item, Block]:
    item = store.add(
        kind="task",
        title=title.strip(),
        details=None,
        project=project,
        area=area,
        due=day.isoformat(),
        question=None,
    )
    block = plans.add(day, item.title, item_id=item.id, source="commit")
    return item, block
