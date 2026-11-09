"""Notion: the owner's 📥 Inbox (they write) and 🗂 Board (Ultron organises).

Both pages live under the owner's "Ultron" page (NOTION_ROOT_PAGE_ID). Calls
here are synchronous; async callers run them with `asyncio.to_thread`.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

from notion_client import (
    APIErrorCode,
    APIResponseError,
    Client,
    collect_paginated_api,
    extract_page_id,
)

from ultron.config import Settings
from ultron.errors import UserFacingError
from ultron.ops import activity
from ultron.state import State

log = logging.getLogger("notion_client")

TIMEOUT_MS = 20_000
# Notion refuses a text piece over 2,000 characters (counted in UTF-16 units, so an
# emoji counts 2) and more than 100 pieces in one block.
TEXT_LIMIT = 2000
MAX_TEXT_PARTS = 100

INBOX_TITLE = "Inbox"
INBOX_EMOJI = "📥"
INBOX_HINT = (
    "Jot anything here, in any format. Every hour Ultron sorts new notes onto the Board "
    "and folds the originals into ✓ Processed below, so your words are kept."
)
PROCESSED_TITLE = "✓ Processed"

BOARD_TITLE = "Board"
BOARD_EMOJI = "🗂"
BOARD_HINT = "Ultron keeps this page organised. Tick to-dos and edit freely: your changes win."
SCHEDULE_TITLE = "Schedule"
SCHEDULE_EMOJI = "📅"
SCHEDULE_HINT = (
    "Your plan, written by Ultron once you approve it. "
    "🔒 lines are your fixed schedule. Tick blocks as you go."
)

# SQLite kv keys
KEY_INBOX = "notion.inbox_page_id"
KEY_INBOX_HINT = "notion.inbox_hint_block_id"
KEY_PROCESSED = "notion.processed_block_id"
KEY_BOARD = "notion.board_page_id"
KEY_BOARD_HINT = "notion.board_hint_block_id"
KEY_SCHEDULE = "notion.schedule_page_id"
KEY_SCHEDULE_HINT = "notion.schedule_hint_block_id"


class NotionError(UserFacingError):
    pass


class LoggedClient(Client):
    """A Notion client that writes every change it makes to the activity log. Reads,
    database queries and searches aren't logged."""

    def request(self, path: str, method: str, query=None, body=None, form_data=None, auth=None):
        write = method.upper() != "GET" and not path.endswith("/query") and path != "search"
        try:
            result = super().request(path, method, query, body, form_data, auth)
        except Exception as exc:
            if write:
                _log_write(path, method, body, failed=f"{type(exc).__name__}: {exc}")
            raise
        if write:
            _log_write(path, method, body)
        return result


PAGE_NAMES = {
    KEY_INBOX: "📥 Inbox",
    KEY_BOARD: "🗂 Board",
    KEY_SCHEDULE: "📅 Schedule",
    KEY_PROCESSED: "✓ Processed",
}


