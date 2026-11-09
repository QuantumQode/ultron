"""The 📅 Schedule page: today and tomorrow as timed blocks, the rest of the week listed.

- 🔒 lines are the fixed schedule (📌 My schedule on the Board); planned blocks are
  checkboxes. Ultron owns the page's layout; the owner ticks blocks.
- Today and tomorrow read top to bottom in time order. Anything without a time sits in
  one "Not timed yet" group just before wind-down, never above the day or below Sleep.
- The page is written in place: unchanged lines aren't touched, changed lines are
  updated, and a section is only rebuilt when its lines change. A sync with nothing new
  costs one Notion read.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ultron.features import habits, profile
from ultron.features.freetime import DayShape, hits, shape_of, to_hhmm, to_min
from ultron.features.items import ItemStore
from ultron.integrations.notion import block_text as notion_text
from ultron.integrations.notion import is_alive, list_children, rich_text
from ultron.ops import activity
from ultron.state import State

WEEK_DAYS = 7  # today + tomorrow + the next 5 days
KEY_LAYOUT = "schedule.layout"  # JSON: section -> {heading, title, lines: [[key, id, sig]]}
APPEND_LIMIT = 100
SECTIONS = ("today", "tomorrow", "rest")


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- what goes on the page ---


@dataclass(frozen=True)
class Fixed:
    """A 🔒 line from the fixed schedule."""

    key: str
    title: str
    start: str | None = None  # "HH:MM"
    end: str | None = None
    note: str | None = None  # e.g. "morning, about 2 hours total"
    hint: str | None = None  # flexible entries: "HH:MM" named in the note ("from about 18:30")
    dated: bool = False  # a one-off appointment on this date


@dataclass(frozen=True)
class Block:
    """A planned block (a row in plan_blocks)."""

    id: int
    day: str
    start: str | None
    end: str | None
    title: str
    item_id: int | None
    minutes: int | None
    status: str  # planned | done | partial | skipped | dropped
    source: str = "plan"  # plan | commit | carry | callout (see SOURCES)
    also_ids: tuple[int, ...] = ()  # a batched block: the other to-dos it covers

    @property
    def item_ids(self) -> list[int]:
        """Every to-do or habit the block stands for, its main one first."""
        return ([self.item_id] if self.item_id else []) + list(self.also_ids)


@dataclass
class DayView:
    day: date
    fixed: list[Fixed] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    free_minutes: int | None = None  # filled in by the free-time step
    clashes: dict[int, str] = field(default_factory=dict)  # block id -> what it runs into

    @property
    def planned_minutes(self) -> int:
        return sum(_length(b) for b in self.blocks if b.status != "dropped")


@dataclass
class ScheduleView:
    today: DayView
    tomorrow: DayView
    rest: list[DayView]


def _length(b: Block) -> int:
    if b.minutes:
        return b.minutes
    if b.start and b.end:
        return max(0, to_min(b.end) - to_min(b.start))
    return 0


DAILY = {"fixed:wake", "fixed:wind", "fixed:sleep"}  # on every day; left out of the week list
FIRST_THING = "First thing:"  # untimed blocks titled like this go straight after waking


def fixed_for(shape: DayShape) -> list[Fixed]:
    """🔒 lines for one day: wake, fixed commitments (with travel), flexible entries,
    wind-down and sleep."""
    lines = [Fixed("fixed:wake", "Wake", to_hhmm(shape.wake))]
    lines += [
        Fixed(c.key, c.title, to_hhmm(c.start), to_hhmm(c.end), c.note, dated=c.dated)
        for c in shape.fixed
    ]
    lines += [
        Fixed(c.key, c.title, note=c.note, hint=to_hhmm(c.preferred) if c.preferred else None)
        for c in shape.flexible
    ]
    note = shape.settings.wind_down_note or None  # the owner's words, e.g. "read"
    lines.append(Fixed("fixed:wind", "Wind down", to_hhmm(shape.wind_down), note=note))
    lines.append(Fixed("fixed:sleep", "Sleep", to_hhmm(shape.bedtime)))
    return lines


def entry_id(key: str) -> int | None:
    """'fixed:127' -> 127 (the schedule item); None for wake, wind-down and sleep."""
    tail = key.removeprefix("fixed:")
    return int(tail) if tail.isdigit() else None


def uncovered(fixed: list[Fixed], blocks: list["Block"]) -> list[Fixed]:
    """Drop a flexible entry (e.g. Workout) once a planned block that day stands for it."""
    covered = {i for b in blocks if b.status != "dropped" for i in b.item_ids}
    return [f for f in fixed if f.start or entry_id(f.key) not in covered]


# --- planned blocks ---

# Where a block came from. Only "plan" blocks are replaced when a new plan is approved;
# the rest are promises (chat commitments, review carry-overs, "First thing" call-outs)
# that the next plan must give a time, never silently drop.
SOURCES = ("plan", "commit", "carry", "callout")


class PlanStore:
    def __init__(self, state: State) -> None:
        self.db = state.db

    @classmethod
    def sharing(cls, store: ItemStore) -> "PlanStore":
        """A PlanStore on the same database connection as `store`."""
        plans = cls.__new__(cls)
        plans.db = store.db
        return plans

    def _row(self, r: Any) -> Block:
        return Block(
            id=r["id"],
            day=r["day"],
            start=r["start_time"],
            end=r["end_time"],
            title=r["title"],
            item_id=r["item_id"],
            minutes=r["minutes"],
            status=r["status"],
            source=r["source"],
            also_ids=tuple(json.loads(r["also_ids"])) if r["also_ids"] else (),
        )

    def add(
        self,
        day: date,
        title: str,
        *,
        start: str | None = None,
        end: str | None = None,
        item_id: int | None = None,
        minutes: int | None = None,
        source: str = "plan",
        also_ids: list[int] | None = None,
    ) -> Block:
        if source not in SOURCES:
            raise ValueError(f"Unknown block source {source!r}")
        now = _now()
        also = json.dumps(also_ids) if also_ids else None
        cur = self.db.execute(
            """INSERT INTO plan_blocks (day, start_time, end_time, title, item_id, minutes,
                   source, also_ids, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (day.isoformat(), start, end, title, item_id, minutes, source, also, now, now),
        )
        block = self.get(cur.lastrowid)
        when = f"{start}–{end}" if start else "untimed"
        activity.record(
            "plan",
            f"Planned '{title}' on {day:%a %d %b} {when} ({source})",
            detail={"block": block.id, "item": item_id, "minutes": minutes},
            db=self.db,
        )
        return block

    def get(self, block_id: int) -> Block | None:
        r = self.db.execute("SELECT * FROM plan_blocks WHERE id = ?", (block_id,)).fetchone()
        return self._row(r) if r else None

    def set_status(self, block_id: int, status: str) -> None:
        before = self.get(block_id)
        self.db.execute(
            "UPDATE plan_blocks SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), block_id),
        )
        if before is not None and before.status != status:
            activity.record(
                "plan",
                f"Plan block '{before.title}' on {before.day}: {before.status} → {status}",
                detail={"block": block_id},
                db=self.db,
            )

    def replace_planned(self, day: date, *, timed: bool, from_time: str | None = None) -> int:
        """Drop a day's not-yet-done blocks (timed or untimed) from earlier plans before a
        new plan replaces them, so replanning never duplicates. Done blocks, and blocks
        that didn't come from a plan (commitments, carry-overs), are kept. With
        `from_time` (a replan of the rest of today), earlier unticked blocks stay as
        misses."""
        null = "IS NOT NULL" if timed else "IS NULL"
        later = " AND start_time >= ?" if from_time else ""
        params = (_now(), day.isoformat()) + ((from_time,) if from_time else ())
        dropped = self.db.execute(
            f"UPDATE plan_blocks SET status = 'dropped', updated_at = ? "
            f"WHERE day = ? AND status = 'planned' AND source = 'plan' "
            f"AND start_time {null}{later}",
            params,
        ).rowcount
        if dropped:
            kind = "timed" if timed else "untimed"
            activity.record(
                "plan", f"Replaced {dropped} planned {kind} block(s) on {day:%a %d %b}", db=self.db
            )
        return dropped

    def rename(self, block_id: int, title: str) -> None:
        before = self.get(block_id)
        self.db.execute(
            "UPDATE plan_blocks SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), block_id),
        )
        if before is not None and before.title != title:
            activity.record(
                "plan",
                f"Renamed plan block '{before.title}' → '{title}'",
                detail={"block": block_id},
                db=self.db,
            )

    def for_day(self, day: date) -> list[Block]:
        rows = self.db.execute(
            "SELECT * FROM plan_blocks WHERE day = ? AND status != 'dropped' "
            "ORDER BY start_time IS NULL, start_time, id",
            (day.isoformat(),),
        ).fetchall()
        return [self._row(r) for r in rows]


