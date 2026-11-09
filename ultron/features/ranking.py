"""Rank open to-dos so the planner and the nightly suggestions start from the right list.

score = (urgency + slipping + waiting) × focus weight of its area × question penalty

- urgency: overdue beats due today beats due this week; no due date counts a little
- slipping: each time the due date was pushed back adds weight (accountability)
- waiting: old to-dos with no date slowly rise, so nothing rots at the bottom forever
- focus: the current focus mode's weight for the to-do's area
- question: items with an open "(?)" question (e.g. "Still needed?") always come last
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from ultron.features.focus import Resolved
from ultron.features.items import Item, ItemStore

MOVED_CAP = 4
WAITING_PER_DAY, WAITING_CAP = 0.05, 2.0
QUESTION_PENALTY = 0.3


@dataclass(frozen=True)
class Ranked:
    item: Item
    score: float
    reasons: list[str] = field(default_factory=list)


def urgency(due: str | None, today: date) -> tuple[float, str | None]:
    if not due:
        return 1.0, None
    days = (date.fromisoformat(due) - today).days
    if days < 0:
        late = -days
        return 10 + min(late, 14) * 0.3, f"overdue {late} day{'s' * (late != 1)}"
    if days == 0:
        return 9.0, "due today"
    if days == 1:
        return 7.0, "due tomorrow"
    if days <= 3:
        return 5.0, f"due in {days} days"
    if days <= 7:
        return 3.0, "due this week"
    if days <= 14:
        return 1.5, None
    return 0.5, None


def score(item: Item, today: date, focus: Resolved) -> Ranked:
    base, why = urgency(item.due, today)
    if item.due_on and item.due > today.isoformat():
        base, why = 0.5, f"set for {date.fromisoformat(item.due):%a %d %b}, not before"
    reasons = [why] if why else []
    if item.times_moved:
        base += min(item.times_moved, MOVED_CAP)
        reasons.append(f"pushed back {item.times_moved}×")
    if not item.due:
        age = (today - datetime.fromisoformat(item.created_at).date()).days
        base += min(max(age, 0) * WAITING_PER_DAY, WAITING_CAP)
        if age >= 14:
            reasons.append(f"waiting {age} days")
    weight = focus.weights.get(item.area, 1.0)
    if weight > 1.0:
        reasons.append(f"{item.area} focus")
    total = base * weight
    if item.question:
        total *= QUESTION_PENALTY
        reasons.append("has an open question")
    return Ranked(item, round(total, 2), reasons)


def last_planned(store: ItemStore) -> dict[int, str]:
    """item id -> the latest day it had a plan block."""
    rows = store.db.execute(
        "SELECT item_id, MAX(day) FROM plan_blocks "
        "WHERE item_id IS NOT NULL AND status != 'dropped' GROUP BY item_id"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def rank(store: ItemStore, focus: Resolved, today: date, limit: int | None = None) -> list[Ranked]:
    """Open to-dos, best first. Anything with an open question comes after everything
    else (the owner hasn't confirmed it's still needed). Ties go to the earlier due date,
    then to what hasn't been planned for longest (never planned first), then the older
    item, so the same few never hog the top."""
    # Roadmap steps have their own daily time and order (roadmap.py), so they're not ranked
    tasks = [i for i in store.all() if i.kind == "task" and i.status == "open" and not i.is_step]
    planned = last_planned(store)
    ranked = [score(i, today, focus) for i in tasks]
    ranked.sort(
        key=lambda r: (
            bool(r.item.question),
            -r.score,
            r.item.due or "9999",
            planned.get(r.item.id, ""),
            r.item.id,
        )
    )
    return ranked[:limit] if limit else ranked