def _texts(body: Any) -> list[str]:
    """The words in a request body: every `text.content`, in order."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            content = (
                node.get("text", {}).get("content") if isinstance(node.get("text"), dict) else None
            )
            if isinstance(content, str):
                found.append(content)
            for key, value in node.items():
                if key != "text":
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return found


def _target_name(object_id: str) -> str:
    """What a page or block id is, in words, from what Ultron knows about it."""
    db = activity.sink()
    if db is None:
        return object_id
    try:
        for key, name in PAGE_NAMES.items():
            row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            if row and row[0].replace("-", "") == object_id.replace("-", ""):
                return name
        row = db.execute(
            "SELECT kind, title FROM items WHERE board_block_id = ?", (object_id,)
        ).fetchone()
        if row:
            return f"{row[0]} '{activity.one_line(row[1], 60)}'"
        row = db.execute("SELECT key FROM board_blocks WHERE block_id = ?", (object_id,)).fetchone()
        if row:
            return f"Board heading {row[0]}"
    except Exception:  # noqa: BLE001 - a name is a nicety
        pass
    return f"block {object_id[:8]}"


def _log_write(path: str, method: str, body: Any, failed: str | None = None) -> None:
    parts = path.strip("/").split("/")
    target = _target_name(parts[1]) if len(parts) > 1 else ""
    method = method.upper()
    if parts[0] == "pages" and method == "POST":
        action = "created a page"
    elif parts[-1] == "children":
        count = len((body or {}).get("children", []))
        action = f"added {count} block(s) under {target}"
    elif method == "DELETE" or (body or {}).get("archived") or (body or {}).get("in_trash"):
        action = f"deleted {target}"
    elif parts[0] == "pages":
        action = f"updated page {target}"
    else:
        action = f"edited {target}"
    words = " | ".join(t for t in _texts(body) if t.strip())
    summary = f"Notion {action}" + (f": {activity.one_line(words, 150)}" if words else "")
    if failed:
        summary = f"FAILED {summary} ({activity.one_line(failed, 80)})"
    activity.record("notion", summary, text=words or None, detail={"method": method, "path": path})


def make_client(settings: Settings) -> Client:
    assert settings.notion_token
    # Pass our logger: without one, every new client adds another console handler to
    # notion_client's logger, so each warning ends up printed once per client ever made.
    return LoggedClient(
        auth=settings.notion_token.get_secret_value(),
        timeout_ms=TIMEOUT_MS,
        logger=log,
    )


def root_page_id(settings: Settings) -> str:
    """Accept either a bare page ID or the full page URL in NOTION_ROOT_PAGE_ID."""
    assert settings.notion_root_page_id
    page_id = extract_page_id(settings.notion_root_page_id)
    if not page_id:
        raise NotionError("NOTION_ROOT_PAGE_ID isn't a page ID or Notion page URL.")
    return page_id


def page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def check_access(settings: Settings) -> str:
    """Confirm the token works and the Ultron root page is shared with the integration."""
    client = make_client(settings)
    try:
        bot = client.users.me()
        client.pages.retrieve(page_id=root_page_id(settings))
    except APIResponseError as exc:
        if exc.code == APIErrorCode.Unauthorized:
            raise NotionError(
                "Notion rejected NOTION_TOKEN. Copy it again from notion.so/my-integrations."
            ) from exc
        if exc.code == APIErrorCode.ObjectNotFound:
            raise NotionError(
                "Token works, but the Ultron page isn't visible to it. Open the page in Notion → "
                "••• → Connections → add your integration."
            ) from exc
        raise
    finally:
        client.close()
    return f"integration '{bot.get('name', 'unknown')}' can see the Ultron page"


# --- block helpers ---


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Cut text into pieces Notion accepts; it shows them joined, so nothing looks different."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1  # outside the BMP: a UTF-16 surrogate pair
        if size + width > limit:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    pieces.append("".join(current))
    return pieces


def rich_text(text: str) -> list[dict[str, Any]]:
    # One part short of the cap leaves room for the Board's "(?) question" suffix
    pieces = split_text(text)[: MAX_TEXT_PARTS - 1]
    return [{"type": "text", "text": {"content": piece}} for piece in pieces]


def plain_text(rich: list[dict[str, Any]]) -> str:
    return "".join(
        part.get("plain_text") or part.get("text", {}).get("content", "") for part in rich
    )


def callout(text: str, emoji: str) -> dict[str, Any]:
    return {
        "type": "callout",
        "callout": {"rich_text": rich_text(text), "icon": {"type": "emoji", "emoji": emoji}},
    }


def toggle(text: str) -> dict[str, Any]:
    return {"type": "toggle", "toggle": {"rich_text": rich_text(text)}}


def block_text(block: dict[str, Any]) -> str:
    """The visible text of a block, whatever its type ('' if it has none)."""
    if block["type"] == "child_page":
        return block["child_page"]["title"]
    return plain_text(block.get(block["type"], {}).get("rich_text", []))


def is_alive(obj: dict[str, Any]) -> bool:
    return not obj.get("in_trash") and not obj.get("archived")


def list_children(client: Client, block_id: str) -> list[dict[str, Any]]:
    return collect_paginated_api(client.blocks.children.list, block_id=block_id)


def _exists(fetch, object_id: str | None) -> bool:
    if not object_id:
        return False
    try:
        return is_alive(fetch(object_id))
    except APIResponseError as exc:
        if exc.code == APIErrorCode.ObjectNotFound:
            return False
        raise


def page_exists(client: Client, page_id: str | None) -> bool:
    return _exists(lambda i: client.pages.retrieve(page_id=i), page_id)


def block_exists(client: Client, block_id: str | None) -> bool:
    return _exists(lambda i: client.blocks.retrieve(block_id=i), block_id)


# --- reading blocks as text ---

# Block types whose text Ultron reads, with the prefix used when rendering them.
TEXT_BLOCKS: dict[str, str] = {
    "paragraph": "",
    "bulleted_list_item": "- ",
    "numbered_list_item": "- ",
    "to_do": "",  # prefix depends on checked state
    "heading_1": "# ",
    "heading_2": "## ",
    "heading_3": "### ",
    "quote": "> ",
    "callout": "",
    "toggle": "",
    "code": "",
}
IGNORED_BLOCKS = {"divider", "table_of_contents", "breadcrumb", "column_list", "column"}
MAX_DEPTH = 3


def block_line(block: dict[str, Any]) -> str:
    kind = block["type"]
    text = block_text(block).strip()
    if kind == "to_do":
        return ("[x] " if block["to_do"].get("checked") else "[ ] ") + text
    return TEXT_BLOCKS.get(kind, "") + text


LINE_MARKER = re.compile(r"^\s*(?:\[[ xX]\] |- |> |#{1,3} )")


def strip_marker(line: str) -> str:
    """'[ ] Call mum' or '- Call mum' (how block_line shows a block) -> 'Call mum'."""
    return LINE_MARKER.sub("", line).strip()


def render_block(
    client: Client, block: dict[str, Any], depth: int = 0
) -> tuple[list[str], list[str]]:
    """Return (text lines, unsupported block types) for a block and its nested children."""
    kind = block["type"]
    if kind in IGNORED_BLOCKS:
        lines: list[str] = []
        unsupported: list[str] = []
    elif kind in TEXT_BLOCKS:
        line = block_line(block)
        lines = ["  " * depth + line] if line.strip() else []
        unsupported = []
    else:
        return [], [kind]  # images, files, links to pages... are left alone
    if block.get("has_children") and depth < MAX_DEPTH:
        for child in list_children(client, block["id"]):
            if is_alive(child):
                child_lines, child_unsupported = render_block(client, child, depth + 1)
                lines += child_lines
                unsupported += child_unsupported
    return lines, unsupported


# --- copying blocks (Notion has no "move block") ---

# Fields Notion accepts when creating each block type; everything else it returns
# on read (ids, timestamps, plain_text...) is dropped.
CREATE_FIELDS: dict[str, tuple[str, ...]] = {
    "to_do": ("rich_text", "checked", "color"),
    "heading_1": ("rich_text", "color", "is_toggleable"),
    "heading_2": ("rich_text", "color", "is_toggleable"),
    "heading_3": ("rich_text", "color", "is_toggleable"),
    "callout": ("rich_text", "icon", "color"),
    "code": ("rich_text", "language", "caption"),
}
DEFAULT_FIELDS = ("rich_text", "color")


def clean_rich_text(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild rich text for creating blocks; mentions and equations become plain text."""
    out = []
    for part in parts:
        text = part.get("text") if part.get("type", "text") == "text" else None
        content = text["content"] if text else part.get("plain_text", "")
        clean: dict[str, Any] = {"type": "text", "text": {"content": content}}
        if text and text.get("link"):
            clean["text"]["link"] = text["link"]
        if part.get("annotations"):
            clean["annotations"] = part["annotations"]
        out.append(clean)
    return out


