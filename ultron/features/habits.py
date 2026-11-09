"""Habit ticks and streaks.

- Each day, the habits due that day show as tick boxes in ☑️ Today's habits on the Board.
  Ticking one records "kept today"; limits ("≤1h social media") are ticked as "kept to it".
- The boxes clear by themselves the next morning: a box is ticked only if it was kept today.
- A streak counts the habit's due days in a row it was kept, ending today (if kept) or
  yesterday, so it doesn't drop to zero just because today isn't ticked yet.
"""

from datetime import UTC, date, datetime, timedelta

from ultron.features.items import Item, ItemStore
from ultron.ops import activity

WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LOOKBACK = 400  # days of history a streak can span


def due_on(item: Item, day: date) -> bool:
    if item.kind != "habit" or (item.paused_until and day.isoformat() < item.paused_until):
        return False
    return not item.days or WEEK[day.weekday()] in item.days.split(",")


def kept_days(store: ItemStore, item_id: int) -> set[str]:
    rows = store.db.execute("SELECT day FROM habit_log WHERE item_id = ?", (item_id,)).fetchall()
    return {r[0] for r in rows}


def streak(item: Item, kept: set[str], today: date) -> int:
    day = today if today.isoformat() in kept else today - timedelta(days=1)
    count = 0
    for _ in range(LOOKBACK):
        if due_on(item, day):
            if day.isoformat() not in kept:
                break
            count += 1
        day -= timedelta(days=1)
    return count


def _update(store: ItemStore, item_id: int, day: date) -> Item:
    item = store.get(item_id)
    kept = kept_days(store, item_id)
    last = max(kept) if kept else None
    # Count from the latest day that matters, so back-filling an older tick is right too
    ref = max(day, date.fromisoformat(last)) if last else day
    return store.update(item_id, streak=streak(item, kept, ref), kept_on=last)


def keep(store: ItemStore, item_id: int, day: date) -> Item:
    added = store.db.execute(
        "INSERT OR IGNORE INTO habit_log (day, item_id, ticked_at) VALUES (?, ?, ?)",
        (day.isoformat(), item_id, datetime.now(UTC).isoformat()),
    ).rowcount
    item = _update(store, item_id, day)
    if added:
        activity.record("habit", f"Kept habit '{item.title}' on {day:%a %d %b}", db=store.db)
    return item


def unkeep(store: ItemStore, item_id: int, day: date) -> Item:
    removed = store.db.execute(
        "DELETE FROM habit_log WHERE day = ? AND item_id = ?", (day.isoformat(), item_id)
    ).rowcount
    item = _update(store, item_id, day)
    if removed:
        activity.record("habit", f"Unticked habit '{item.title}' on {day:%a %d %b}", db=store.db)
    return item


def refresh(store: ItemStore, today: date) -> int:
    """Recompute streaks (a missed day breaks one). Returns how many changed."""
    changed = 0
    for item in store.all():
        if item.kind != "habit" or item.status != "open":
            continue
        new = streak(item, kept_days(store, item.id), today)
        if new != item.streak:
            store.update(item.id, streak=new)
            changed += 1
    return changed


def today_status(store: ItemStore, today: date) -> tuple[list[Item], list[Item]]:
    """(kept, not yet kept) among the habits due today; for the evening review."""
    due = [i for i in store.all() if i.status == "open" and due_on(i, today)]
    kept = [i for i in due if i.kept_on == today.isoformat()]
    return kept, [i for i in due if i not in kept]
