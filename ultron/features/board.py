"""Render items onto the 🗂 Board and read the owner's edits back.

Always-open sections (To-do) keep a heading followed by their blocks at page level;
collapsible sections are toggle headings with their items inside. 📓 Log & journal holds a
folded heading per week, newest first: the to-dos done that week (ticked), those dropped
(struck through), then the week's day reviews and journal entries. A roadmap (roadmap.py)
gets its own folded section: inside it, a status box, stage headings for the phases under
way, and a folded toggle for each later phase with its steps inside. Ultron updates
the Board in place: it adds, edits or removes only the blocks that changed, after
first reading back ticks, deletions and rewording, because the owner's edits win.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ultron.config import local_today
from ultron.features import habits, profile
from ultron.features.freetime import WAKE
from ultron.features.inbox import Jotting
from ultron.features.items import Item, ItemStore
from ultron.features.onboarding import DAY_NAMES, WEEK, describe_days, describe_times
from ultron.features.roadmap import BoardPlan
from ultron.integrations.notion import (
    Workspace,
    block_text,
    callout,
    is_alive,
    list_children,
    render_block,
    rich_text,
    strip_marker,
)

AREAS = ("Health", "Wealth", "Career", "Relationships", "Learning", "Admin", "Fun")
APPEND_LIMIT = 100  # Notion accepts at most 100 blocks per append
HEADINGS = ("heading_1", "heading_2", "heading_3")


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    kinds: tuple[str, ...]
    collapsible: bool


SECTIONS = (
    Section("today", "☑️ Today's habits", (), False),  # habits due today, as tick boxes
    Section("roadmap", "🚀 Roadmap", (), False),  # roadmap steps; titled by the roadmap
    Section("todo", "✅ To-do", ("task",), False),
    Section("habits", "🔁 Habits", ("habit",), True),
    Section("goals", "🎯 Goals", ("goal",), True),
    Section("ideas", "💡 Ideas", ("idea",), True),
    Section("principles", "🧭 Principles & notes", ("principle", "reference"), True),
    Section("schedule", "📌 My schedule", ("schedule",), True),
    Section("about", "🧠 About me & focus", ("fact",), True),
    Section("log", "📓 Log & journal", ("log",), True),
)
SECTION_BY_KIND = {kind: s for s in SECTIONS for kind in s.kinds}
ROADMAP_KEYS = ("section:roadmap", "group:roadmap:")  # its blocks' keys start with these
STATUS_KEY = "group:roadmap:status"
LOG_WEEKS = "group:log:week:"  # a week's folded heading in 📓 Log
TYPED_IN_ROADMAP = "roadmap:current"  # a line typed under the roadmap's own heading


# --- placement and rendering ---


def group_of(item: Item) -> str | None:
    if item.kind != "task":
        return None
    return f"project:{item.project}" if item.project else f"area:{item.area}"


SECTION_BY_KEY = {s.key: s for s in SECTIONS}
LONDON = ZoneInfo("Europe/London")


def local_date(iso: str, zone: ZoneInfo) -> date:
    return datetime.fromisoformat(iso).astimezone(zone).date()


def ticked_today(item: Item, today: date, zone: ZoneInfo) -> bool:
    """Done today and still showing (ticked) where it was on the Board, not in 📓 Log."""
    return (
        item.status == "done"
        and item.board_block_id is not None
        and not (item.board_place or "").startswith("log/")
        and item.done_at is not None
        and local_date(item.done_at, zone) == today
    )


def logged(item: Item, today: date, zone: ZoneInfo = LONDON) -> bool:
    """A to-do that belongs in 📓 Log: done (once it has left the Board, or if it arrived
    done) or dropped, unless kept out on purpose."""
    if item.kind != "task" or item.log_hidden:
        return False
    if item.status == "dropped":
        return item.dropped_at is not None
    return (
        item.status == "done" and item.done_at is not None and not ticked_today(item, today, zone)
    )


def log_day(item: Item, zone: ZoneInfo) -> date:
    """The day an item belongs to in 📓 Log: done, dropped, or (an entry) written."""
    stamp = item.done_at if item.status == "done" else item.dropped_at
    return local_date(stamp if item.kind == "task" and stamp else item.created_at, zone)


def week_of(item: Item, zone: ZoneInfo) -> str:
    """'week:2026-10-19': the Monday of the item's week in 📓 Log."""
    day = log_day(item, zone)
    return f"week:{day - timedelta(days=day.weekday())}"


def section_of(item: Item, today: date, zone: ZoneInfo = LONDON) -> Section:
    """Habits due today sit in ☑️ Today's habits, roadmap steps in the roadmap's section,
    done and dropped to-dos in 📓 Log; the rest in their kind's section."""
    if logged(item, today, zone):
        return SECTION_BY_KEY["log"]
    if item.kind == "habit" and habits.due_on(item, today):
        return SECTION_BY_KEY["today"]
    if item.is_step:
        return SECTION_BY_KEY["roadmap"]
    return SECTION_BY_KIND[item.kind]


def place_of(
    item: Item, today: date, layout: dict[int, str] | None = None, zone: ZoneInfo = LONDON
) -> str:
    """'todo/project:Robin', 'log/week:2026-10-19', or for a roadmap step
    'roadmap/stage:4' (its stage's heading) or 'roadmap/phase:2' (inside a folded phase).
    `layout` is BoardPlan.groups."""
    section = section_of(item, today, zone)
    if section.key == "log":
        return f"log/{week_of(item, zone)}"
    if item.is_step:
        return f"roadmap/{(layout or {}).get(item.stage_id, 'hidden')}"
    return f"{section.key}/{group_of(item) or ''}"


def is_roadmap_key(key: str) -> bool:
    return key.startswith(ROADMAP_KEYS)


def streak_suffix(item: Item) -> str:
    return f" · 🔥 {item.streak}" if item.streak >= 2 else ""


def paused_suffix(item: Item, today: date) -> str:
    if not item.paused_until or item.paused_until <= today.isoformat():
        return ""
    return f" · paused until {date.fromisoformat(item.paused_until):%a %d %b}"


def shown(item: Item, today: date, zone: ZoneInfo) -> bool:
    """Open items are shown. Ticked ones stay (checked) for the rest of the day they were
    ticked on the Board, then move to 📓 Log; items that arrived already done go straight
    there."""
    return item.status == "open" or ticked_today(item, today, zone) or logged(item, today, zone)