def clean_block(block: dict[str, Any]) -> dict[str, Any]:
    kind = block["type"]
    body = block.get(kind, {})
    clean: dict[str, Any] = {}
    for key in CREATE_FIELDS.get(kind, DEFAULT_FIELDS):
        if key not in body:
            continue
        value = body[key]
        if key in ("rich_text", "caption"):
            value = clean_rich_text(value)
        if key == "icon" and isinstance(value, dict) and value.get("type") == "file":
            continue  # uploaded icons have expiring URLs and can't be recreated
        clean[key] = value
    return {"type": kind, kind: clean}


def copy_block(
    client: Client, block: dict[str, Any], parent_id: str, depth: int = 0, at_start: bool = False
) -> str:
    """Copy a text block and its nested children under `parent_id` (at the top if
    `at_start`, else at the end). Returns the copy's id."""
    kwargs: dict[str, Any] = {"block_id": parent_id, "children": [clean_block(block)]}
    if at_start:
        kwargs["position"] = {"type": "start"}
    # The reply may also list the blocks after the new one: the new block comes first
    copy_id = client.blocks.children.append(**kwargs)["results"][0]["id"]
    if block.get("has_children") and depth < MAX_DEPTH:
        for child in list_children(client, block["id"]):
            if is_alive(child) and child["type"] in TEXT_BLOCKS:
                copy_block(client, child, copy_id, depth + 1)
    return copy_id