def clashes(shape: DayShape, blocks: list[Block]) -> dict[int, str]:
    """Planned blocks (not yet done) that now run into the fixed schedule, e.g. an
    appointment added after the plan was approved: block id -> what it runs into."""
    found = {}
    for b in blocks:
        if b.status != "planned" or not (b.start and b.end):
            continue
        hit = hits(shape, to_min(b.start), to_min(b.end))
        if hit:
            found[b.id] = ", ".join(hit)
    return found


def build_view(state: State, today: date) -> ScheduleView:
    schedule = [i for i in ItemStore(state).all() if i.kind == "schedule"]
    plans = PlanStore(state)
    settings = profile.load(state)

    def view(day: date, timed: bool = True) -> DayView:
        shape = shape_of(day, schedule, settings=settings)
        free = shape.free_minutes if timed else None
        blocks = plans.for_day(day)
        found = clashes(shape, blocks) if timed else {}
        return DayView(day, uncovered(fixed_for(shape), blocks), blocks, free, found)

    rest = [view(today + timedelta(days=n), timed=False) for n in range(2, WEEK_DAYS)]
    return ScheduleView(view(today), view(today + timedelta(days=1)), rest)


# --- rendering ---


@dataclass(frozen=True)
class Line:
    key: str  # stable identity, e.g. "block:12", "fixed:123:end"
    block: dict[str, Any]  # Notion block to create
    sig: str  # what it shows; a changed sig means the block needs updating