def due_suffix(due: str | None, today: date, on_day: bool = False) -> str:
    """'· due Fri' for a deadline, '· on Mon' for something that happens that day."""
    if not due:
        return ""
    d = date.fromisoformat(due)
    word = "on" if on_day else "due"
    if d < today:
        return f" · overdue ({d:%a %d %b})"
    if d == today:
        return " · today" if on_day else " · due today"
    if d <= today + timedelta(days=6):
        return f" · {word} {d:%a}"
    return f" · {word} {d:%a %d %b}" + (f" {d.year}" if d.year != today.year else "")


def email_suffix(item: Item) -> str:
    """ " · £120.00 · 📧 Council" for to-dos that came from email."""
    parts = []
    if item.amount and item.amount not in item.title:
        parts.append(item.amount)
    if item.source:
        parts.append(f"📧 {item.source}")
    return "".join(f" · {p}" for p in parts)


def _rich(
    text: str, question: str | None, link: str | None = None, linked: str = ""
) -> list[dict[str, Any]]:
    """`linked`, the start of `text`, links to `link` (e.g. a roadmap step's course page)."""
    if link and linked and text.startswith(linked) and link.startswith(("https://", "http://")):
        parts = [{"type": "text", "text": {"content": linked, "link": {"url": link}}}]
        parts += rich_text(text[len(linked) :]) if text[len(linked) :] else []
    else:
        parts = rich_text(text)
    if question:
        parts.append(
            {
                "type": "text",
                "text": {"content": f" (?) {question}"},
                "annotations": {"italic": True, "color": "gray"},
            }
        )
    return parts


NOTE_LINES = 3  # a to-do's grey note shows its last few lines of notes…
NOTE_CHARS = 400  # …up to this long


def note_of(item: Item) -> str:
    """The grey line under a to-do on the Board: its notes, the
    latest few lines. Empty if it has none."""
    if item.kind != "task" or not item.details:
        return ""
    lines = [line.strip() for line in item.details.splitlines() if line.strip()]
    text = "\n".join(lines[-NOTE_LINES:])
    return text if len(text) <= NOTE_CHARS else text[: NOTE_CHARS - 1].rstrip() + "…"


def render_item(
    item: Item, today: date, zone: ZoneInfo, *, routine_minutes: int
) -> tuple[dict[str, Any], str]:
    """Return (Notion block, its plain text as Notion will read it back)."""
    question = None if logged(item, today, zone) else item.question  # moot once it's history
    if item.kind == "habit" and habits.due_on(item, today):
        text = item.title + streak_suffix(item)
        block = {
            "type": "to_do",
            "to_do": {
                "rich_text": _rich(text, question),
                "checked": item.kept_on == today.isoformat(),
            },
        }
    elif logged(item, today, zone):
        # 📓 Log: "Pay Amex · Wed 21" ticked, or "~~Call builders~~ · dropped Thu 22"
        day = f"{log_day(item, zone):%a %d}"
        if item.status == "done":
            text = f"{item.title} · {day}"
            rich = _rich(text, question, item.link, item.title)
            block = {"type": "to_do", "to_do": {"rich_text": rich, "checked": True}}
        else:
            text = f"{item.title} · dropped {day}"
            struck = [
                {**part, "annotations": {"strikethrough": True}} for part in rich_text(item.title)
            ]
            rich = struck + _rich(text[len(item.title) :], question)
            block = {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich}}
        if note_of(item):
            block[block["type"]]["children"] = [_label(note_of(item), italic=True)]
    elif item.kind == "task":
        text = item.title + email_suffix(item) + due_suffix(item.due, today, item.due_on)
        block = {
            "type": "to_do",
            "to_do": {
                "rich_text": _rich(text, question, item.link, item.title),
                "checked": item.status == "done",
            },
        }
        if note_of(item):
            block["to_do"]["children"] = [_label(note_of(item), italic=True)]
    elif item.kind == "principle":
        text = item.title
        block = {"type": "quote", "quote": {"rich_text": _rich(text, question)}}
    elif item.kind == "reference":
        text = item.title
        block = {"type": "toggle", "toggle": {"rich_text": _rich(text, question)}}
        if item.details:
            block["toggle"]["children"] = [
                {"type": "paragraph", "paragraph": {"rich_text": rich_text(item.details)}}
            ]
    elif item.kind == "log" and item.details:
        # A day review: the counts on the line, the lists and reflection inside
        text = f"{local_date(item.created_at, zone):%d %b} · {item.title}"
        block = {"type": "toggle", "toggle": {"rich_text": _rich(text, question)}}
        block["toggle"]["children"] = [
            {"type": "paragraph", "paragraph": {"rich_text": rich_text(line)}}
            for line in item.details.splitlines()
            if line.strip()
        ]
    else:
        if item.kind == "log":
            text = f"{local_date(item.created_at, zone):%d %b} · {item.title}"
        elif item.kind == "schedule" and (
            item.days or item.start_time or item.end_time or item.anchor
        ):
            times = describe_times(item.start_time, item.end_time)
            if item.anchor == WAKE:
                times = f"on waking, {item.minutes or routine_minutes} min"
            day = describe_days(item.days.split(",")) if item.days else ""
            if not item.days and item.due:  # a one-off: its date, so repeats can be told apart
                day = f"{date.fromisoformat(item.due):%a %d %b}"
            text = " · ".join([w for w in (day, times) if w] + [item.title])
            text += f" ({item.details})" if item.details else ""
        else:
            text = item.title + (f" — {item.details}" if item.details else "")
            if item.kind == "habit":
                text = item.title + streak_suffix(item) + paused_suffix(item, today)
        block = {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich(text, question)},
        }
    return block, text + (f" (?) {question}" if question else "")


def section_heading(section: Section) -> dict[str, Any]:
    body: dict[str, Any] = {"rich_text": rich_text(section.title)}
    if section.collapsible:
        body["is_toggleable"] = True
    return {"type": "heading_2", "heading_2": body}


def group_heading(group: str, notes: dict[str, str | None]) -> dict[str, Any]:
    kind, name = group.split(":", 1)
    text = name + (f" · {notes[name]}" if kind == "project" and notes.get(name) else "")
    return {"type": "heading_3", "heading_3": {"rich_text": rich_text(text)}}


def group_order(groups: set[str]) -> list[str]:
    """Projects first (A–Z), then life areas in their fixed order."""
    projects = sorted((g for g in groups if g.startswith("project:")), key=str.casefold)
    areas = [f"area:{a}" for a in AREAS if f"area:{a}" in groups]
    return projects + areas


# --- reading the owner's edits back ---


