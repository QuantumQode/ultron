import json
import sqlite3
from datetime import date

import pytest
from conftest import make_settings
from fake_notion import FakeNotion

from ultron.integrations.notion import KEY_BOARD, KEY_INBOX, KEY_SCHEDULE
from ultron.ops.backup import make_backup, prune, run_backup
from ultron.state import State

TODAY = date(2026, 10, 22)


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "data" / "ultron.db")
    s.set("marker", "hello")
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path):
    return make_settings(backup_dir=tmp_path / "backups")


@pytest.fixture
def fake(state):
    fake = FakeNotion()
    inbox = fake.add_page(fake.root, "Inbox")
    board = fake.add_page(fake.root, "Board")
    fake.add_text(inbox, "paragraph", "call mum")
    section = fake.add_text(board, "toggle", "To-do")
    group = fake.add_text(section, "toggle", "Acme")
    fake.add_text(group, "to_do", "Send invoice", checked=False)
    fake.add_page(board, "Some sub-page")  # not exported into
    state.set(KEY_INBOX, inbox)
    state.set(KEY_BOARD, board)
    return fake


def test_backup_copies_database_and_notion_pages(settings, state, fake):
    report = make_backup(settings, state, TODAY, client=fake)

    folder = settings.backup_dir / "2026-10-22"
    assert report.folder == folder
    copy = sqlite3.connect(folder / "ultron.db")
    assert copy.execute("SELECT value FROM kv WHERE key = 'marker'").fetchone() == ("hello",)
    copy.close()

    pages = json.loads((folder / "notion.json").read_text(encoding="utf-8"))["pages"]
    assert set(pages) == {"inbox", "board"}  # no Schedule page stored yet
    todo = pages["board"]["children"][0]
    assert todo["children"][0]["children"][0]["to_do"]["rich_text"][0]["plain_text"] == (
        "Send invoice"
    )
    assert report.blocks == {"inbox": 1, "board": 4}
    assert "board 4" in report.summary


def test_backup_never_writes_to_notion(settings, state, fake):
    make_backup(settings, state, TODAY, client=fake)
    assert all(call in {"pages.retrieve", "blocks.children.list"} for call in fake.calls)


def test_missing_page_fails_and_leaves_no_dated_folder(settings, state, fake):
    state.set(KEY_SCHEDULE, "page-gone")
    with pytest.raises(Exception, match="page-gone"):
        make_backup(settings, state, TODAY, client=fake)
    assert not (settings.backup_dir / "2026-10-22").exists()


def test_same_day_backup_replaces_the_first(settings, state, fake):
    make_backup(settings, state, TODAY, client=fake)
    state.set("marker", "later")
    make_backup(settings, state, TODAY, client=fake)
    copy = sqlite3.connect(settings.backup_dir / "2026-10-22" / "ultron.db")
    assert copy.execute("SELECT value FROM kv WHERE key = 'marker'").fetchone() == ("later",)
    copy.close()
    assert [p.name for p in settings.backup_dir.iterdir()] == ["2026-10-22"]


def test_without_notion_only_the_database_is_saved(settings, state):
    report = make_backup(settings, state, TODAY)
    assert (report.folder / "ultron.db").exists()
    assert not (report.folder / "notion.json").exists()
    assert report.blocks == {}


def test_prune_keeps_the_last_14_days(tmp_path):
    for name in ["2026-10-08", "2026-10-09", "2026-10-22", "2026-10-21.partial", "notes"]:
        (tmp_path / name).mkdir()
    (tmp_path / "2026-10-01").write_text("a file, not a backup")

    removed = prune(tmp_path, TODAY, keep_days=14)

    assert removed == ["2026-10-08", "2026-10-21.partial"]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["2026-10-01", "2026-10-09", "2026-10-22", "notes"]


async def test_run_backup_uses_todays_date(settings, state, fake):
    report = await run_backup(settings, state, client=fake, today=TODAY)
    assert report.folder.name == "2026-10-22"