def fmt_minutes(minutes: int) -> str:
    h, m = divmod(max(0, minutes), 60)
    if h and m:
        return f"{h}h {m:02d}m"
    return f"{h}h" if h else f"{m}m"


def heading_title(section: str, day: date | None = None) -> str:
    if section == "rest":
        return "Rest of the week"
    return f"{'Today' if section == 'today' else 'Tomorrow'} · {day:%a %d %b}"


def heading_block(section: str, title: str) -> dict[str, Any]:
    body: dict[str, Any] = {"rich_text": rich_text(title)}
    if section == "rest":
        body["is_toggleable"] = True
    return {"type": "heading_2", "heading_2": body}


def _paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": rich_text(text)}}


def fixed_text(f: Fixed) -> str:
    if f.start and f.end:
        when = f"{f.start}–{f.end} "
    elif f.start:
        when = f"{f.start} "
    elif f.hint:
        when = f"~{f.hint} "
    else:
        when = ""
    return f"🔒 {when}{f.title}" + (f" · {f.note}" if f.note else "")


STATUS_MARKS = {"partial": "◐ partly done", "skipped": "❌ skipped"}
CLASH_MARK = "⚠️ clashes with"


def block_text(b: Block, clash: str | None = None) -> str:
    when = f"{b.start}–{b.end} " if b.start and b.end else ""
    length = _length(b)
    text = f"{when}{b.title}" + (f" (~{fmt_minutes(length)})" if length else "")
    if clash:
        text += f" · {CLASH_MARK} {clash}"
    return text + (f" · {STATUS_MARKS[b.status]}" if b.status in STATUS_MARKS else "")


def day_lines(view: DayView) -> list[Line]:
    """Today or tomorrow: the whole day in time order, Wake first and Sleep last, with
    anything untimed grouped just before wind-down."""
    lines: list[Line] = []
    if view.free_minutes:
        planned = view.planned_minutes
        text = f"Free {fmt_minutes(view.free_minutes)}"
        if planned:
            pct = round(100 * planned / view.free_minutes)
            text += f" · planned {fmt_minutes(planned)} ({pct}%)"
        lines.append(Line("free", _paragraph(text), text))
    if not view.blocks:
        text = "Nothing planned yet."
        lines.append(Line("empty", _paragraph(text), text))
    wake = next((f.start for f in view.fixed if f.key == "fixed:wake"), "00:00")
    timed: list[tuple[str, int, Line]] = []
    untimed: list[Line] = []
    for n, f in enumerate(view.fixed):
        text = fixed_text(f)
        line = Line(f.key, _paragraph(text), text)
        when = f.start or f.hint
        if when is None:
            untimed.append(line)
            continue
        order = -2 if f.key == "fixed:wake" else n  # wake up before anything else
        timed.append((when, order, line))
    for n, b in enumerate(view.blocks, start=len(view.fixed)):
        text = block_text(b, view.clashes.get(b.id))
        done = b.status == "done"
        block = {"type": "to_do", "to_do": {"rich_text": rich_text(text), "checked": done}}
        line = Line(f"block:{b.id}", block, text + " ✓" * done)
        if b.start:
            timed.append((b.start, n, line))
        elif b.title.startswith(FIRST_THING):
            timed.append((wake, -1, line))  # straight after waking
        else:
            untimed.append(line)
    ordered = [line for *_, line in sorted(timed, key=lambda t: (t[0], t[1]))]
    if untimed:
        text = "Not timed yet:"
        group = [Line("untimed", _paragraph(text), text), *untimed]
        at = next(
            (i for i, line in enumerate(ordered) if line.key in ("fixed:wind", "fixed:sleep")),
            len(ordered),
        )
        ordered[at:at] = group
    return lines + ordered