@dataclass
class BoardSnapshot:
    page_order: list[str]  # page-level block ids, top to bottom
    present: set[str]  # every live block id seen (page level and inside sections)
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    # block id -> the heading it sits under inside a section: (heading text, the Board
    # group it names, e.g. "project:Robin", or None for a heading the owner typed)
    under: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)  # toggle id -> child ids
    section: dict[str, str] = field(default_factory=dict)  # block id -> its section's key
    above: dict[str, str] = field(default_factory=dict)  # block id -> the block before it


@dataclass
class BoardChanges:
    ticked: list[int] = field(default_factory=list)
    unticked: list[int] = field(default_factory=list)
    habits_kept: list[tuple[int, date]] = field(default_factory=list)  # (habit, day) ticked
    habits_unkept: list[tuple[int, date]] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    hidden: list[int] = field(default_factory=list)  # done/dropped to-dos deleted from 📓 Log
    noted: list[tuple[int, str]] = field(default_factory=list)  # lines typed inside a to-do
    renamed: list[tuple[int, str]] = field(default_factory=list)
    dismissed: list[int] = field(default_factory=list)  # questions the owner deleted
    new_lines: list[Jotting] = field(default_factory=list)  # typed on the Board by the owner
    retimed: list[tuple[int, dict[str, Any]]] = field(default_factory=list)  # 📌 times edited

    @property
    def any(self) -> bool:
        return bool(
            self.ticked
            or self.unticked
            or self.habits_kept
            or self.habits_unkept
            or self.deleted
            or self.hidden
            or self.noted
            or self.renamed
            or self.dismissed
            or self.new_lines
            or self.retimed
        )


def snapshot(client: Any, ws: Workspace, store: ItemStore) -> BoardSnapshot:
    keys = store.block_keys()
    toggles = {
        keys[f"section:{s.key}"] for s in SECTIONS if s.collapsible and f"section:{s.key}" in keys
    }
    toggles |= {b for k, b in keys.items() if k.startswith(("group:roadmap:phase:", LOG_WEEKS))}
    groups = {  # a week in 📓 Log isn't a group a typed line could join
        block_id: key.split(":", 2)[2]
        for key, block_id in keys.items()
        if key.startswith("group:") and not key.startswith(LOG_WEEKS)
    }
    # A stage's label inside a folded phase works like a heading: lines under it join it
    labels = {
        b: "stage:" + k.rsplit(":", 1)[1]
        for k, b in keys.items()
        if k.startswith("group:roadmap:label:")
    }
    sections = {block_id: key[8:] for key, block_id in keys.items() if key.startswith("section:")}
    roadmap_heading = keys.get("section:roadmap")
    if roadmap_heading:
        toggles.add(roadmap_heading)  # the roadmap folds, with everything inside (25 Oct)
    page = [b for b in list_children(client, ws.board_id) if is_alive(b)]
    snap = BoardSnapshot(page_order=[b["id"] for b in page], present=set())
    heading: tuple[str, str | None] | None = None  # the heading the next lines sit under
    roadmap_title = ""
    section = ""  # the section the next lines are in

    def seen(block: dict[str, Any], before: str | None) -> None:
        nonlocal heading, roadmap_title, section
        snap.blocks[block["id"]] = block
        if before:
            snap.above[block["id"]] = before
        if block["id"] in sections:
            section = sections[block["id"]]
        snap.section[block["id"]] = section
        if block["id"] == roadmap_heading:
            roadmap_title = block_text(block).strip()
            heading = (roadmap_title, TYPED_IN_ROADMAP)
        elif block["id"] in sections:
            heading = None
        elif block["id"] in labels:
            heading = (f"{roadmap_title} › {block_text(block).strip()}", labels[block["id"]])
        elif block["type"] in HEADINGS:
            group = groups.get(block["id"])
            named = group and group.startswith(("project:", "area:"))
            text = group.split(":", 1)[1] if named else block_text(block).strip()
            if group and group.startswith(("stage:", "phase:")):
                text = f"{roadmap_title} › {text}"  # so the sorter files it in the roadmap
            heading = (text, group) if text else None
        elif heading:
            snap.under[block["id"]] = heading

    def visit(blocks: list[dict[str, Any]]) -> None:
        nonlocal heading
        before = None
        for block in blocks:
            seen(block, before)
            before = block["id"]
            if block["id"] in toggles:
                inside = [c for c in list_children(client, block["id"]) if is_alive(c)]
                snap.children[block["id"]] = [c["id"] for c in inside]
                visit(inside)  # a folded phase sits inside the folded roadmap
                # After a folded roadmap phase, a line is still in the roadmap
                in_roadmap = groups.get(block["id"], "").startswith("phase:")
                heading = (roadmap_title, TYPED_IN_ROADMAP) if in_roadmap else None

    visit(page)
    snap.present = set(snap.blocks)
    return snap


def parse_edit(item: Item, old_text: str, new_text: str) -> tuple[str, bool]:
    """Recover (title, question_dismissed) from text the owner edited on the Board.

    Ultron renders `title + due suffix + " (?) question"`. Owners may reword the title,
    and deleting the "(?) …" part dismisses the question.
    """
    q_part = f" (?) {item.question}" if item.question else ""
    body_old = (
        old_text[: len(old_text) - len(q_part)]
        if q_part and old_text.endswith(q_part)
        else old_text
    )
    due_part = body_old[len(item.title) :] if body_old.startswith(item.title) else ""

    text = new_text.strip()
    dismissed = False
    if q_part:
        if text.endswith(q_part.strip()):
            text = text[: -len(q_part.strip())].rstrip()
        elif " (?) " in text:
            text = text[: text.index(" (?) ")].rstrip()
        else:
            dismissed = True
    if due_part.strip() and text.endswith(due_part.strip()):
        text = text[: -len(due_part.strip())].rstrip()
    return text.strip(" ·-—"), dismissed


SCHEDULE_TIMES = re.compile(
    r"^(?:(?P<start>\d{1,2}:\d{2})\s*[–-]\s*(?P<end>\d{1,2}:\d{2})"
    r"|from (?P<from>\d{1,2}:\d{2})|until (?P<until>\d{1,2}:\d{2}))$"
)
ON_WAKING = re.compile(r"^on waking, (?P<minutes>\d{1,3}) ?min$")
ONE_OFF_DATE = re.compile(r"^[A-Z][a-z]{2} \d{2} [A-Z][a-z]{2}$")  # "Wed 25 Nov": left as is
NAMED_DAYS = {"every day": ",".join(WEEK), "weekdays": ",".join(WEEK[:5]), "weekends": "sat,sun"}


