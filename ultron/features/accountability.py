"""Accountability extras: pattern call-outs, the midday check-in, and /easy.

- Pattern call-outs: a to-do pushed back 3+ times, a goal with nothing done towards it
  for 7 days, or a roadmap stage past its date (named by its next step) is named plainly
  with a choice: do it first thing tomorrow, drop it, or keep it as is. At most 2 an
  evening, and the same item at most once every 3 days. Items with an open "(?)"
  question are left alone until the owner answers it.
- Midday check-in (13:00): what's done so far and what's left, with [On track] [Replan].
- /easy: no call-outs or check-ins for the rest of the day. Briefs and reviews still come.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ultron.features import roadmap
from ultron.features.items import Item, ItemStore
from ultron.features.schedule import PlanStore, block_text
from ultron.notify import Notifier, Space
from ultron.state import State

CALLOUT_TOPIC = "callout"
CHECKIN_TOPIC = "checkin"
KEY_EASY = "accountability.easy_day"
KEY_CALLED = "accountability.called."  # + item id -> ISO date last called out
KEY_CHECKIN = "accountability.checkin_day"
SLIPS = 3
UNTOUCHED = timedelta(days=7)
COOLDOWN = timedelta(days=3)
PER_EVENING = 2
OBJECTIVE_PREFIX = "This week: "


# --- /easy ---


def easy(state: State, today: date) -> None:
    state.set(KEY_EASY, today.isoformat())


def is_easy(state: State, today: date) -> bool:
    return state.get(KEY_EASY) == today.isoformat()


# --- pattern call-outs ---


@dataclass(frozen=True)
class Callout:
    item: Item
    text: str


def _touched_recently(state: State, goal: Item, today: date) -> bool:
    since = (today - UNTOUCHED).isoformat()
    if goal.updated_at[:10] >= since or goal.created_at[:10] >= since:
        return True
    row = state.db.execute(
        "SELECT 1 FROM plan_blocks WHERE item_id = ? AND day >= ? AND status = 'done' LIMIT 1",
        (goal.id, since),
    ).fetchone()
    return row is not None


def callouts(state: State, today: date, limit: int = PER_EVENING) -> list[Callout]:
    """Who to call out tonight, worst first (respecting the cooldown and /easy)."""
    if is_easy(state, today):
        return []
    store = ItemStore(state)
    found: list[tuple[int, Callout]] = []
    v = roadmap.view(state, today)
    late = (
        {left[0].id: (stage, left) for stage, left in roadmap.overdue_stages(v, today)} if v else {}
    )
    for item in store.all():
        if item.status != "open" or item.question:
            continue
        last = state.get(f"{KEY_CALLED}{item.id}")
        if last and today - date.fromisoformat(last) < COOLDOWN:
            continue
        if item.id in late:  # the next step of a roadmap stage that's behind
            stage, left = late[item.id]
            n = len(left)
            text = (
                f'"{stage.title}" was due {stage.due:%a %d %b} and has {n} step'
                f'{"s" if n != 1 else ""} left, starting with "{item.title}". '
                "Do it first thing tomorrow, or drop it?"
            )
            found.append((1 + (today - stage.due).days // 7, Callout(item, text)))
        elif item.kind == "task" and item.times_moved >= SLIPS:
            text = (
                f'You\'ve pushed back "{item.title}" {item.times_moved} times. '
                "Do it first thing tomorrow, or drop it?"
            )
            found.append((item.times_moved, Callout(item, text)))
        elif (
            item.kind == "goal"
            and not item.title.startswith(OBJECTIVE_PREFIX)
            and "paused" not in item.title.casefold()
            and not _touched_recently(state, item, today)
        ):
            text = (
                f'Nothing done towards "{item.title}" in over a week. '
                "Plan a first step tomorrow, or drop the goal?"
            )
            found.append((1, Callout(item, text)))
    found.sort(key=lambda x: -x[0])
    return [c for _, c in found[:limit]]


def callout_choices(item: Item) -> list[tuple[str, str]]:
    first = "⏰ First thing tomorrow" if item.kind == "task" else "📅 Step tomorrow"
    return [
        (f"{item.id}:tomorrow", first),
        (f"{item.id}:drop", "🗑 Drop"),
        (f"{item.id}:keep", "Keep"),
    ]


async def send_callouts(state: State, notifier: Notifier, today: date) -> int:
    sent = 0
    for c in callouts(state, today):
        await notifier.offer(f"⚠️ {c.text}", CALLOUT_TOPIC, callout_choices(c.item), Space.BRIEFS)
        state.set(f"{KEY_CALLED}{c.item.id}", today.isoformat())
        sent += 1
    return sent


def answer_callout(state: State, key: str, today: date) -> str:
    item_id, _, choice = key.partition(":")
    store, plans = ItemStore(state), PlanStore(state)
    item = store.get(int(item_id))
    if item is None or item.status != "open":
        return "Already sorted."
    tomorrow = today + timedelta(days=1)
    if choice == "drop":
        store.set_status(item.id, "dropped")
        return f"Dropped: {item.title}"
    if choice == "tomorrow":
        if item.kind == "task":
            plans.add(tomorrow, f"First thing: {item.title}", item_id=item.id, source="callout")
            store.update(item.id, due=tomorrow.isoformat())
            return f"Tomorrow, first thing: {item.title}"
        plans.add(
            tomorrow,
            f"First step towards: {item.title}",
            item_id=item.id,
            minutes=30,
            source="callout",
        )
        return f"Tomorrow: a first step towards {item.title}"
    return "Kept as it is."


# --- midday check-in ---


def checkin_text(state: State, now: datetime) -> tuple[str, list[tuple[str, str]]]:
    today = now.date()
    blocks = [b for b in PlanStore(state).for_day(today) if b.status != "dropped"]
    hhmm = f"{now:%H:%M}"
    done = [b for b in blocks if b.status == "done"]
    ahead = [b for b in blocks if b.status == "planned" and (b.start or "99:99") >= hhmm]
    behind = [b for b in blocks if b.status == "planned" and (b.start or "99:99") < hhmm]
    lines = ["🕐 How's the day going?"]
    if not blocks:
        lines.append("No plan for today.")
        return "\n".join(lines), [("replan", "🔁 Plan the rest of today"), ("ok", "👍 Fine")]
    if done:
        lines.append("Done: " + ", ".join(b.title for b in done))
    if behind:
        lines.append("Not ticked yet: " + ", ".join(b.title for b in behind))
    if ahead:
        lines.append("Still to come: " + ", ".join(block_text(b) for b in ahead))
    return "\n".join(lines), [("ok", "👍 On track"), ("replan", "🔁 Replan")]


async def send_checkin(state: State, notifier: Notifier, now: datetime) -> bool:
    """Once a day, unless /easy is on."""
    today = now.date()
    if is_easy(state, today) or state.get(KEY_CHECKIN) == today.isoformat():
        return False
    state.set(KEY_CHECKIN, today.isoformat())
    text, choices = checkin_text(state, now)
    await notifier.offer(text, CHECKIN_TOPIC, choices, Space.BRIEFS)
    return True


def weekly_lines(state: State, today: date) -> list[str]:
    """Patterns for the weekly review (no cooldown, no buttons)."""
    store = ItemStore(state)
    lines = [
        f"{i.title}: pushed back {i.times_moved}×"
        for i in store.all()
        if i.kind == "task" and i.status == "open" and i.times_moved >= SLIPS and not i.question
    ]
    lines += [
        f"{i.title}: nothing done in over a week"
        for i in store.all()
        if i.kind == "goal"
        and i.status == "open"
        and not i.title.startswith(OBJECTIVE_PREFIX)
        and "paused" not in i.title.casefold()
        and not _touched_recently(state, i, today)
    ]
    return lines