def rest_lines(days: list[DayView]) -> list[Line]:
    """Later days: one line each, no times, so the week stays flexible. One-off
    appointments keep their time."""
    lines = []
    for view in days:
        parts = [
            f"🔒 {f.start + ' ' if f.dated and f.start else ''}{f.title}"
            for f in view.fixed
            if f.key not in DAILY
        ]
        parts += [b.title for b in view.blocks]
        text = f"{view.day:%a %d} · " + (" · ".join(parts) if parts else "nothing planned")
        lines.append(Line(f"day:{view.day.isoformat()}", _paragraph(text), text))
    return lines


def render(view: ScheduleView) -> dict[str, tuple[str, list[Line]]]:
    """section -> (heading title, lines)."""
    return {
        "today": (heading_title("today", view.today.day), day_lines(view.today)),
        "tomorrow": (heading_title("tomorrow", view.tomorrow.day), day_lines(view.tomorrow)),
        "rest": (heading_title("rest"), rest_lines(view.rest)),
    }


# --- writing ---


@dataclass
class ScheduleWrite:
    created: int = 0
    updated: int = 0
    removed: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.removed)


def _insert(client: Any, parent: str, blocks: list[dict], after: str | None) -> list[str]:
    ids: list[str] = []
    for start in range(0, len(blocks), APPEND_LIMIT):
        chunk = blocks[start : start + APPEND_LIMIT]
        kwargs: dict[str, Any] = {"block_id": parent, "children": chunk}
        if after:
            kwargs["position"] = {"type": "after_block", "after_block": {"id": after}}
        results = client.blocks.children.append(**kwargs)["results"]
        # Inserting after a block, Notion also returns every following sibling: keep only ours
        ids += [b["id"] for b in results[: len(chunk)]]
        after = ids[-1]
    return ids


def write_schedule(
    client: Any, state: State, page_id: str, hint_id: str | None, view: ScheduleView
) -> ScheduleWrite:
    """Make the page match `view`, touching only what changed."""
    result = ScheduleWrite()
    wanted = render(view)
    layout: dict[str, Any] = json.loads(state.get(KEY_LAYOUT) or "{}")
    alive = {b["id"] for b in list_children(client, page_id) if not b.get("archived")}

    if not all(layout.get(s, {}).get("heading") in alive for s in SECTIONS):
        # First run, or the owner deleted a heading: rebuild the page's sections
        for section in layout.values():
            for block_id in [
                section.get("heading"),
                *(entry[1] for entry in section.get("lines", [])),
            ]:
                if block_id in alive:
                    client.blocks.delete(block_id=block_id)
                    result.removed += 1
        after = hint_id if hint_id in alive else None
        heads = [heading_block(s, wanted[s][0]) for s in SECTIONS]
        ids = _insert(client, page_id, heads, after)
        result.created += len(ids)
        layout = {
            s: {"heading": i, "title": wanted[s][0], "lines": []}
            for s, i in zip(SECTIONS, ids, strict=True)
        }

    for section in SECTIONS:
        title, lines = wanted[section]
        stored = layout[section]
        heading = stored["heading"]
        if stored["title"] != title:
            client.blocks.update(block_id=heading, heading_2={"rich_text": rich_text(title)})
            stored["title"] = title
            result.updated += 1
        old = stored["lines"]
        same_keys = [k for k, _, _ in old] == [line.key for line in lines]
        # Lines on the page itself must still exist (the owner may have deleted one)
        intact = section == "rest" or all(entry[1] in alive for entry in old)
        if same_keys and intact:
            for entry, line in zip(old, lines, strict=True):
                if entry[2] != line.sig:
                    kind = line.block["type"]
                    client.blocks.update(block_id=entry[1], **{kind: line.block[kind]})
                    entry[2] = line.sig
                    result.updated += 1
            continue
        for _, block_id, _ in old:
            if section == "rest" or block_id in alive:
                client.blocks.delete(block_id=block_id)
                result.removed += 1
        if section == "rest":  # lines live inside the collapsible heading
            ids = _insert(client, heading, [line.block for line in lines], None)
        else:
            ids = _insert(client, page_id, [line.block for line in lines], heading)
        result.created += len(ids)
        stored["lines"] = [[line.key, i, line.sig] for line, i in zip(lines, ids, strict=True)]

    state.set(KEY_LAYOUT, json.dumps(layout))
    return result


