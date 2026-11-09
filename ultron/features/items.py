"""Board items: the structured records behind what the owner sees on the 🗂 Board."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from ultron.config import local_today
from ultron.features.sorter import SortOutcome, parse_when
from ultron.ops import activity
from ultron.state import State

COLUMNS = (
    "id, kind, title, details, project, area, due, status, question, source_blocks, "
    "created_at, updated_at, done_at, times_moved, board_block_id, board_place, board_text, "
    "days, start_time, end_time, amount, source, source_key, email_id, thread_ids, subject_key, "
    "travel_minutes, streak, kept_on, paused_until, due_on, stage_id, step, link, anchor, minutes, "
    "board_note, dropped_at, log_hidden"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Item:
    id: int
    kind: str
    title: str
    details: str | None
    project: str | None
    area: str
    due: str | None  # YYYY-MM-DD
    status: str  # open | done | dropped
    question: str | None
    source_blocks: tuple[str, ...]
    created_at: str
    updated_at: str
    done_at: str | None
    times_moved: int
    board_block_id: str | None
    board_place: str | None
    board_text: str | None
    days: str | None = None  # schedule entries: "mon,tue,wed"
    start_time: str | None = None  # "HH:MM"
    end_time: str | None = None
    amount: str | None = None  # from email: "£120.00"
    source: str | None = None  # from email: sender shown on the Board
    source_key: str | None = None  # from email: sender domain
    email_id: int | None = None
    thread_ids: str | None = None  # from email: Message-IDs in its thread
    subject_key: str | None = None  # from email: normalised subject
    travel_minutes: int | None = None  # schedule: travel each way, e.g. a commute
    streak: int = 0  # habit: days in a row it was kept
    kept_on: str | None = None  # habit: last day it was kept (YYYY-MM-DD)
    paused_until: str | None = None  # habit: not due before this day (YYYY-MM-DD)
    due_on: bool = False  # due is the day it happens ("call GP Monday"), not a deadline
    stage_id: int | None = None  # a roadmap step: its stage (roadmap_stages.id)
    step: int | None = None  # roadmap step: order within its stage
    link: str | None = None  # a URL the Board links the title to
    anchor: str | None = None  # schedule: "wake" = starts when the owner wakes (a routine)
    minutes: int | None = None  # schedule: how long an anchored routine takes
    board_note: str | None = None  # the note line shown under the to-do on the Board now
    dropped_at: str | None = None  # when it was deleted or dropped
    log_hidden: bool = False  # kept out of 📓 Log (e.g. the owner deleted it there)

    @property
    def is_step(self) -> bool:
        """A roadmap step: shown in the roadmap's own Board section, not ✅ To-do."""
        return self.kind == "task" and self.stage_id is not None


@dataclass(frozen=True)
class Project:
    name: str
    area: str
    note: str | None


