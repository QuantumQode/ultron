"""Read new jottings from the owner's 📥 Inbox as plain text.

Everything on the Inbox page counts, above or below ✓ Processed (people often
type at the bottom), except Ultron's hint, the ✓ Processed toggle itself, and
blocks already handled. Images, files and links to other pages are reported
but not read.
"""

from dataclasses import dataclass, field
from typing import Any

from ultron.integrations.notion import Workspace, is_alive, list_children, render_block
from ultron.state import State

KEY_LAST_SEEN_EDIT = "inbox.last_seen_edit"


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


@dataclass(frozen=True)
class InboxRead:
    changed: bool  # False: the Inbox wasn't edited since the last sync, nothing was read
    last_edited: str  # the Inbox's last_edited_time, saved once the sync succeeds
    jottings: list[Jotting] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)  # block types left untouched


def read_inbox(client: Any, state: State, ws: Workspace, *, force: bool = False) -> InboxRead:
    """Return new jottings. Cheap when nothing changed: one page lookup, no block reads."""
    last_edited = client.pages.retrieve(page_id=ws.inbox_id)["last_edited_time"]
    if not force and state.get(KEY_LAST_SEEN_EDIT) == last_edited:
        return InboxRead(changed=False, last_edited=last_edited)

    skip = {ws.processed_id, ws.inbox_hint_id} | state.inbox_handled()
    jottings: list[Jotting] = []
    unsupported: list[str] = []
    for block in list_children(client, ws.inbox_id):
        if block["id"] in skip or not is_alive(block):
            continue
        lines, block_unsupported = render_block(client, block)
        unsupported += block_unsupported
        if lines:
            jottings.append(Jotting(block_id=block["id"], text="\n".join(lines)))
    return InboxRead(
        changed=True, last_edited=last_edited, jottings=jottings, unsupported=unsupported
    )


def mark_synced(state: State, read: InboxRead) -> None:
    """Call after the jottings were sorted successfully, so they're never handled twice."""
    state.mark_inbox_handled([j.block_id for j in read.jottings])
    state.set(KEY_LAST_SEEN_EDIT, read.last_edited)