# --- reading the owner's edits back (two-way) ---

TIMES_PREFIX = re.compile(r"^\d{2}:\d{2}\s*[–-]\s*\d{2}:\d{2}\s+")
LENGTH_SUFFIX = re.compile(r"\s*\(~[^)]*\)\s*$")
MARK_SUFFIX = re.compile(r"\s*·\s*(◐ partly done|❌ skipped)\s*$")
CLASH_SUFFIX = re.compile(r"\s*·\s*⚠️ clashes with .*$")


@dataclass
class ScheduleEdits:
    ticked: list[int] = field(default_factory=list)  # plan block ids
    unticked: list[int] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    renamed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.ticked or self.unticked or self.deleted or self.renamed)


def title_from(text: str) -> str:
    """'09:00–10:00 Call builders (~1h)' -> 'Call builders'."""
    text = CLASH_SUFFIX.sub("", MARK_SUFFIX.sub("", text.strip()))
    return LENGTH_SUFFIX.sub("", TIMES_PREFIX.sub("", text)).strip()


def read_edits(client: Any, state: State, page_id: str) -> ScheduleEdits:
    """Compare today's and tomorrow's blocks on the page with what Ultron wrote: ticks,
    unticks, deletions and rewording. If a heading is gone the page is being rebuilt, so
    nothing is read as deleted."""
    edits = ScheduleEdits()
    layout: dict[str, Any] = json.loads(state.get(KEY_LAYOUT) or "{}")
    if not layout:
        return edits
    children = {b["id"]: b for b in list_children(client, page_id) if is_alive(b)}
    if any(layout.get(s, {}).get("heading") not in children for s in ("today", "tomorrow")):
        return edits
    plans = PlanStore(state)
    for section in ("today", "tomorrow"):
        for key, block_id, sig in layout[section]["lines"]:
            if not key.startswith("block:"):
                continue
            block = plans.get(int(key.split(":", 1)[1]))
            if block is None or block.status == "dropped":
                continue
            on_page = children.get(block_id)
            if on_page is None:
                edits.deleted.append(block.id)
                continue
            checked = bool(on_page.get("to_do", {}).get("checked"))
            if checked and block.status != "done":
                edits.ticked.append(block.id)
            elif not checked and block.status == "done":
                edits.unticked.append(block.id)
            text = notion_text(on_page)
            if text != sig.removesuffix(" ✓"):
                title = title_from(text)
                if title and title != block.title:
                    edits.renamed.append((block.id, title))
    return edits


def apply_edits(state: State, edits: ScheduleEdits) -> None:
    """Owner edits win. Ticking a block also ticks the Board to-dos it came from (all of
    them, for a batched block), or keeps the habit it stands for that day. A roadmap step
    isn't ticked: the block was one session, and a step can take several (it's ticked on
    the Board, or at the review)."""
    plans, store = PlanStore(state), ItemStore(state)
    for block_id in edits.ticked:
        plans.set_status(block_id, "done")
        block = plans.get(block_id)
        for item in filter(None, map(store.get, block.item_ids)):
            if item.status == "open" and item.kind == "task" and not item.is_step:
                store.set_status(item.id, "done")
            if item.kind == "habit":
                habits.keep(store, item.id, date.fromisoformat(block.day))
    for block_id in edits.unticked:
        plans.set_status(block_id, "planned")
        block = plans.get(block_id)
        for item in filter(None, map(store.get, block.item_ids)):
            if item.status == "done" and item.kind == "task" and not item.is_step:
                store.set_status(item.id, "open")
            if item.kind == "habit":
                habits.unkeep(store, item.id, date.fromisoformat(block.day))
    for block_id in edits.deleted:
        plans.set_status(block_id, "dropped")
    for block_id, title in edits.renamed:
        plans.rename(block_id, title)


def follow_board(state: State, day: date) -> int:
    """To-dos ticked on the Board (or in chat) tick their planned block too, once every
    to-do a batched block covers is done."""
    plans, store = PlanStore(state), ItemStore(state)
    moved = 0
    for block in plans.for_day(day):
        items = [store.get(i) for i in block.item_ids]
        if block.status == "planned" and items and all(i and i.status == "done" for i in items):
            plans.set_status(block.id, "done")
            moved += 1
    return moved