class ItemStore:
    def __init__(self, state: State) -> None:
        self.db = state.db

    def _row(self, r) -> Item:
        return Item(
            **{
                **dict(r),
                "source_blocks": tuple(json.loads(r["source_blocks"])),
                "due_on": bool(r["due_on"]),
                "log_hidden": bool(r["log_hidden"]),
            }
        )

    def stamp(self) -> str:
        """Changes whenever any item is added or updated (used to spot unsynced changes)."""
        row = self.db.execute("SELECT COUNT(*), MAX(updated_at) FROM items").fetchone()
        return f"{row[0]}:{row[1]}"

    def all(self, *, include_dropped: bool = False) -> list[Item]:
        where = "" if include_dropped else "WHERE status != 'dropped'"
        rows = self.db.execute(f"SELECT {COLUMNS} FROM items {where} ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def get(self, item_id: int) -> Item | None:
        r = self.db.execute(f"SELECT {COLUMNS} FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._row(r) if r else None

    def add(self, **fields) -> Item:
        now = _now()
        fields.setdefault("status", "open")
        if fields["status"] == "done":
            fields.setdefault("done_at", now)
        if fields["status"] == "dropped":
            fields.setdefault("dropped_at", now)
        fields["source_blocks"] = json.dumps(list(fields.get("source_blocks", ())))
        fields.update(created_at=now, updated_at=now)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.db.execute(
            f"INSERT INTO items ({cols}) VALUES ({marks})", tuple(fields.values())
        )
        item = self.get(cur.lastrowid)
        _log_added(self.db, item)
        return item

    def update(self, item_id: int, **fields) -> Item:
        if not fields:
            return self.get(item_id)
        if "source_blocks" in fields:
            fields["source_blocks"] = json.dumps(list(fields["source_blocks"]))
        fields["updated_at"] = _now()
        if "status" in fields:
            dropped = fields["status"] == "dropped"
            fields.setdefault("dropped_at", fields["updated_at"] if dropped else None)
        before = self.get(item_id)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE items SET {sets} WHERE id = ?", (*fields.values(), item_id))
        after = self.get(item_id)
        if before is not None and after is not None:
            _log_changed(self.db, before, after)
        return after

    def set_status(self, item_id: int, status: str) -> Item:
        return self.update(item_id, status=status, done_at=_now() if status == "done" else None)

    # --- projects ---

    def projects(self) -> list[Project]:
        rows = self.db.execute("SELECT name, area, note FROM projects ORDER BY name").fetchall()
        return [Project(r["name"], r["area"], r["note"]) for r in rows]

    def upsert_project(self, name: str, area: str, note: str | None) -> None:
        self.db.execute(
            """INSERT INTO projects (name, area, note) VALUES (?, ?, ?)
               ON CONFLICT (name) DO UPDATE SET area = excluded.area,
               note = COALESCE(excluded.note, projects.note)""",
            (name, area, note),
        )

    # --- board block keys (section and group headings) ---

    def block_keys(self) -> dict[str, str]:
        return {r[0]: r[1] for r in self.db.execute("SELECT key, block_id FROM board_blocks")}

    def set_block_key(self, key: str, block_id: str | None) -> None:
        if block_id is None:
            self.db.execute("DELETE FROM board_blocks WHERE key = ?", (key,))
        else:
            self.db.execute(
                "INSERT INTO board_blocks (key, block_id) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET block_id = excluded.block_id",
                (key, block_id),
            )


# --- the activity log ---

# Bookkeeping that changes as the Board is rendered or streaks are counted, not real changes
QUIET_FIELDS = {
    "id",
    "created_at",
    "updated_at",
    "done_at",
    "dropped_at",
    "board_block_id",
    "board_place",
    "board_text",
    "board_note",
    "source_blocks",
    "thread_ids",
    "streak",
    "kept_on",
}
KIND_NAMES = {"task": "to-do", "schedule": "schedule entry", "log": "log entry"}


def _name(item: Item) -> str:
    return f"{KIND_NAMES.get(item.kind, item.kind)} '{activity.one_line(item.title, 100)}'"


def _log_added(db, item: Item | None) -> None:
    if item is None:
        return
    fields = {
        k: v
        for k, v in item.__dict__.items()
        if k not in QUIET_FIELDS and v not in (None, False, 0, "", ())
    }
    verb = {"done": "Added (already done)", "dropped": "Added (dropped)"}.get(item.status, "Added")
    activity.record("item", f"{verb} {_name(item)}", detail={"id": item.id, **fields}, db=db)


def _log_changed(db, before: Item, after: Item) -> None:
    changes = {
        k: [v, after.__dict__[k]]
        for k, v in before.__dict__.items()
        if k not in QUIET_FIELDS and after.__dict__[k] != v
    }
    if not changes:
        return
    if "status" in changes:
        old, new = changes["status"]
        verb = {"done": "Ticked", "dropped": "Dropped"}.get(new, "Reopened" if old else "Changed")
    else:
        verb = "Changed"
    shown = ", ".join(
        f"{k} {activity.one_line(str(a), 40)} → {activity.one_line(str(b), 40)}"
        for k, (a, b) in changes.items()
        if k != "status"
    )
    summary = f"{verb} {_name(before)}" + (f": {shown}" if shown else "")
    activity.record("item", summary, detail={"id": after.id, "changes": changes}, db=db)


# --- one-off appointments ---


def close_past_appointments(store: ItemStore, today: date) -> list[Item]:
    """Tick off one-off appointments (schedule entries with a date and no days) whose day
    has passed, with a 📓 Log line so it isn't silent. done_at is the appointment's own
    day, so the Board doesn't keep showing it ticked today."""
    closed = []
    for item in store.all():
        if not (item.kind == "schedule" and item.status == "open" and not item.days):
            continue
        if not item.due or item.due >= today.isoformat():
            continue
        store.update(item.id, status="done", done_at=f"{item.due}T12:00:00+00:00")
        store.add(
            kind="log",
            title=f"Appointment passed: {item.title} ({date.fromisoformat(item.due):%a %d %b})",
            details=None,
            project=item.project,
            area=item.area,
            due=None,
            question=None,
        )
        closed.append(item)
    return closed


# --- sorter integration ---


@dataclass(frozen=True)
class BoardContext:
    lines: list[str]  # "[E#]" lines shown to the sorter
    ids: list[int]  # item id for each E# (E1 = ids[0])


RECENT_DONE_DAYS = 7  # ticked items the sorter still sees, so an update can reopen them


def board_context(store: ItemStore, today: date | None = None) -> BoardContext:
    """Open items (and recently ticked ones) as short lines for the sorter, so new notes
    merge or update instead of duplicating."""
    today = today or local_today()
    cutoff = (today - timedelta(days=RECENT_DONE_DAYS)).isoformat()
    shown = [
        i
        for i in store.all()
        if i.status == "open" or (i.status == "done" and (i.done_at or "") >= cutoff)
    ]
    lines = [
        f"{i.kind}: {i.title}"
        + (f" · {schedule_when(i)}" if i.kind == "schedule" and schedule_when(i) else "")
        + (f" (project: {i.project})" if i.project else "")
        + (f" · {'on' if i.due_on else 'due'} {i.due}" if i.due else "")
        + (" · done" if i.status == "done" else "")
        for i in shown
    ]
    return BoardContext(lines, [i.id for i in shown])


def schedule_when(item: Item) -> str:
    """A schedule entry's timing in the sorter's own format: 'thu,sun 07:00-18:00 +30m'."""
    times = "-".join(t for t in (item.start_time, item.end_time) if t)
    travel = f"+{item.travel_minutes}m" if item.travel_minutes else ""
    return " ".join(p for p in (item.days, times, travel) if p)


def is_weekly(item: Item) -> bool:
    return item.kind == "schedule" and bool(item.days)


def apply_sort(
    store: ItemStore,
    outcome: SortOutcome,
    context: BoardContext | None = None,
    merges: list["AppliedUpdate"] | None = None,
    today: date | None = None,
) -> list[Item]:
    """Save a sort's projects and items. Returns the items added or updated. Notes merged
    into an existing item are added to `merges`, so the owner can be told.

    A weekly 📌 schedule entry is never rewritten by a note: a one-off (or anything that
    isn't a weekly entry) becomes its own item, and a repeat of the entry only adds a
    dated note to it."""
    today = today or local_today()
    result = outcome.result
    for p in result.projects:
        store.upsert_project(p.name, p.area, p.note)
    changed: list[Item] = []
    for s in result.items:
        sources = tuple(outcome.line_blocks[n] for n in s.lines if n in outcome.line_blocks)
        existing = None
        if context and s.same_as and 1 <= s.same_as <= len(context.ids):
            existing = store.get(context.ids[s.same_as - 1])
        fields = dict(
            kind=s.kind,
            title=s.title,
            details=s.details,
            project=s.project,
            area=s.area,
            due=s.due,
            due_on=bool(s.due and s.on_day),
            question=s.question,
        )
        timing = parse_when(s.when) if s.kind == "schedule" else {}
        fields.update(timing)
        if existing is not None and is_weekly(existing):
            if s.kind != "schedule" or s.due or (timing and not timing.get("days")):
                existing = None  # e.g. "gym Friday": a one-off, not the weekly Workout
            else:
                stamp = f"{today:%d %b}: {s.title}" + (f" ({s.details})" if s.details else "")
                details = f"{existing.details}\n{stamp}" if existing.details else stamp
                item = store.update(
                    existing.id, details=details, source_blocks=existing.source_blocks + sources
                )
                if merges is not None:
                    merges.append(AppliedUpdate(item, s.title, ["note saved"]))
                changed.append(item)
                continue
        if existing is None:
            changed.append(store.add(**fields, status=s.status, source_blocks=sources))
            continue
        if existing.is_step:  # a roadmap step keeps its wording and place; a repeat adds notes
            fields.update(
                kind=existing.kind,
                title=existing.title,
                project=existing.project,
                area=existing.area,
            )
        if existing.due and s.due and s.due > existing.due:
            fields["times_moved"] = existing.times_moved + 1  # pushed back again
        fields["details"] = s.details or existing.details
        fields["source_blocks"] = existing.source_blocks + sources
        item = store.update(existing.id, **fields)
        if s.status == "done" and item.status != "done":
            item = store.set_status(item.id, "done")
        if merges is not None:
            renamed = [f"renamed from {existing.title!r}"] if item.title != existing.title else []
            merges.append(AppliedUpdate(item, s.title, ["merged a repeat", *renamed]))
        changed.append(item)
    return changed


# --- updates from notes ("spoke to 2 builders, meeting Fri") ---


@dataclass(frozen=True)
class AppliedUpdate:
    item: Item
    note: str
    changes: list[str]  # e.g. ["ticked", "due Fri 23 Oct"]


def apply_updates(
    store: ItemStore, outcome: SortOutcome, context: BoardContext, today: date
) -> list[AppliedUpdate]:
    """Apply progress notes to existing items: status and due date change as the note
    says, the note is kept in the item's details, and a dated 📓 Log line is added. The
    item's own wording is left alone."""
    applied: list[AppliedUpdate] = []
    for u in outcome.result.updates:
        if not 1 <= u.target <= len(context.ids):
            continue
        item = store.get(context.ids[u.target - 1])
        if item is None:
            continue
        changes: list[str] = []
        fields: dict = {}
        if u.due and u.due != item.due:
            try:
                new_due = date.fromisoformat(u.due)
            except ValueError:
                new_due = None
            if new_due:
                if item.due and u.due > item.due:
                    fields["times_moved"] = item.times_moved + 1
                fields["due"] = u.due
                fields["due_on"] = u.on_day
                changes.append(f"{'on' if u.on_day else 'due'} {new_due:%a %d %b}")
        timing = parse_when(u.when) if item.kind == "schedule" else {}
        if timing:
            fields.update(timing)
            changes.append(f"time now {schedule_when(replace(item, **timing))}")
        stamp = f"{today:%d %b}: {u.note.strip()}"
        fields["details"] = f"{item.details}\n{stamp}" if item.details else stamp
        sources = tuple(outcome.line_blocks[n] for n in u.lines if n in outcome.line_blocks)
        fields["source_blocks"] = item.source_blocks + sources
        item = store.update(item.id, **fields)
        if u.status and u.status != item.status:
            item = store.set_status(item.id, u.status)
            changes.append({"done": "ticked", "dropped": "dropped", "open": "reopened"}[u.status])
        changes.append("note saved")
        if u.follow_up and u.follow_up.strip():
            title = u.follow_up.strip()
            due = None
            try:
                due = date.fromisoformat(u.follow_up_due).isoformat() if u.follow_up_due else None
            except ValueError:
                pass
            open_titles = {
                i.title.casefold() for i in store.all() if i.kind == "task" and i.status == "open"
            }
            if title.casefold() not in open_titles:  # don't add it twice
                store.add(
                    kind="task",
                    title=title,
                    details=f"Follow-up from: {u.note.strip()}",
                    project=item.project,
                    area=item.area,
                    due=due,
                    due_on=bool(due and u.follow_up_on_day),
                    question=None,
                    source_blocks=sources,
                )
                word = "on" if u.follow_up_on_day else "due"
                when = f", {word} {date.fromisoformat(due):%a %d %b}" if due else ""
                changes.append(f'added follow-up "{title}"{when}')
        store.add(
            kind="log",
            title=f"{item.title}: {u.note.strip()}",
            details=None,
            project=item.project,
            area=item.area,
            due=None,
            question=None,
        )
        applied.append(AppliedUpdate(item, u.note.strip(), changes))
    return applied
