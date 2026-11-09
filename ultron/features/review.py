"""The evening review (21:00): close today, then hand over to planning tomorrow.

1. "How did today go?": each of today's commitments not yet ticked gets ✅ / ◐ / ❌. A
   roadmap step's block gets ✅ / ⏩ Progress / ❌ instead: a step can take several
   sessions, so ✅ finishes the step and ⏩ logs the session and keeps the step open. Steps
   whose session was ticked on the Schedule get one "Finished any?" question.
2. Habits due today that aren't ticked yet, one tap each to mark them kept.
3. For anything ◐ or ❌: Tomorrow / Shrink / Drop, then "what got in the way?". Nothing
   slips away silently: it's moved, shrunk to a first step, or dropped on purpose.
4. "One line on today?": the reflection. Then the 📓 Log entry is written and the
   planning message ("what moves the needle" + "your priorities?") follows.

If the review isn't finished by its close time (30 minutes after it starts), the day is
closed as it stands and planning goes ahead anyway. No Claude calls here.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ultron.features import accountability, daylog, focus, habits, nightly
from ultron.features.items import ItemStore
from ultron.features.schedule import Block, PlanStore
from ultron.notify import Notifier, Space
from ultron.state import State

TOPIC = "review"  # button taps
WHY_TOPIC = "review_why:"  # + block id: "what got in the way?" replies
REFLECT_TOPIC = "review_reflect"
KEY_SESSION = "review.session"
CLOSE_AFTER = timedelta(minutes=30)
SHRUNK_MINUTES = 15
SKIP_WORDS = {"skip", "no", "none", "nothing", "pass", "-"}
MAX_LABEL = 40  # each button has a full-width row; Telegram never wraps button text


@dataclass
class Session:
    day: str
    started_at: str
    reasons: dict[str, str] = field(default_factory=dict)  # block id -> what got in the way
    outcomes: dict[str, str] = field(default_factory=dict)  # block id -> tomorrow/shrink/drop
    reflection: str | None = None
    closed: bool = False


def load(state: State) -> Session | None:
    raw = state.get(KEY_SESSION)
    return Session(**json.loads(raw)) if raw else None


def _save(state: State, session: Session) -> None:
    state.set(KEY_SESSION, json.dumps(asdict(session)))


def _label(title: str, limit: int = MAX_LABEL) -> str:
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def habit_label(title: str) -> str:
    """Habit buttons have a row each: room for more, and "Morning routine · " isn't needed
    to tell them apart."""
    return _label(title.split(" · ", 1)[-1])


# --- the messages ---


def block_rows(blocks: list[Block], store: ItemStore | None = None) -> list[list[tuple[str, str]]]:
    """Two rows per block: the title alone (tap = done), so a phone shows it in full,
    then ◐ and ❌ under it (⏩ Progress instead of ◐ for a roadmap step)."""
    rows: list[list[tuple[str, str]]] = []
    for b in blocks:
        rows.append([(f"b:{b.id}:done", f"✅ {_label(b.title)}")])
        if _is_step_block(store, b):
            rows.append([(f"b:{b.id}:progress", "⏩ Progress"), (f"b:{b.id}:skipped", "❌ Missed")])
        else:
            rows.append([(f"b:{b.id}:partial", "◐ Partly"), (f"b:{b.id}:skipped", "❌ Missed")])
    return rows


def _is_step_block(store: ItemStore | None, block: Block) -> bool:
    item = store.get(block.item_id) if store and block.item_id else None
    return bool(item and item.is_step)


def finished_rows(state: State, day: date) -> list[list[tuple[str, str]]]:
    """Roadmap steps worked on today (their block ticked) that are still open."""
    store = ItemStore(state)
    seen: set[int] = set()
    rows = []
    for b in PlanStore(state).for_day(day):
        item = store.get(b.item_id) if b.item_id else None
        if b.status == "done" and item and item.is_step and item.status == "open":
            if item.id not in seen:
                seen.add(item.id)
                rows.append([(f"f:{item.id}", f"✅ {_label(item.title)}")])
    return rows + [[("f:none", "Not yet")]] if rows else []


def habit_rows(store: ItemStore, day: date) -> list[list[tuple[str, str]]]:
    _, missing = habits.today_status(store, day)
    rows = [[(f"h:{h.id}", f"✓ {habit_label(h.title)}")] for h in missing]
    return rows + [[("h:none", "None of the rest")]] if rows else []


def open_blocks(state: State, day: date) -> list[Block]:
    return [b for b in PlanStore(state).for_day(day) if b.status == "planned"]


async def start(state: State, notifier: Notifier, now: datetime) -> Session | None:
    """Send tonight's review. None if it has already started for today."""
    today = now.date()
    existing = load(state)
    if existing and existing.day == today.isoformat():
        return None
    session = Session(day=today.isoformat(), started_at=now.isoformat())
    _save(state, session)
    blocks = PlanStore(state).for_day(today)
    done = [b.title for b in blocks if b.status == "done"]
    pending = [b for b in blocks if b.status == "planned"]
    lines = [f"🌙 Today in review · {today:%a %d %b}"]
    if done:
        lines.append("Already done: " + ", ".join(done))
    if pending:
        lines.append("How did these go?")
        rows = block_rows(pending, ItemStore(state))
        await notifier.offer("\n".join(lines), TOPIC, rows, Space.BRIEFS)
    else:
        lines.append("Nothing left to mark from today's plan." if blocks else "No plan today.")
        await notifier.send("\n".join(lines), Space.BRIEFS)
    rows = finished_rows(state, today)
    if rows:
        await notifier.offer(
            "🚀 You worked on these today. Finished any of them?", TOPIC, rows, Space.BRIEFS
        )
    rows = habit_rows(ItemStore(state), today)
    if rows:
        await notifier.offer(
            "Habits not ticked yet. Tap the ones you kept:", TOPIC, rows, Space.BRIEFS
        )
    await accountability.send_callouts(state, notifier, today)
    await notifier.ask(
        'One line on today? Anything on your mind, or "skip". Then on to tomorrow.',
        REFLECT_TOPIC,
        Space.BRIEFS,
        "How was today?",
    )
    return session


