"""Nightly backups: a copy of the SQLite database plus a JSON export of the Notion pages.

Each night gets its own folder, `backups/YYYY-MM-DD/` holding `ultron.db` and
`notion.json`. It's written under a temporary name and renamed when complete, so a
folder with a date name is always a whole backup. Folders older than the keep
period are deleted. Notion is only read, never written.
"""

import asyncio
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ultron.config import Settings
from ultron.integrations.notion import (
    KEY_BOARD,
    KEY_INBOX,
    KEY_SCHEDULE,
    list_children,
    make_client,
)
from ultron.state import State

PAGES = {"inbox": KEY_INBOX, "board": KEY_BOARD, "schedule": KEY_SCHEDULE}
# Sub-pages and databases are separate Notion objects; their contents aren't Ultron's
SKIP_CHILDREN = {"child_page", "child_database"}


@dataclass(frozen=True)
class BackupReport:
    folder: Path
    db_bytes: int
    blocks: dict[str, int] = field(default_factory=dict)  # blocks exported per page
    removed: list[str] = field(default_factory=list)  # old backup folders deleted

    @property
    def summary(self) -> str:
        pages = ", ".join(f"{name} {count}" for name, count in self.blocks.items()) or "none"
        return (
            f"Backup {self.folder.name}: database {self.db_bytes // 1024} KB, "
            f"Notion blocks: {pages}, removed {len(self.removed)} old"
        )


def backup_database(state: State, dest: Path) -> int:
    """Copy the live database with SQLite's online backup, safe while the bot writes."""
    target = sqlite3.connect(dest)
    try:
        state.db.backup(target)
    finally:
        target.close()
    return dest.stat().st_size


def export_blocks(client: Any, block_id: str) -> tuple[list[dict[str, Any]], int]:
    """All blocks under `block_id`, nested children included. Returns (blocks, count)."""
    blocks = list_children(client, block_id)
    count = len(blocks)
    for block in blocks:
        if block.get("has_children") and block.get("type") not in SKIP_CHILDREN:
            block["children"], nested = export_blocks(client, block["id"])
            count += nested
    return blocks, count


def export_notion(client: Any, state: State) -> tuple[dict[str, Any], dict[str, int]]:
    """The Inbox, Board and Schedule pages with all their blocks, by page name."""
    export: dict[str, Any] = {}
    counts: dict[str, int] = {}
    for name, key in PAGES.items():
        page_id = state.get(key)
        if not page_id:
            continue
        page = client.pages.retrieve(page_id=page_id)
        page["children"], counts[name] = export_blocks(client, page_id)
        export[name] = page
    return export, counts


def prune(backup_dir: Path, today: date, keep_days: int) -> list[str]:
    """Delete dated backup folders older than `keep_days`, plus leftover partial ones."""
    oldest = today - timedelta(days=keep_days - 1)
    removed = []
    for folder in sorted(backup_dir.iterdir()):
        if not folder.is_dir():
            continue
        if folder.name.endswith(".partial"):
            stale = folder.name != f"{today.isoformat()}.partial"
        else:
            try:
                stale = date.fromisoformat(folder.name) < oldest
            except ValueError:
                continue  # not one of ours
        if stale:
            shutil.rmtree(folder)
            removed.append(folder.name)
    return removed


def make_backup(
    settings: Settings, state: State, today: date, *, client: Any | None = None
) -> BackupReport:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    final = settings.backup_dir / today.isoformat()
    partial = settings.backup_dir / f"{today.isoformat()}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()

    db_bytes = backup_database(state, partial / "ultron.db")
    counts: dict[str, int] = {}
    if settings.notion_enabled or client is not None:
        own_client = client is None
        client = client or make_client(settings)
        try:
            export, counts = export_notion(client, state)
        finally:
            if own_client:
                client.close()
        exported_at = datetime.now(settings.zone).isoformat()
        (partial / "notion.json").write_text(
            json.dumps({"exported_at": exported_at, "pages": export}, ensure_ascii=False),
            encoding="utf-8",
        )

    if final.exists():  # a second backup on the same day replaces the first
        shutil.rmtree(final)
    partial.rename(final)
    removed = prune(settings.backup_dir, today, settings.backup_keep_days)
    return BackupReport(folder=final, db_bytes=db_bytes, blocks=counts, removed=removed)


async def run_backup(
    settings: Settings, state: State, *, client: Any | None = None, today: date | None = None
) -> BackupReport:
    today = today or datetime.now(settings.zone).date()
    return await asyncio.to_thread(make_backup, settings, state, today, client=client)
