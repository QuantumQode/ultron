"""Schedule watch: after every sync, look for what an appointment added since the plan
was approved has broken, and ask the owner about it in Telegram, so a new appointment
never lands on top of an approved plan without a word.

- Clash: a planned block (not done yet) now runs into a fixed commitment. One message per
  day lists them, with [🔁 Replan <day>] / [Keep as is]. The blocks also show ⚠️ on 📅
  Schedule until they're moved.
- Tight: two fixed commitments overlap, or one starts as another ends, and one of them is
  a one-off ("Padel ends 08:30, Call GP starts 08:30"). Only the owner can sort that, so
  Ultron asks what the plan is.
- Unsized: a one-off with a start time but no end (its length is guessed) gets "How
  long?" with buttons, instead of a "(?)" line on the Board that's easy to miss.

Each finding is told once (remembered by key until its day has passed). No Claude calls.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ultron.features import profile
from ultron.features.freetime import (
    Commitment,
    DayShape,
    is_one_off,
    minutes_in,
    shape_of,
    to_hhmm,
    to_min,
)
from ultron.features.items import Item, ItemStore
from ultron.features.schedule import Block, PlanStore, clashes, fmt_minutes
from ultron.notify import Notifier, Space
from ultron.state import State

TOPIC = "watch"  # buttons: replan:<day>, keep:<day>, len:<id>:<minutes|other>
LENGTH_TOPIC = "watch_len:"  # + item id: a typed answer to "how long?"
NOTE_TOPIC = "watch_note"  # a reply to a "what's the plan?" question goes to chat
KEY_TOLD = "watch.told"  # JSON {finding key: its day}
LOOK_AHEAD = 7  # days ahead to ask about appointment lengths
LENGTHS = [[30, 60, 90], [120, 180]]  # the "how long?" buttons, in minutes
LENGTH_QUESTION = "How long, and travel each way?"  # the sorter's Board question


@dataclass(frozen=True)
class Clash:
    day: date
    block: Block
    hits: str  # what it runs into


@dataclass(frozen=True)
class Tight:
    day: date
    first: Commitment
    second: Commitment

    @property
    def overlap(self) -> bool:
        return self.second.start < self.first.end


def _shape(state: State, day: date) -> DayShape:
    return shape_of(day, ItemStore(state).all(), settings=profile.load(state))


def find_clashes(state: State, now: datetime) -> list[Clash]:
    """Today's (still to come) and tomorrow's planned blocks that hit the fixed schedule."""
    plans = PlanStore(state)
    found = []
    minute = now.hour * 60 + now.minute
    for day in (now.date(), now.date() + timedelta(days=1)):
        blocks = [
            b for b in plans.for_day(day) if day != now.date() or (b.end and to_min(b.end) > minute)
        ]
        for block_id, hit in clashes(_shape(state, day), blocks).items():
            found.append(Clash(day, next(b for b in blocks if b.id == block_id), hit))
    return found


def find_tight(state: State, now: datetime) -> list[Tight]:
    """Fixed commitments today and tomorrow that overlap or leave no gap, where one of
    them is a one-off (weekly ones were set up together and fit)."""
    found = []
    for day in (now.date(), now.date() + timedelta(days=1)):
        shape = _shape(state, day)
        fixed = [c for c in shape.fixed if not c.routine]
        for a, b in zip(fixed, fixed[1:], strict=False):
            if (a.dated or b.dated) and b.start < a.end + shape.settings.gap:
                found.append(Tight(day, a, b))
    return found


def unsized(state: State, now: datetime) -> list[Item]:
    """One-offs in the next week with a start but no end: their length is a guess."""
    last = (now.date() + timedelta(days=LOOK_AHEAD)).isoformat()
    return [
        i
        for i in ItemStore(state).all()
        if is_one_off(i)
        and i.status == "open"
        and now.date().isoformat() <= (i.due or "") <= last
        and i.start_time
        and not i.end_time
    ]


# --- what's been told ---


def _told(state: State, today: date) -> dict[str, str]:
    told = json.loads(state.get(KEY_TOLD) or "{}")
    return {k: d for k, d in told.items() if d >= today.isoformat()}  # forget past days


def _remember(state: State, told: dict[str, str]) -> None:
    state.set(KEY_TOLD, json.dumps(told))


# --- the messages ---


def clash_text(day: date, found: list[Clash], now: datetime, plan_time: time) -> str:
    lines = [f"⚠️ {day:%a %d %b}: your approved plan now clashes with your fixed schedule."]
    lines += [f"• {c.block.start}–{c.block.end} {c.block.title} ↔ {c.hits}" for c in found]
    if day == now.date() + timedelta(days=1) and now.time().replace(tzinfo=None) < plan_time:
        lines.append(
            f"Tonight's {plan_time:%H:%M} planning redoes {day:%a} anyway, so you can wait."
        )
    lines.append(f"Replan {day:%a} around them? (one Claude call, about $0.04)")
    return "\n".join(lines)


def clash_choices(day: date) -> list[tuple[str, str]]:
    iso = day.isoformat()
    return [(f"replan:{iso}", f"🔁 Replan {day:%a}"), (f"keep:{iso}", "Keep as is")]


def _short(title: str) -> str:
    """'Call GP: test results…' -> 'Call GP'."""
    return title.split(":", 1)[0].strip() if len(title) > 30 else title


def tight_text(t: Tight) -> str:
    a, b = t.first, t.second
    a_title, b_title = _short(a.title), _short(b.title)
    if t.overlap:
        what = (
            f"🔒 {a_title} ({to_hhmm(a.start)}–{to_hhmm(a.end)}) overlaps "
            f"🔒 {b_title} ({to_hhmm(b.start)}–{to_hhmm(b.end)})."
        )
    else:
        what = (
            f"🔒 {a_title} ends {to_hhmm(a.end)} and 🔒 {b_title} starts {to_hhmm(b.start)}, "
            "with no time in between."
        )
    return (
        f"⚠️ {t.day:%a %d %b}: {what} What's the plan? Reply here, e.g. what you'll do "
        f"instead or a new time for {b_title}."
    )


def length_text(item: Item) -> str:
    day = date.fromisoformat(item.due or "")
    return (
        f"⏱ How long is {item.title} ({day:%a %d %b}, from {item.start_time}), travel "
        f"included? I've guessed {fmt_minutes(60)} for now, so plans may land on it."
    )


def length_choices(item: Item) -> list[list[tuple[str, str]]]:
    rows = [[(f"len:{item.id}:{m}", fmt_minutes(m)) for m in row] for row in LENGTHS]
    rows[-1].append((f"len:{item.id}:other", "✏️ Other"))
    return rows


async def check(state: State, notifier: Notifier, now: datetime, plan_time: time) -> int:
    """Tell the owner about anything new; returns how many messages went out."""
    told = _told(state, now.date())
    sent = 0
    by_day: dict[date, list[Clash]] = {}
    for c in find_clashes(state, now):
        by_day.setdefault(c.day, []).append(c)
    for day, found in by_day.items():
        keys = {f"clash:{c.block.id}:{c.hits}" for c in found}
        if keys <= told.keys():
            continue
        await notifier.offer(
            clash_text(day, found, now, plan_time), TOPIC, clash_choices(day), Space.CHAT
        )
        told.update(dict.fromkeys(keys, day.isoformat()))
        sent += 1
    for t in find_tight(state, now):
        key = f"tight:{t.first.key}:{t.first.end}:{t.second.key}:{t.second.start}"
        if key in told:
            continue
        await notifier.ask(tight_text(t), NOTE_TOPIC, Space.CHAT, "What's the plan?")
        told[key] = t.day.isoformat()
        sent += 1
    for item in unsized(state, now):
        key = f"len:{item.id}"
        if key in told:
            continue
        await notifier.offer(length_text(item), TOPIC, length_choices(item), Space.CHAT)
        told[key] = item.due or now.date().isoformat()
        sent += 1
    _remember(state, told)
    return sent


# --- the owner's answers ---


class LengthError(ValueError):
    pass


def set_length(state: State, item_id: int, minutes: int) -> Item:
    """The appointment takes `minutes` from its start, travel included."""
    store = ItemStore(state)
    item = store.get(item_id)
    if item is None or not item.start_time:
        raise LengthError("That appointment is gone.")
    if not 5 <= minutes <= 16 * 60:
        raise LengthError("Give a length between 5 minutes and 16 hours, e.g. '1h 50m'.")
    end = min(to_min(item.start_time) + minutes, 23 * 60 + 59)
    question = None if item.question == LENGTH_QUESTION else item.question
    return store.update(item.id, end_time=to_hhmm(end), travel_minutes=None, question=question)


def length_reply(state: State, item_id: int, text: str) -> str:
    minutes = minutes_in(text)
    if minutes is None:
        return "I couldn't read a length there. Try '1h 50m' or '110'."
    try:
        item = set_length(state, item_id, minutes)
    except LengthError as exc:
        return str(exc)
    return f"Got it: {item.title} {item.start_time}–{item.end_time}."