# --- taps ---


@dataclass(frozen=True)
class Tap:
    ack: str  # short confirmation shown on the button tap
    rows: list | None  # the message's buttons after this tap ([] = remove them all)


async def tap(state: State, notifier: Notifier, key: str, now: datetime) -> Tap:
    session = load(state)
    if session is None:
        return Tap("This review has finished.", [])
    day = date.fromisoformat(session.day)
    plans, store = PlanStore(state), ItemStore(state)
    kind, _, rest = key.partition(":")

    if kind == "h":
        if rest == "none":
            return Tap("OK", [])
        item = habits.keep(store, int(rest), day)
        return Tap(f"Kept: {item.title}", habit_rows(store, day))

    if kind == "f":
        if rest == "none":
            return Tap("OK, they stay on the Board.", [])
        item = store.get(int(rest))
        if item and item.status == "open":
            store.set_status(item.id, "done")
        return Tap(
            f"✅ Finished: {item.title}" if item else "That one's gone.", finished_rows(state, day)
        )

    if kind == "b":
        block_id, _, outcome = rest.partition(":")
        block = plans.get(int(block_id))
        if block is None:
            return Tap("That one's gone.", block_rows(open_blocks(state, day), store))
        item = store.get(block.item_id) if block.item_id else None
        if outcome == "progress":  # a session on a roadmap step: done, the step carries on
            plans.set_status(block.id, "done")
            return Tap(
                f"⏩ Session logged: {block.title}. The step stays open.",
                block_rows(open_blocks(state, day), store),
            )
        plans.set_status(block.id, outcome)
        covered = filter(None, map(store.get, block.item_ids)) if outcome == "done" else []
        for each in covered:  # a batched block covers several to-dos
            if each.kind == "task" and each.status == "open":
                store.set_status(each.id, "done")
            if each.kind == "habit":
                habits.keep(store, each.id, date.fromisoformat(block.day))
        if outcome in ("partial", "skipped"):
            step = bool(item and item.is_step)
            await notifier.offer(
                f"{'◐' if outcome == 'partial' else '❌'} {block.title}: what now?",
                TOPIC,
                [
                    (f"m:{block.id}:tomorrow", "➡️ Tomorrow"),
                    (f"m:{block.id}:shrink", "✂️ Shrink"),
                    (f"m:{block.id}:drop", "🗑 Drop step" if step else "🗑 Drop"),
                ],
                Space.BRIEFS,
            )
        marks = {"done": "✅", "partial": "◐", "skipped": "❌"}
        return Tap(f"{marks[outcome]} {block.title}", block_rows(open_blocks(state, day), store))

    if kind == "m":
        block_id, _, choice = rest.partition(":")
        block = plans.get(int(block_id))
        if block is None:
            return Tap("That one's gone.", [])
        text = follow_up(state, block, choice, day)
        session.outcomes[block_id] = choice
        _save(state, session)
        await notifier.ask(
            f'What got in the way of "{block.title}"? One line, or "skip".',
            f"{WHY_TOPIC}{block.id}",
            Space.BRIEFS,
            "What got in the way?",
        )
        return Tap(text, [])

    return Tap("?", None)