def _days_in(text: str) -> str | None:
    """'Every day', 'Weekdays', 'Mon–Fri', 'Thu, Sun' -> 'mon,…'; None if it isn't days."""
    low = text.casefold().strip()
    if low in NAMED_DAYS:
        return NAMED_DAYS[low]
    names = {v.casefold(): k for k, v in DAY_NAMES.items()}
    if "–" in low or "-" in low:
        ends = [p.strip() for p in re.split(r"[–-]", low)]
        if len(ends) == 2 and all(e in names for e in ends):
            a, b = WEEK.index(names[ends[0]]), WEEK.index(names[ends[1]])
            return ",".join(WEEK[a : b + 1]) if a < b else None
        return None
    parts = [p.strip() for p in low.split(",")]
    if all(p in names for p in parts):
        return ",".join(d for d in WEEK if d in {names[p] for p in parts})
    return None


def _hhmm(text: str) -> str:
    h, m = text.split(":")
    return f"{int(h):02d}:{m}"


def schedule_edit(item: Item, text: str) -> dict[str, Any] | None:
    """A 📌 line the owner edited ("Every day · 22:00–07:00 · Sleep"): the days, times
    and title it now says, as the fields that changed (plus "title"). None if it doesn't
    read as a schedule line, so it's treated as a plain rename. A time the owner deleted
    is left as it was, never wiped."""
    if item.details and text.endswith(f"({item.details})"):
        text = text[: -len(f"({item.details})")].rstrip()
    fields: dict[str, Any] = {}
    title = []
    for part in (p.strip() for p in text.split(" · ")):
        days = _days_in(part)
        times = SCHEDULE_TIMES.match(part)
        waking = ON_WAKING.match(part)
        if days is not None:
            fields["days"] = days
        elif ONE_OFF_DATE.match(part) and item.due:
            continue
        elif times:
            start = times["start"] or times["from"]
            end = times["end"] or times["until"]
            fields["start_time"] = _hhmm(start) if start else None
            fields["end_time"] = _hhmm(end) if end else None
        elif waking:
            fields["minutes"] = int(waking["minutes"])
        else:
            title.append(part)
    if not title or not (set(fields) - {"title"}):
        return None
    changed = {k: v for k, v in fields.items() if getattr(item, k) != v}
    if changed.get("end_time") and item.question == "How long, and travel each way?":
        changed["question"] = None  # the owner answered it
    return {**changed, "title": " · ".join(title)}


# What a line typed in each section becomes: it stays in the section it was typed in.
# An appointment typed in To-do still becomes one, so it reaches the 📅 Schedule.
TYPED_KINDS = {
    "today": ("habit",),
    "roadmap": ("task",),
    "todo": ("task", "schedule"),
    **{s.key: s.kinds for s in SECTIONS if s.collapsible},
}


def _item_above(snap: BoardSnapshot, items: dict[str, Item], block_id: str) -> str | None:
    """The title of the Board item just above a typed line (past other typed lines)."""
    before = snap.above.get(block_id)
    for _ in range(20):
        if before is None or snap.blocks.get(before, {}).get("type") in HEADINGS:
            return None
        if before in items:
            return items[before].title
        before = snap.above.get(before)
    return None


def read_changes(
    client: Any,
    snap: BoardSnapshot,
    ws: Workspace,
    store: ItemStore,
    today: date | None = None,
    shown_day: date | None = None,
) -> BoardChanges:
    """`shown_day` is the day the habit boxes on the page were drawn for (the last write):
    the morning after, a box still ticked from yesterday is yesterday's tick, not today's."""
    today = today or local_today()
    shown = shown_day or today
    changes = BoardChanges()
    items = {i.board_block_id: i for i in store.all(include_dropped=True) if i.board_block_id}
    structure = set(store.block_keys().values()) | {ws.board_hint_id}
    for block_id, block in snap.blocks.items():
        item = items.get(block_id)
        if item is None:
            if block_id not in structure:
                lines, _ = render_block(client, block)
                if lines:
                    heading, group = snap.under.get(block_id, (None, None))
                    changes.new_lines.append(
                        Jotting(
                            block_id,
                            "\n".join(lines),
                            heading=heading,
                            group=group,
                            on_board=True,
                            kinds=TYPED_KINDS.get(snap.section.get(block_id, ""), ()),
                            above=_item_above(snap, items, block_id),
                            section=snap.section.get(block_id, ""),
                        )
                    )
            continue
        if item.kind == "task" and block.get("has_children"):
            note = _typed_inside(client, block, item)
            if note:
                changes.noted.append((item.id, note))
        if block["type"] == "to_do" and item.kind == "habit":
            checked = bool(block["to_do"].get("checked"))
            kept = shown.isoformat() in habits.kept_days(store, item.id)
            if checked != kept:
                (changes.habits_kept if checked else changes.habits_unkept).append((item.id, shown))
        elif block["type"] == "to_do":
            checked = bool(block["to_do"].get("checked"))
            if checked and item.status != "done":
                changes.ticked.append(item.id)
            elif not checked and item.status == "done":
                changes.unticked.append(item.id)
        text = block_text(block).strip()
        if item.board_text is not None and text != item.board_text.strip():
            title, dismissed = parse_edit(item, item.board_text, text)
            if not title:
                _deleted(changes, item)  # emptied out = deleted
                continue
            timing = schedule_edit(item, title) if item.kind == "schedule" else None
            if timing is not None:
                title = timing.pop("title", item.title)
                if timing:
                    changes.retimed.append((item.id, timing))
            if title != item.title:
                changes.renamed.append((item.id, title))
            if dismissed:
                changes.dismissed.append(item.id)
    keys = store.block_keys()
    for block_id, item in items.items():
        if block_id not in snap.present:
            if item.is_step and _toggle_gone(item, keys, snap):
                continue  # its folded phase was deleted: draw it again rather than drop it
            if _log_week_gone(item, keys, snap):
                continue  # likewise a week in 📓 Log
            _deleted(changes, item)
    return changes


def _typed_inside(client: Any, block: dict[str, Any], item: Item) -> str:
    """Lines the owner indented under a to-do (anything inside it but Ultron's grey note
    line): they become notes on it."""
    lines = []
    for child in list_children(client, block["id"]):
        if not is_alive(child) or block_text(child).strip() == (item.board_note or "").strip():
            continue
        lines += [strip_marker(line) for line in render_block(client, child)[0]]
    return "\n".join(line for line in lines if line)


