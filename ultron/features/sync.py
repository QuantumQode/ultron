"""The hourly sync: Board edits → new notes → sort → Board → tidy the Inbox.

Claude is only called when there's something new to sort. Notion-only work (reading
edits, writing the Board, moving notes into ✓ Processed) is free.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ultron.config import Settings
from ultron.errors import UserFacingError
from ultron.features import habits, roadmap, watch
from ultron.features.board import (
    Placed,
    apply_changes,
    place_typed,
    read_changes,
    snapshot,
    write_board,
)
from ultron.features.email_report import in_quiet_hours
from ultron.features.inbox import KEY_LAST_SEEN_EDIT, Jotting, read_inbox
from ultron.features.items import (
    AppliedUpdate,
    ItemStore,
    apply_sort,
    apply_updates,
    board_context,
    close_past_appointments,
)
from ultron.features.schedule import (
    apply_edits,
    build_view,
    follow_board,
    read_edits,
    render,
    write_schedule,
)
from ultron.features.sorter import sort_jottings
from ultron.integrations import notion
from ultron.notify import Notifier, Space
from ultron.ops import activity
from ultron.ops.guard import CostGuard
from ultron.state import State

log = logging.getLogger(__name__)

KEY_BOARD_SEEN = "board.last_seen_edit"
KEY_BOARD_DAY = "board.last_write_day"
KEY_ITEMS_STAMP = "board.items_stamp"  # items as of the last Board write
KEY_SCHEDULE_SIG = "schedule.last_written"  # what the Schedule page showed last time
KEY_SCHEDULE_SEEN = "schedule.last_seen_edit"  # page edit time after our last write
KEY_ROADMAP_SHOWN = "board.roadmap_status"  # the roadmap's status box as last written
PARK_AFTER = 2  # a line missed this many times (unchanged) is parked, not re-sent

_lock = asyncio.Lock()  # never run two syncs at once


@dataclass
class SyncReport:
    sorted_lines: int = 0
    items_saved: int = 0
    board_created: int = 0
    board_updated: int = 0
    board_removed: int = 0
    moved: int = 0
    edits: int = 0  # ticks, deletions, rewording read back from the Board
    missed: list[str] = field(default_factory=list)  # left in place this time
    parked: list[str] = field(default_factory=list)  # newly parked; owner is told
    move_failures: int = 0
    cost_usd: float = 0.0
    skipped: bool = False  # nothing changed anywhere
    schedule_changed: bool = False
    updates: list[AppliedUpdate] = field(default_factory=list)  # progress notes applied

    def summary(self) -> str:
        if self.skipped:
            return "Nothing new since the last sync."
        parts = []
        if self.sorted_lines:
            parts.append(f"sorted {self.sorted_lines} new line(s) into {self.items_saved} item(s)")
        if self.edits:
            parts.append(f"picked up {self.edits} edit(s) from the Board")
        if self.updates:
            parts.append(f"applied {len(self.updates)} update(s) to existing items")
        board = self.board_created + self.board_updated + self.board_removed
        if board:
            parts.append(
                f"Board: +{self.board_created} ~{self.board_updated} −{self.board_removed}"
            )
        if self.schedule_changed:
            parts.append("updated the 📅 Schedule")
        if self.moved:
            parts.append(f"moved {self.moved} note(s) into ✓ Processed")
        if self.missed:
            parts.append(f"{len(self.missed)} line(s) couldn't be sorted and stay in place")
        if self.move_failures:
            parts.append(f"{self.move_failures} note(s) couldn't be moved and stay in place")
        text = "; ".join(parts) or "Everything already up to date"
        if self.cost_usd:
            text += f" (cost ${self.cost_usd:.4f})"
        return text[0].upper() + text[1:] + "."


def updates_message(updates: list[AppliedUpdate]) -> str:
    """What Ultron changed from progress notes, so nothing happens silently."""
    lines = ["📥 Updates from your notes:"]
    for u in updates:
        lines.append(f"• {u.item.title}: {', '.join(u.changes)} ({u.note})")
    lines.append("Anything wrong? Fix it on the Board, or tell me here.")
    return "\n".join(lines)


class _Misses:
    def __init__(self, state: State) -> None:
        self.db = state.db

    def parked(self) -> set[str]:
        rows = self.db.execute(
            "SELECT block_id FROM sort_misses WHERE misses >= ?", (PARK_AFTER,)
        ).fetchall()
        return {r[0] for r in rows}

    def forget_if_edited(self, jottings: list[Jotting]) -> None:
        """An edited line gets a fresh chance."""
        for j in jottings:
            self.db.execute(
                "DELETE FROM sort_misses WHERE block_id = ? AND text != ?", (j.block_id, j.text)
            )

    def record(self, missed: list[Jotting]) -> list[Jotting]:
        """Count misses; return lines that just reached the parking limit."""
        newly_parked = []
        for j in missed:
            self.db.execute(
                """INSERT INTO sort_misses (block_id, text, misses) VALUES (?, ?, 1)
                   ON CONFLICT (block_id)
                   DO UPDATE SET misses = misses + 1, text = excluded.text""",
                (j.block_id, j.text),
            )
            row = self.db.execute(
                "SELECT misses, told_owner FROM sort_misses WHERE block_id = ?", (j.block_id,)
            ).fetchone()
            if row["misses"] >= PARK_AFTER and not row["told_owner"]:
                self.db.execute(
                    "UPDATE sort_misses SET told_owner = 1 WHERE block_id = ?", (j.block_id,)
                )
                newly_parked.append(j)
        return newly_parked

    def clear(self, block_ids: list[str]) -> None:
        self.db.executemany("DELETE FROM sort_misses WHERE block_id = ?", [(b,) for b in block_ids])


def _inbox_leftovers(client: Any, state: State, ws: notion.Workspace) -> list[str]:
    """Handled Inbox blocks still sitting outside ✓ Processed."""
    handled = state.inbox_handled()
    return [
        b["id"]
        for b in notion.list_children(client, ws.inbox_id)
        if notion.is_alive(b) and b["id"] in handled and b["id"] != ws.processed_id
    ]


async def run_sync(
    settings: Settings,
    state: State,
    guard: CostGuard,
    notifier: Notifier,
    make_llm: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    client: Any | None = None,
    force: bool = False,
    today: datetime | None = None,
) -> SyncReport:
    async with _lock:
        own_client = client is None
        client = client or notion.make_client(settings)
        try:
            report = await _sync(settings, state, guard, notifier, make_llm, client, force, today)
        finally:
            if own_client:
                client.close()
    await _watch(settings, state, notifier, today)
    return report


async def _watch(settings: Settings, state: State, notifier: Notifier, now: datetime | None):
    """Ask about clashes and unknown lengths the sync may have brought in (not at night:
    they wait for the first sync after quiet hours)."""
    now = now or datetime.now(settings.zone)
    if in_quiet_hours(now, settings.quiet_hours_start, settings.quiet_hours_end):
        return
    try:
        await watch.check(state, notifier, now, settings.nightly_time)
    except Exception:  # noqa: BLE001 - the sync itself worked; the next one retries
        log.exception("Schedule watch failed")


def _update_schedule(client: Any, state: State, ws: notion.Workspace, now: datetime) -> bool:
    """Rewrite the 📅 Schedule only if what it should show has changed (new day, new plan,
    fixed schedule edited): an unchanged hour costs no Notion calls."""
    if not ws.schedule_id:
        return False
    view = build_view(state, now.date())
    sig = json.dumps(
        [ws.schedule_id, [(t, [ln.sig for ln in lines]) for t, lines in render(view).values()]]
    )
    if sig == state.get(KEY_SCHEDULE_SIG):
        return False
    written = write_schedule(client, state, ws.schedule_id, ws.schedule_hint_id, view)
    state.set(KEY_SCHEDULE_SIG, sig)
    page = client.pages.retrieve(page_id=ws.schedule_id)
    state.set(KEY_SCHEDULE_SEEN, page["last_edited_time"])  # our own write isn't an edit
    return written.changed


def _schedule_edits(client: Any, state: State, ws: notion.Workspace, force: bool) -> int:
    """Read ticks, deletions and rewording from the 📅 Schedule (only if it changed)."""
    if not ws.schedule_id:
        return 0
    seen = client.pages.retrieve(page_id=ws.schedule_id)["last_edited_time"]
    if not force and seen == state.get(KEY_SCHEDULE_SEEN):
        return 0
    edits = read_edits(client, state, ws.schedule_id)
    with activity.caused_by("you, on the 📅 Schedule"):
        apply_edits(state, edits)
    state.set(KEY_SCHEDULE_SEEN, seen)
    return len(edits.ticked) + len(edits.unticked) + len(edits.deleted) + len(edits.renamed)


async def _sync(settings, state, guard, notifier, make_llm, client, force, today) -> SyncReport:
    run = asyncio.to_thread
    report = SyncReport()
    store = ItemStore(state)
    misses = _Misses(state)
    now = today or datetime.now(settings.zone)
    day = now.date().isoformat()

    ws = (
        await run(notion.ensure_workspace, client, state, notion.root_page_id(settings))
    ).workspace

    # 0. Schedule edits: a block ticked there ticks its Board to-do in this same sync.
    schedule_edits = await run(_schedule_edits, client, state, ws, force)
    report.edits += schedule_edits

    # 1. Board edits first: the owner's changes win. Skipped if the Board is untouched today.
    board_edit = (await run(client.pages.retrieve, page_id=ws.board_id))["last_edited_time"]
    board_dirty = (
        force or board_edit != state.get(KEY_BOARD_SEEN) or state.get(KEY_BOARD_DAY) != day
    )
    board_lines: list[Jotting] = []
    board_snap = None
    if board_dirty:
        snap = board_snap = await run(snapshot, client, ws, store)
        last_day = state.get(KEY_BOARD_DAY)
        shown_day = date.fromisoformat(last_day) if last_day else None
        changes = await run(read_changes, client, snap, ws, store, now.date(), shown_day)
        with activity.caused_by("you, on the 🗂 Board"):
            apply_changes(store, changes, now.date())
        report.edits = len(changes.ticked) + len(changes.unticked) + len(changes.deleted)
        report.edits += len(changes.renamed) + len(changes.dismissed) + len(changes.noted)
        report.edits += len(changes.habits_kept) + len(changes.habits_unkept)
        board_lines = changes.new_lines
    for d in (now.date(), now.date() + timedelta(days=1)):
        follow_board(state, d)  # to-dos ticked on the Board tick their planned blocks
    habits.refresh(store, now.date())  # a missed day breaks a streak
    close_past_appointments(store, now.date())  # yesterday's one-offs leave the Schedule

    # 2. New notes from the Inbox (cheap no-op when it's unchanged) plus lines typed on the Board.
    inbox = await run(read_inbox, client, state, ws, force=force)
    handled = state.inbox_handled()
    board_lines = [j for j in board_lines if j.block_id not in handled]  # sorted, move pending
    fresh = inbox.jottings + board_lines
    for j in fresh:
        where = "line typed on the 🗂 Board" if j.on_board else "📥 Inbox note"
        activity.record("inbox", f"Read {where}: {activity.one_line(j.text)}", text=j.text)
    misses.forget_if_edited(fresh)
    parked = misses.parked()
    to_sort = [j for j in fresh if j.block_id not in parked]

    roadmap.adopt(state, now.date())  # e.g. a to-do for the roadmap added in chat
    plan = roadmap.board_plan(state, now.date())
    items_changed = store.stamp() != state.get(KEY_ITEMS_STAMP)  # e.g. onboarding saved
    # The status box counts time from the Schedule, which can change with no item changing
    items_changed |= (plan.status if plan else "") != (state.get(KEY_ROADMAP_SHOWN) or "")
    if not board_dirty and not inbox.changed and not items_changed:
        leftovers = await run(_inbox_leftovers, client, state, ws)
        if not leftovers:
            report.schedule_changed = await run(_update_schedule, client, state, ws, now)
            report.skipped = not report.schedule_changed
            return report

    # 3. Sort (the only step that can cost money).
    covered: list[Jotting] = []
    if to_sort:
        context = board_context(store, now.date())
        async with make_llm() as llm:
            outcome = await sort_jottings(
                llm,
                guard,
                to_sort,
                model=settings.model_fast,
                today=now.date(),
                tz=settings.tz,
                existing=context.lines,
            )
        report.cost_usd = outcome.cost_usd
        report.sorted_lines = len(to_sort)
        merges: list[AppliedUpdate] = []
        with activity.caused_by("your notes"):
            report.items_saved = len(apply_sort(store, outcome, context, merges, now.date()))
            report.updates = apply_updates(store, outcome, context, now.date()) + merges
        missed_ids = {j.block_id for j in outcome.uncovered}
        covered = [j for j in to_sort if j.block_id not in missed_ids]
        report.missed = [j.text for j in outcome.uncovered]
        for j in misses.record(outcome.uncovered):
            report.parked.append(j.text)
        misses.clear([j.block_id for j in covered])
        state.mark_inbox_handled([j.block_id for j in covered])
    # Lines typed on the Board stay where they were typed, as the items they became
    placed = Placed()
    typed_here = [j for j in covered if j.on_board]
    if board_snap and typed_here:
        placed = place_typed(store, board_snap, typed_here, now.date(), settings.zone)

    # 4. Write the Board. Lines typed under a roadmap heading join that stage first.
    roadmap.adopt(state, now.date(), {j.block_id: j.group for j in board_lines})
    plan = roadmap.board_plan(state, now.date())
    snap = await run(snapshot, client, ws, store)
    written = await run(
        write_board, client, ws, store, snap, now.date(), settings.zone, plan, placed.anchors
    )
    state.set(KEY_ROADMAP_SHOWN, plan.status if plan else "")
    report.board_created, report.board_updated = written.created, written.updated
    report.board_removed = written.removed
    report.schedule_changed = await run(_update_schedule, client, state, ws, now)

    # 5. Tidy: move sorted Inbox notes, and sorted Board-typed lines, into ✓ Processed.
    board_typed = {j.block_id for j in board_lines}
    to_move = await run(_inbox_leftovers, client, state, ws)
    to_move += [
        j.block_id for j in covered if j.block_id in board_typed - placed.adopted
    ]  # a line that became its item in place stays where it was typed
    # Newest notes go to the top of ✓ Processed. Moving the batch last-first keeps it in
    # the order it was written.
    for block_id in reversed(to_move):
        try:
            await run(notion.move_block, client, block_id, ws.processed_id, True)
            report.moved += 1
        except UserFacingError as exc:
            log.warning("Couldn't move block %s: %s", block_id, exc)
            report.move_failures += 1

    # 6. Remember what we've seen, so a quiet hour costs two page lookups and nothing else.
    #    If a line was missed once (not parked yet), don't: the next sync retries it.
    retry = len(report.missed) > len(report.parked)
    if not retry:
        inbox_page = await run(client.pages.retrieve, page_id=ws.inbox_id)
        board_page = await run(client.pages.retrieve, page_id=ws.board_id)
        state.set(KEY_LAST_SEEN_EDIT, inbox_page["last_edited_time"])
        state.set(KEY_BOARD_SEEN, board_page["last_edited_time"])
    state.set(KEY_BOARD_DAY, day)
    state.set(KEY_ITEMS_STAMP, store.stamp())

    if report.updates:
        await notifier.send(updates_message(report.updates), Space.CHAT)
    if report.parked:
        lines = "\n".join(f"• {t}" for t in report.parked)
        await notifier.send(
            "I couldn't sort these notes twice, so I've left them where they are:\n"
            f"{lines}\n\nReword them in Notion and I'll try again, or delete them.",
            Space.CHAT,
        )
    return report