def follow_up(state: State, block: Block, choice: str, day: date) -> str:
    """Tomorrow: carry it over. Shrink: tomorrow, as a 15-minute first step. Drop: let it go."""
    plans, store = PlanStore(state), ItemStore(state)
    item = store.get(block.item_id) if block.item_id else None
    tomorrow = day + timedelta(days=1)
    if choice == "drop":
        if item and item.kind == "task" and item.status == "open":
            store.set_status(item.id, "dropped")
        return f"Dropped: {block.title}"
    title = block.title if choice == "tomorrow" else f"{block.title}: first {SHRUNK_MINUTES} min"
    minutes = None if choice == "tomorrow" else SHRUNK_MINUTES
    plans.add(tomorrow, title, item_id=block.item_id, minutes=minutes, source="carry")
    if item and item.kind == "task" and item.status == "open":
        fields: dict[str, Any] = {"due": tomorrow.isoformat()}
        if item.due and tomorrow.isoformat() > item.due:
            fields["times_moved"] = item.times_moved + 1
        store.update(item.id, **fields)
    return f"{'Moved' if choice == 'tomorrow' else 'Shrunk'} to tomorrow: {title}"


# --- replies ---


def note_why(state: State, block_id: int, text: str) -> str:
    session = load(state)
    if session is None:
        return "Noted."
    reason = text.strip()
    if reason.casefold().rstrip(".!") not in SKIP_WORDS:
        session.reasons[str(block_id)] = reason
        _save(state, session)
    return "Noted."


def log_notes(state: State, session: Session) -> list[str]:
    plans = PlanStore(state)
    notes = []
    for block_id, outcome in session.outcomes.items():
        block = plans.get(int(block_id))
        if block is None:
            continue
        why = session.reasons.get(block_id)
        line = f"{block.title}: {outcome}"
        notes.append(line + (f" ({why})" if why else ""))
    for block_id, why in session.reasons.items():
        if block_id not in session.outcomes:
            block = plans.get(int(block_id))
            if block:
                notes.append(f"{block.title}: {why}")
    return ["In the way: " + "; ".join(notes)] if notes else []


async def finish(
    settings: Any,
    state: State,
    guard: Any,
    notifier: Notifier,
    make_llm: Any,
    *,
    reflection: str | None,
    now: datetime | None = None,
) -> bool:
    """Write the 📓 Log entry and move on to planning tomorrow. False if already done."""
    now = now or datetime.now(settings.zone)
    session = load(state)
    if session is None or session.closed:
        return False
    text = (reflection or "").strip()
    session.reflection = None if text.casefold().rstrip(".!") in SKIP_WORDS else text or None
    session.closed = True
    _save(state, session)
    day = date.fromisoformat(session.day)
    label = focus.current(state).label
    entry = daylog.close_day(
        state, day, label, session.reflection, log_notes(state, session), zone=settings.zone
    )
    await notifier.send(f"📓 Logged: {entry.title}", Space.BRIEFS)
    await nightly.run_nightly(settings, state, guard, notifier, make_llm, now)
    return True


def overdue(state: State, now: datetime) -> bool:
    """The review started a while ago and was never finished: close it and plan anyway."""
    session = load(state)
    if session is None or session.closed:
        return False
    return now - datetime.fromisoformat(session.started_at) >= CLOSE_AFTER


def due_now(state: State, now: datetime, at: Any, quiet_start: Any) -> bool:
    """Startup catch-up: past tonight's time, before quiet hours, and not started yet."""
    session = load(state)
    started = session is not None and session.day == now.date().isoformat()
    return not started and at <= now.time().replace(tzinfo=None) < quiet_start