def _deleted(changes: BoardChanges, item: Item) -> None:
    """A line the owner deleted. In 📓 Log, a to-do only leaves the Log: it stays done (or
    dropped), so the history behind it is kept."""
    if (item.board_place or "").startswith("log/") and item.kind == "task":
        changes.hidden.append(item.id)
    elif item.status != "dropped":  # already dropped: its line was on its way out anyway
        changes.deleted.append(item.id)


def _log_week_gone(item: Item, keys: dict[str, str], snap: BoardSnapshot) -> bool:
    """An entry whose week (or the whole 📓 Log) is no longer on the page: deleting the
    heading takes the week with it, more likely a slip than a wish to lose that history."""
    place = item.board_place or ""
    if not place.startswith("log/"):
        return False
    week = keys.get(f"group:log:{place.removeprefix('log/')}")
    return week not in snap.present or keys.get("section:log") not in snap.present


def _toggle_gone(item: Item, keys: dict[str, str], snap: BoardSnapshot) -> bool:
    """A step inside a folded phase whose toggle is no longer on the page. Deleting the
    toggle takes every step inside with it, which is far more likely a slip than a wish
    to drop the whole phase."""
    place = (item.board_place or "").split("/", 1)[-1]
    if not place.startswith("phase:"):
        return False
    return keys.get(f"group:roadmap:{place}") not in snap.present


def apply_changes(store: ItemStore, changes: BoardChanges, today: date | None = None) -> None:
    today = today or local_today()
    for item_id, day in changes.habits_kept:
        habits.keep(store, item_id, day)
    for item_id, day in changes.habits_unkept:
        habits.unkeep(store, item_id, day)
    habits.refresh(store, today)
    for item_id in changes.ticked:
        store.set_status(item_id, "done")
    for item_id in changes.unticked:
        store.set_status(item_id, "open")
    for item_id, title in changes.renamed:
        store.update(item_id, title=title)
    for item_id in changes.dismissed:
        store.update(item_id, question=None)
    for item_id, fields in changes.retimed:
        store.update(item_id, **fields)
    for item_id, note in changes.noted:
        item = store.get(item_id)
        store.update(item_id, details="\n".join(filter(None, (item.details, note))))
    for item_id in changes.hidden:
        store.update(
            item_id,
            log_hidden=True,
            board_block_id=None,
            board_place=None,
            board_text=None,
            board_note=None,
        )
    for item_id in changes.deleted:
        store.update(
            item_id,
            status="dropped",
            board_block_id=None,
            board_place=None,
            board_text=None,
            board_note=None,
        )


# --- writing ---


@dataclass
class WriteResult:
    created: int = 0
    updated: int = 0
    removed: int = 0


class _Writer:
    def __init__(self, client: Any, ws: Workspace, store: ItemStore, snap: BoardSnapshot) -> None:
        self.client = client
        self.ws = ws
        self.store = store
        self.order = list(snap.page_order)
        self.present = set(snap.present)
        self.keys = {k: v for k, v in store.block_keys().items() if v in snap.present}
        self.children = {k: list(v) for k, v in snap.children.items()}  # inside toggles
        self.snap = snap
        self.result = WriteResult()
        self.routine = profile.load(store.db).routine_minutes  # 🔒 routine with no length

    def delete(self, block_id: str) -> None:
        self.client.blocks.delete(block_id=block_id)
        self.present.discard(block_id)
        if block_id in self.order:
            self.order.remove(block_id)
        for kids in self.children.values():
            if block_id in kids:
                kids.remove(block_id)
        self.result.removed += 1

    def retext(self, block_id: str, block: dict[str, Any]) -> None:
        """Rewrite a heading or callout Ultron owns if its text is out of date."""
        current = self.snap.blocks.get(block_id)
        kind = block["type"]
        if current is None or current["type"] != kind:
            return
        if block_text(current) != plain(block):
            body = {"rich_text": block[kind]["rich_text"]}
            self.client.blocks.update(block_id=block_id, **{kind: body})
            self.result.updated += 1

    def forget(self, key: str) -> None:
        self.keys.pop(key, None)
        self.store.set_block_key(key, None)

    def remember(self, key: str, block_id: str) -> None:
        self.keys[key] = block_id
        self.store.set_block_key(key, block_id)

    def insert(
        self, parent: str, blocks: list[dict], after: str | None, at_start: bool = False
    ) -> list[str]:
        """Insert blocks after `after` (or at the end, or at the top if `at_start`),
        returning their ids in order."""
        ids: list[str] = []
        for start in range(0, len(blocks), APPEND_LIMIT):
            chunk = blocks[start : start + APPEND_LIMIT]
            kwargs: dict[str, Any] = {"block_id": parent, "children": chunk}
            if after:
                kwargs["position"] = {"type": "after_block", "after_block": {"id": after}}
            elif at_start:
                kwargs["position"] = {"type": "start"}
            results = self.client.blocks.children.append(**kwargs)["results"]
            # Inserting after a block, Notion returns the new blocks followed by every block after
            # them, so keep only the first len(chunk) results.
            new = [b["id"] for b in results[: len(chunk)]]
            top = at_start and not after
            if parent == self.ws.board_id:
                at = self.order.index(after) + 1 if after in self.order else len(self.order)
                self.order[at:at] = new
            elif parent in self.children:
                kids = self.children[parent]
                at = 0 if top else kids.index(after) + 1 if after in kids else len(kids)
                kids[at:at] = new
            self.present.update(new)
            ids += new
            after = new[-1]
        return ids

    def parent_of(self, block_id: str) -> str | None:
        """The page or toggle a live block sits in."""
        if block_id in self.order:
            return self.ws.board_id
        return next((t for t, kids in self.children.items() if block_id in kids), None)

    def last_of(self, section: str, group: str | None = None) -> str | None:
        """The bottom page-level block belonging to a section (or one group within it)."""
        wanted = {self.keys.get(f"section:{section}")} if group is None else set()
        for key, block_id in self.keys.items():
            if group is None and key.startswith(f"group:{section}:"):
                wanted.add(block_id)
            if group is not None and key == f"group:{section}:{group}":
                wanted.add(block_id)
        for item in self.store.all():
            if item.board_block_id in self.present and item.board_place:
                s, g = item.board_place.split("/", 1)
                if s == section and (group is None or g == group):
                    wanted.add(item.board_block_id)
        positions = [self.order.index(b) for b in wanted if b in self.order]
        return self.order[max(positions)] if positions else None

    def anchor_for_section(self, section: Section) -> str | None:
        before = SECTIONS[: SECTIONS.index(section)]
        for prev in reversed(before):
            if f"section:{prev.key}" in self.keys:
                return self.last_of(prev.key)
        hint = self.ws.board_hint_id
        return hint if hint in self.order else None


