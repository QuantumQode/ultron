"""Read new jottings from the owner's 📥 Inbox as plain text.

Everything on the Inbox page counts, above or below ✓ Processed (people often
type at the bottom), except Ultron's hint, the ✓ Processed toggle itself, and
blocks already handled. Images, files and links to other pages are reported
but not read.

Lines in a row, with no blank line between them, are one note: they're sorted
together and wait together. A note any line of which was edited in the last
SETTLE minutes is left for the next sync: the Notion phone app saves typing in
chunks, and taking a note mid-way (it's then moved out of the Inbox) loses the
rest of it.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ultron.integrations.notion import Workspace, is_alive, list_children, render_block
from ultron.state import State

KEY_LAST_SEEN_EDIT = "inbox.last_seen_edit"
SETTLE = timedelta(minutes=15)  # a note must sit untouched this long before it's taken


@dataclass(frozen=True)
class Jotting:
    block_id: str  # top-level Inbox block this text came from
    text: str
    heading: str | None = None  # typed on the Board under this heading
    group: str | None = None  # ...which is this Board group ("project:Robin"): it stays there
    on_board: bool = False  # typed straight onto the 🗂 Board, not the Inbox
    kinds: tuple[str, ...] = ()  # Board: the kinds the section it's in holds ("task",)
    above: str | None = None  # Board: the item right above it, e.g. a to-do it may belong to
    section: str = ""  # Board: the key of the section it was typed in ("todo")
    note: int | None = None  # Inbox: lines with the same number were written as one note


@dataclass(frozen=True)
class InboxRead:
    changed: bool  # False: the Inbox wasn't edited since the last sync, nothing was read
    last_edited: str  # the Inbox's last_edited_time, saved once the sync succeeds
    jottings: list[Jotting] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)  # block types left untouched
    held: int = 0  # lines of notes still being written, left for the next sync


def _edited(block: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(block["last_edited_time"].replace("Z", "+00:00"))


def read_inbox(
    client: Any,
    state: State,
    ws: Workspace,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> InboxRead:
    """Return new jottings. Cheap when nothing changed: one page lookup, no block reads."""
    last_edited = client.pages.retrieve(page_id=ws.inbox_id)["last_edited_time"]
    if not force and state.get(KEY_LAST_SEEN_EDIT) == last_edited:
        return InboxRead(changed=False, last_edited=last_edited)

    settled_before = (now or datetime.now(UTC)) - SETTLE
    skip = {ws.processed_id, ws.inbox_hint_id} | state.inbox_handled()
    notes: list[list[Jotting]] = [[]]
    recent: list[bool] = [False]  # per note: a line was edited within SETTLE
    unsupported: list[str] = []
    for block in list_children(client, ws.inbox_id):
        lines: list[str] = []
        if block["id"] not in skip and is_alive(block):
            lines, block_unsupported = render_block(client, block)
            unsupported += block_unsupported
        if not lines:  # a blank line, the hint, ✓ Processed, a picture...: the note ends
            if notes[-1]:
                notes.append([])
                recent.append(False)
            continue
        n = len(notes)
        notes[-1].append(Jotting(block_id=block["id"], text="\n".join(lines), note=n))
        recent[-1] |= _edited(block) > settled_before

    jottings = [j for note, fresh in zip(notes, recent, strict=True) if not fresh for j in note]
    held = sum(len(note) for note, fresh in zip(notes, recent, strict=True) if fresh)
    return InboxRead(
        changed=True,
        last_edited=last_edited,
        jottings=jottings,
        unsupported=unsupported,
        held=held,
    )


def mark_synced(state: State, read: InboxRead) -> None:
    """Call after the jottings were sorted successfully, so they're never handled twice."""
    state.mark_inbox_handled([j.block_id for j in read.jottings])
    if not read.held:  # a held note must be read again next time
        state.set(KEY_LAST_SEEN_EDIT, read.last_edited)