class MoveError(UserFacingError):
    pass


def move_block(client: Client, block_id: str, parent_id: str, at_start: bool = False) -> str:
    """Copy a block into `parent_id`, check the copy reads the same, then delete the original.

    If the check fails the copy is removed and the original is left exactly where it was.
    """
    original = client.blocks.retrieve(block_id=block_id)
    copy_id = copy_block(client, original, parent_id, at_start=at_start)
    copy = client.blocks.retrieve(block_id=copy_id)
    if render_block(client, copy)[0] != render_block(client, original)[0]:
        client.blocks.delete(block_id=copy_id)
        raise MoveError("A note didn't copy cleanly, so it was left where it was.")
    client.blocks.delete(block_id=block_id)
    return copy_id


# --- workspace bootstrap ---


@dataclass(frozen=True)
class Workspace:
    inbox_id: str
    processed_id: str  # the ✓ Processed toggle at the bottom of the Inbox
    board_id: str
    inbox_hint_id: str | None  # optional: the owner may delete the hints
    board_hint_id: str | None
    schedule_id: str | None = None
    schedule_hint_id: str | None = None

    @property
    def inbox_url(self) -> str:
        return page_url(self.inbox_id)

    @property
    def board_url(self) -> str:
        return page_url(self.board_id)

    @property
    def schedule_url(self) -> str | None:
        return page_url(self.schedule_id) if self.schedule_id else None


@dataclass(frozen=True)
class EnsureResult:
    workspace: Workspace
    created: list[str]  # human-readable list of what was created this run


def _title_matches(title: str, wanted: str) -> bool:
    # Accept "Inbox", "inbox " or "📥 Inbox" (emoji typed into the title)
    words = title.strip().casefold().split()
    return wanted.casefold() in (" ".join(words), " ".join(words[1:]))


def _find_child_page(client: Client, root_id: str, title: str) -> str | None:
    for block in list_children(client, root_id):
        if (
            block["type"] == "child_page"
            and is_alive(block)
            and _title_matches(block_text(block), title)
        ):
            return block["id"]
    return None


def _create_page(client: Client, root_id: str, title: str, emoji: str, children: list) -> str:
    page = client.pages.create(
        parent={"page_id": root_id},
        icon={"type": "emoji", "emoji": emoji},
        properties={"title": {"title": rich_text(title)}},
        children=children,
    )
    return page["id"]


def _ensure_page(
    client: Client, state: State, root_id: str, key: str, title: str, emoji: str, children: list
) -> tuple[str, str]:
    """Return (page_id, how): reuse the stored page ("kept"), else adopt an existing
    child page with the same title ("adopted"), else create one ("created")."""
    page_id = state.get(key)
    if page_exists(client, page_id):
        return page_id, "kept"
    page_id = _find_child_page(client, root_id, title)
    how = "adopted"
    if page_id is None:
        page_id, how = _create_page(client, root_id, title, emoji, children), "created"
    state.set(key, page_id)
    return page_id, how