def plain(block: dict[str, Any]) -> str:
    return "".join(p["text"]["content"] for p in block[block["type"]].get("rich_text", []))


def write_board(
    client: Any,
    ws: Workspace,
    store: ItemStore,
    snap: BoardSnapshot,
    today: date,
    zone: ZoneInfo,
    plan: BoardPlan | None = None,
    anchors: dict[int, str] | None = None,
) -> WriteResult:
    """Make the Board match the items: remove, update, then add only what changed.
    `plan` draws the active roadmap's section (roadmap.board_plan); without one, its
    steps aren't shown. `anchors` puts a new item right after a block (the line the owner
    typed it as) instead of at the end of its group."""
    w = _Writer(client, ws, store, snap)
    items = store.all(include_dropped=True)
    layout = plan.groups if plan else {}
    wanted = {
        i.id: i
        for i in items
        if shown(i, today, zone)
        and (not i.is_step or i.stage_id in layout or logged(i, today, zone))
    }

    # 1. Remove blocks that shouldn't be there, or moved section/group, or changed block type.
    for item in items:
        if not item.board_block_id:
            continue
        keep = item.id in wanted and item.board_place == place_of(item, today, layout, zone)
        if keep and item.board_block_id in w.present:
            block, text = render_item(item, today, zone, routine_minutes=w.routine)
            keep = snap.blocks.get(item.board_block_id, {}).get("type") == block["type"]
            # A toggle's nested lines can't be edited in place: replace it when it changes
            if keep and block["type"] == "toggle" and text != item.board_text:
                keep = False
        if not keep or item.board_block_id not in w.present:
            if item.board_block_id in w.present:
                w.delete(item.board_block_id)
            store.update(
                item.id, board_block_id=None, board_place=None, board_text=None, board_note=None
            )

    # 2. Update blocks whose text or tick changed (e.g. a new due date or question).
    for item in store.all(include_dropped=True):
        if item.id not in wanted or not item.board_block_id:
            continue
        block, text = render_item(item, today, zone, routine_minutes=w.routine)
        current = snap.blocks.get(item.board_block_id, {})
        checked = current.get("to_do", {}).get("checked")
        if text != item.board_text or (
            block["type"] == "to_do" and checked != block["to_do"]["checked"]
        ):
            payload = {k: v for k, v in block[block["type"]].items() if k != "children"}
            client.blocks.update(block_id=item.board_block_id, **{block["type"]: payload})
            store.update(item.id, board_text=text)
            w.result.updated += 1
        if item.kind == "task" and note_of(item) != (item.board_note or ""):
            _renote(w, item)

    # 3. Add new blocks, section by section.
    notes = {p.name: p.note for p in store.projects()}
    current_items = [i for i in store.all(include_dropped=True) if i.id in wanted]
    pending = [i for i in current_items if not i.board_block_id]
    for item in pending:
        after = (anchors or {}).get(item.id)
        parent = w.parent_of(after) if after else None
        if parent:
            _add_items(w, store, parent, after, [item], today, zone, layout)
    pending = [i for i in pending if not (store.get(i.id) or i).board_block_id]
    for section in SECTIONS:
        in_section = [i for i in current_items if section_of(i, today, zone) is section]
        new = [i for i in pending if section_of(i, today, zone) is section]
        if section.key == "roadmap":
            _write_roadmap(w, plan, in_section, today, zone)
            continue
        heading_key = f"section:{section.key}"
        if in_section and heading_key not in w.keys:
            [heading] = w.insert(
                ws.board_id, [section_heading(section)], w.anchor_for_section(section)
            )
            w.keys[heading_key] = heading
            store.set_block_key(heading_key, heading)
            w.children[heading] = []
        if section.key == "log" and in_section:
            _write_log(w, in_section, today, zone)
            continue
        if not new:
            continue
        if section.collapsible:
            new.sort(key=lambda i: (i.kind != "principle", i.id))
            _add_items(w, store, w.keys[heading_key], None, new, today, zone)
            continue
        if section.key == "today":  # an always-open list with no groups
            new.sort(key=lambda i: i.title.casefold())  # "Morning routine · …" sit together
            after = w.last_of(section.key) or w.keys[heading_key]
            _add_items(w, store, ws.board_id, after, new, today, zone)
            continue
        groups = group_order({group_of(i) for i in in_section})
        for n, group in enumerate(groups):
            group_new = [i for i in new if group_of(i) == group]
            if not group_new:
                continue
            gkey = f"group:{section.key}:{group}"
            if gkey not in w.keys:
                prev = next(
                    (
                        w.last_of(section.key, g)
                        for g in reversed(groups[:n])
                        if w.last_of(section.key, g)
                    ),
                    None,
                )
                [gid] = w.insert(
                    ws.board_id, [group_heading(group, notes)], prev or w.keys[heading_key]
                )
                w.keys[gkey] = gid
                store.set_block_key(gkey, gid)
            after = w.last_of(section.key, group) or w.keys[gkey]
            _add_items(w, store, ws.board_id, after, group_new, today, zone)

    # 4. Remove headings left with nothing under them (the roadmap's own tidy themselves).
    live_places = {
        place_of(i, today, layout, zone) for i in store.all(include_dropped=True) if i.id in wanted
    }
    live_sections = {p.split("/")[0] for p in live_places}
    # Headings inside a section (a 📓 Log week) go before the section that holds them
    for key, block_id in sorted(w.keys.items(), key=lambda kv: kv[0].startswith("section:")):
        if is_roadmap_key(key):
            continue
        parts = key.split(":", 2)
        empty = (
            parts[1] not in live_sections
            if parts[0] == "section"
            else f"{parts[1]}/{parts[2]}" not in live_places
        )
        if empty:
            w.delete(block_id)
            del w.keys[key]
            store.set_block_key(key, None)
    return w.result


def _renote(w: _Writer, item: Item) -> None:
    """Redraw the grey note line under a to-do, in place. Anything else inside it (lines
    the owner indented under it, now in its notes) goes too."""
    for child in list_children(w.client, item.board_block_id):
        if is_alive(child):
            w.client.blocks.delete(block_id=child["id"])
    note = note_of(item)
    if note:
        w.client.blocks.children.append(
            block_id=item.board_block_id, children=[_label(note, italic=True)]
        )
    w.store.update(item.id, board_note=note or None)
    w.result.updated += 1


def _add_items(
    w: _Writer,
    store: ItemStore,
    parent: str,
    after: str | None,
    items: list[Item],
    today: date,
    zone: ZoneInfo,
    layout: dict[int, str] | None = None,
    at_start: bool = False,
) -> None:
    rendered = [render_item(i, today, zone, routine_minutes=w.routine) for i in items]
    ids = w.insert(parent, [block for block, _ in rendered], after, at_start)
    for item, block_id, (_, text) in zip(items, ids, rendered, strict=True):
        store.update(
            item.id,
            board_block_id=block_id,
            board_place=place_of(item, today, layout, zone),
            board_text=text,
            board_note=note_of(item) or None,
        )
    w.result.created += len(items)


# --- lines the owner typed ---

# Where a typed line stays put as the item it became. Not the roadmap, ☑️ Today's habits
# or 📓 Log: those keep their own order.
STAY_PUT = {s.key for s in SECTIONS} - {"roadmap", "today", "log"}


@dataclass
class Placed:
    adopted: set[str] = field(default_factory=set)  # typed blocks that are now the item
    anchors: dict[int, str] = field(default_factory=dict)  # item id -> typed block to follow


def place_typed(
    store: ItemStore,
    snap: BoardSnapshot,
    typed: list[Jotting],
    today: date,
    zone: ZoneInfo,
) -> Placed:
    """A line typed on the Board stays where it was typed. If it
    became one item that belongs in the section it sits in, Ultron takes over the block
    itself (or, if it needs another kind of block, e.g. a paragraph that's now a to-do,
    draws the item right after it). Otherwise it goes where it's filed, as before."""
    placed = Placed()
    routine = profile.load(store.db).routine_minutes
    for jotting in typed:
        block = snap.blocks.get(jotting.block_id)
        if block is None or jotting.section not in STAY_PUT:
            continue
        mine = [i for i in store.all() if list(i.source_blocks) == [jotting.block_id]]
        if len(mine) != 1 or mine[0].board_block_id:
            continue
        item = mine[0]
        if section_of(item, today, zone).key != jotting.section:
            continue
        if render_item(item, today, zone, routine_minutes=routine)[0]["type"] == block["type"]:
            store.update(
                item.id,
                board_block_id=jotting.block_id,
                board_place=place_of(item, today, None, zone),
                board_text=block_text(block).strip(),  # step 2 of the write fixes it up
                board_note=None,
            )
            placed.adopted.add(jotting.block_id)
        else:
            placed.anchors[item.id] = jotting.block_id
    return placed


# --- the 📓 Log section ---


def _log_rank(item: Item) -> int:
    """Within a week: done to-dos, then dropped ones, then entries."""
    if item.kind != "task":
        return 2
    return 0 if item.status == "done" else 1


def week_title(week: str, items: list[Item], today: date) -> str:
    """'Week of Mon 19 Oct · 34 done'."""
    monday = date.fromisoformat(week.removeprefix("week:"))
    title = f"Week of {monday:%a %d %b}" + (f" {monday.year}" if monday.year != today.year else "")
    done = sum(i.kind == "task" and i.status == "done" for i in items)
    return title + (f" · {done} done" if done else "")


def _write_log(w: _Writer, items: list[Item], today: date, zone: ZoneInfo) -> None:
    """A folded heading per week, newest at the top. New lines go in their place among the
    week's lines already there; lines the owner typed in it are left alone. Lines that left
    or moved were dealt with in write_board's step 1."""
    section = w.keys["section:log"]
    weeks: dict[str, list[Item]] = {}
    for item in items:
        weeks.setdefault(week_of(item, zone), []).append(item)
    above = None  # the newer week's heading: the next week goes below it
    for week in sorted(weeks, reverse=True):
        mine = sorted(weeks[week], key=lambda i: (_log_rank(i), log_day(i, zone), i.id))
        key = f"group:log:{week}"
        heading = _heading3(week_title(week, mine, today), toggle=True)
        if key in w.keys:
            w.retext(w.keys[key], heading)
        else:
            [gid] = w.insert(section, [heading], above, at_start=above is None)
            w.remember(key, gid)
            w.children[gid] = []
        toggle = above = w.keys[key]
        after, run = None, []
        for item in mine:
            if item.board_block_id in w.present:
                if run:
                    _add_items(w, w.store, toggle, after, run, today, zone, at_start=not after)
                    run = []
                after = item.board_block_id
            else:
                run.append(item)
        if run:
            _add_items(w, w.store, toggle, after, run, today, zone, at_start=not after)


# --- the roadmap section ---


def _heading3(text: str, toggle: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"rich_text": rich_text(text)}
    if toggle:
        body["is_toggleable"] = True
    return {"type": "heading_3", "heading_3": body}


def _label(text: str, italic: bool = False) -> dict[str, Any]:
    parts = rich_text(text)
    for p in parts:
        p["annotations"] = {"italic": True, "color": "gray"} if italic else {"bold": True}
    return {"type": "paragraph", "paragraph": {"rich_text": parts}}


def _write_roadmap(
    w: _Writer, plan: BoardPlan | None, steps: list[Item], today: date, zone: ZoneInfo
) -> None:
    """The roadmap section, folded: one toggle heading with
    everything inside it: the status box, a heading per open stage with its steps in
    order, then a folded toggle per later phase. Steps already removed or moved were dealt
    with in write_board's step 1; new ones are added in their place here."""
    if plan is None:
        _tidy_roadmap(w, set())
        return
    section = SECTION_BY_KEY["roadmap"]
    layout = plan.groups
    by_stage: dict[int, list[Item]] = {}
    for item in steps:
        by_stage.setdefault(item.stage_id, []).append(item)
    for mine in by_stage.values():
        mine.sort(key=lambda i: (i.step or 0, i.id))

    heading = {
        "type": "heading_2",
        "heading_2": {"rich_text": rich_text(plan.title), "is_toggleable": True},
    }
    status = callout(plan.status, "🎯")
    hid = w.keys.get("section:roadmap")
    if hid and not w.snap.blocks.get(hid, {}).get("heading_2", {}).get("is_toggleable"):
        _unfold_roadmap(w)  # drawn open, at page level, before 25 Oct: redraw it folded
        hid = None
    if hid is None:
        [hid] = w.insert(w.ws.board_id, [heading], w.anchor_for_section(section))
        w.remember("section:roadmap", hid)
        w.children[hid] = []
    else:
        w.retext(hid, heading)
    if STATUS_KEY not in w.keys:
        [sid] = w.insert(hid, [status], None)
        w.remember(STATUS_KEY, sid)
    else:
        w.retext(w.keys[STATUS_KEY], status)
    live = {"section:roadmap", STATUS_KEY}
    prev = w.keys[STATUS_KEY]

    for stage in plan.stages:  # the stages of phases under way
        group = layout[stage.id]
        mine = by_stage.get(stage.id, [])
        if not group.startswith("stage:") or not mine:
            continue
        key = f"group:roadmap:{group}"
        block = _heading3(plan.stage_headings[stage.id])
        if key not in w.keys:
            [gid] = w.insert(hid, [block], prev)
            w.remember(key, gid)
        else:
            w.retext(w.keys[key], block)
        live.add(key)
        _place_in_order(w, hid, w.keys[key], mine, today, zone, layout)
        placed = [w.keys[key]] + [(w.store.get(i.id) or i).board_block_id for i in mine]
        prev = _last_inside(w, hid, placed) or w.keys[key]

    phases = sorted({s.phase for s in plan.stages if s.phase in plan.folded})
    for phase in phases:  # later phases, folded
        mine = [i for s in plan.stages if s.phase == phase for i in by_stage.get(s.id, [])]
        if not mine:
            continue
        key = f"group:roadmap:phase:{phase}"
        title, about = plan.folded[phase]
        block = _heading3(title, toggle=True)
        if key not in w.keys:
            [tid] = w.insert(hid, [block], prev)
            w.remember(key, tid)
            w.children[tid] = []
        else:
            w.retext(w.keys[key], block)
        live.add(key)
        live |= _fill_toggle(w, plan, phase, about, by_stage, today, zone)
        prev = w.keys[key]
    _tidy_roadmap(w, live)