def _find_block(blocks: list[dict], block_type: str, text: str | None = None) -> str | None:
    for block in blocks:
        if block["type"] == block_type and is_alive(block):
            if text is None or block_text(block).strip().casefold() == text.casefold():
                return block["id"]
    return None


def ensure_workspace(client: Client, state: State, root_id: str) -> EnsureResult:
    """Make sure the Inbox, Board and Schedule exist. Safe to run any number of times."""
    created: list[str] = []

    inbox_id, inbox_how = _ensure_page(
        client,
        state,
        root_id,
        KEY_INBOX,
        INBOX_TITLE,
        INBOX_EMOJI,
        [callout(INBOX_HINT, "💡"), toggle(PROCESSED_TITLE)],
    )
    board_id, board_how = _ensure_page(
        client, state, root_id, KEY_BOARD, BOARD_TITLE, BOARD_EMOJI, [callout(BOARD_HINT, "🗂")]
    )
    schedule_id, schedule_how = _ensure_page(
        client,
        state,
        root_id,
        KEY_SCHEDULE,
        SCHEDULE_TITLE,
        SCHEDULE_EMOJI,
        [callout(SCHEDULE_HINT, SCHEDULE_EMOJI)],
    )
    for how, label in (
        (inbox_how, "📥 Inbox page"),
        (board_how, "🗂 Board page"),
        (schedule_how, "📅 Schedule page"),
    ):
        if how == "created":
            created.append(label)

    inbox_blocks = list_children(client, inbox_id)
    # When the page changed, forget block ids from the old page. Hints are only
    # recognised by their exact text, so the owner's own callouts are never skipped.
    if inbox_how != "kept":
        state.set(KEY_INBOX_HINT, _find_block(inbox_blocks, "callout", INBOX_HINT) or "")
    if board_how != "kept":
        board_blocks = list_children(client, board_id)
        state.set(KEY_BOARD_HINT, _find_block(board_blocks, "callout", BOARD_HINT) or "")
    if schedule_how != "kept":
        schedule_blocks = list_children(client, schedule_id)
        hint = _find_block(schedule_blocks, "callout", SCHEDULE_HINT)
        state.set(KEY_SCHEDULE_HINT, hint or "")

    # The ✓ Processed toggle is required; hints are only added when a page is first created.
    processed_id = state.get(KEY_PROCESSED)
    if inbox_how != "kept" or not block_exists(client, processed_id):
        processed_id = _find_block(inbox_blocks, "toggle", PROCESSED_TITLE)
        if processed_id is None:
            result = client.blocks.children.append(
                block_id=inbox_id, children=[toggle(PROCESSED_TITLE)]
            )
            processed_id = result["results"][0]["id"]
            created.append("✓ Processed section in the Inbox")
        state.set(KEY_PROCESSED, processed_id)

    return EnsureResult(
        workspace=Workspace(
            inbox_id=inbox_id,
            processed_id=processed_id,
            board_id=board_id,
            inbox_hint_id=state.get(KEY_INBOX_HINT) or None,
            board_hint_id=state.get(KEY_BOARD_HINT) or None,
            schedule_id=schedule_id,
            schedule_hint_id=state.get(KEY_SCHEDULE_HINT) or None,
        ),
        created=created,
    )


def ensure_workspace_for(settings: Settings, state: State) -> EnsureResult:
    """Convenience wrapper: build a client from settings, ensure, close."""
    client = make_client(settings)
    try:
        return ensure_workspace(client, state, root_page_id(settings))
    except APIResponseError as exc:
        if exc.code == APIErrorCode.ObjectNotFound:
            raise NotionError(
                "The Ultron page isn't visible to the integration. Open it in Notion → "
                "••• → Connections → add your integration."
            ) from exc
        raise
    finally:
        client.close()