def _last_inside(w: _Writer, parent: str, ids: list[str | None]) -> str | None:
    """The lowest of `ids` among a toggle's children."""
    kids = w.children.get(parent, [])
    found = [kids.index(b) for b in ids if b in kids]
    return kids[max(found)] if found else None


def _unfold_roadmap(w: _Writer) -> None:
    """Remove the roadmap drawn at page level (and its steps' blocks), so it's redrawn
    inside one toggle. Blocks inside its phase toggles go with them."""
    for item in w.store.all(include_dropped=True):
        if (item.board_place or "").startswith("roadmap/"):
            if item.board_block_id in w.order:
                w.delete(item.board_block_id)
            w.store.update(
                item.id, board_block_id=None, board_place=None, board_text=None, board_note=None
            )
    for key in [k for k in w.keys if is_roadmap_key(k)]:
        if w.keys[key] in w.order:
            w.delete(w.keys[key])
        w.forget(key)


def _place_in_order(
    w: _Writer,
    parent: str,
    heading_id: str,
    mine: list[Item],
    today: date,
    zone: ZoneInfo,
    layout: dict[int, str],
) -> None:
    """Add a stage's new steps in step order, between the ones already on the page."""
    after, run = heading_id, []
    for item in mine:
        if item.board_block_id and item.board_block_id in w.present:
            if run:
                _add_items(w, w.store, parent, after, run, today, zone, layout)
                run = []
            after = item.board_block_id
        else:
            run.append(w.store.get(item.id) or item)
    if run:
        _add_items(w, w.store, parent, after, run, today, zone, layout)


def _fill_toggle(
    w: _Writer,
    plan: BoardPlan,
    phase: int,
    about: str,
    by_stage: dict[int, list[Item]],
    today: date,
    zone: ZoneInfo,
) -> set[str]:
    """A folded phase's contents: its goal line, then each stage (labelled, if the phase
    has several) with its steps. If anything is missing or out of order, Ultron's own
    blocks inside are redrawn; lines the owner typed there are left alone. Returns the
    keys in use."""
    toggle = w.keys[f"group:roadmap:phase:{phase}"]
    layout = plan.groups
    wanted: list[tuple[str, Any]] = [(f"group:roadmap:about:{phase}", _label(about, italic=True))]
    for stage in plan.stages:
        mine = by_stage.get(stage.id, [])
        if stage.phase != phase or not mine:
            continue
        if phase in plan.multi_stage:
            label = _label(plan.stage_headings[stage.id])
            wanted.append((f"group:roadmap:label:{stage.id}", label))
        wanted += [("item", w.store.get(i.id) or i) for i in mine]

    def identity(entry: tuple[str, Any]) -> str | None:
        key, value = entry
        if key == "item":
            return value.board_block_id if value.board_block_id in w.present else None
        return w.keys.get(key)

    inside = ("group:roadmap:about:", "group:roadmap:label:")
    mine_ids = {i.board_block_id for s in by_stage.values() for i in s if i.board_block_id}
    mine_ids |= {b for k, b in w.keys.items() if k.startswith(inside)}
    current = [c for c in w.children.get(toggle, []) if c in mine_ids and c in w.present]
    expected = [identity(e) for e in wanted]
    keys = {k for k, _ in wanted if k != "item"}
    if None not in expected and current == expected:
        for key, block in wanted:
            if key != "item":
                w.retext(w.keys[key], block)
        return keys
    for block_id in current:  # redraw: remove Ultron's blocks, then add them back in order
        w.delete(block_id)
    for key, value in wanted:
        if key == "item":
            w.store.update(
                value.id, board_block_id=None, board_place=None, board_text=None, board_note=None
            )
        else:
            w.forget(key)
    blocks, texts = [], []
    for key, value in wanted:
        if key == "item":
            block, text = render_item(value, today, zone, routine_minutes=w.routine)
        else:
            block, text = value, None
        blocks.append(block)
        texts.append(text)
    ids = w.insert(toggle, blocks, None)  # at the end, after any lines the owner typed
    for (key, value), block_id, text in zip(wanted, ids, texts, strict=True):
        if key == "item":
            w.store.update(
                value.id,
                board_block_id=block_id,
                board_place=place_of(value, today, layout),
                board_text=text,
                board_note=note_of(value) or None,
            )
            w.result.created += 1
        else:
            w.remember(key, block_id)
    return keys


def _tidy_roadmap(w: _Writer, live: set[str]) -> None:
    """Remove the roadmap's headings, labels and toggles that no longer have anything to
    show. Blocks inside a toggle go first, so nothing is deleted twice."""
    dead = [k for k in w.keys if is_roadmap_key(k) and k not in live]
    inside = [k for k in dead if k.startswith(("group:roadmap:about:", "group:roadmap:label:"))]
    for key in inside + [k for k in dead if k not in inside]:
        block_id = w.keys[key]
        parent_gone = key in inside and not any(
            block_id in kids for t, kids in w.children.items() if t in w.present
        )
        if block_id in w.present and not parent_gone:
            w.delete(block_id)
        w.forget(key)
