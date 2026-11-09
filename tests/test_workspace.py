import pytest
from conftest import make_settings
from fake_notion import FakeNotion

from ultron.features import workspace as workspace_feature
from ultron.integrations import notion
from ultron.integrations.notion import (
    BOARD_HINT,
    INBOX_HINT,
    PROCESSED_TITLE,
    SCHEDULE_HINT,
    block_text,
    ensure_workspace,
)
from ultron.notify import Space
from ultron.state import State


@pytest.fixture
def state():
    s = State(":memory:")
    yield s
    s.close()


@pytest.fixture
def fake():
    return FakeNotion()


def titles(fake, parent):
    return [block_text(b) for b in fake.child_blocks(parent)]


def test_first_run_creates_all_three_pages(fake, state):
    result = ensure_workspace(fake, state, fake.root)
    ws = result.workspace
    assert result.created == ["📥 Inbox page", "🗂 Board page", "📅 Schedule page"]
    assert titles(fake, fake.root) == ["Inbox", "Board", "Schedule"]
    assert fake.objects[ws.inbox_id]["icon"]["emoji"] == "📥"
    # Inbox: hint callout on top, ✓ Processed toggle at the bottom
    assert titles(fake, ws.inbox_id) == [INBOX_HINT, PROCESSED_TITLE]
    assert fake.objects[ws.processed_id]["type"] == "toggle"
    assert ws.inbox_hint_id == fake.children[ws.inbox_id][0]
    assert titles(fake, ws.board_id) == [BOARD_HINT]
    assert ws.board_hint_id is not None
    assert titles(fake, ws.schedule_id) == [SCHEDULE_HINT] and ws.schedule_hint_id is not None


def test_second_run_changes_nothing(fake, state):
    first = ensure_workspace(fake, state, fake.root).workspace
    fake.calls.clear()
    second = ensure_workspace(fake, state, fake.root)
    assert second.created == []
    assert second.workspace == first
    assert "pages.create" not in fake.calls and "blocks.children.append" not in fake.calls


def test_trashed_inbox_is_recreated(fake, state):
    first = ensure_workspace(fake, state, fake.root).workspace
    fake.trash(first.inbox_id)
    result = ensure_workspace(fake, state, fake.root)
    assert result.created == ["📥 Inbox page"]
    assert result.workspace.inbox_id != first.inbox_id
    assert result.workspace.processed_id != first.processed_id
    assert result.workspace.board_id == first.board_id


def test_adopts_owners_existing_inbox_and_adds_processed(fake, state):
    mine = fake.add_page(fake.root, "📥 inbox")
    my_callout = fake.add_block(mine, notion.callout("Remember: these are MY thoughts", "⭐"))
    result = ensure_workspace(fake, state, fake.root)
    ws = result.workspace
    assert ws.inbox_id == mine
    assert result.created == [
        "🗂 Board page",
        "📅 Schedule page",
        "✓ Processed section in the Inbox",
    ]
    # The owner's own callout is not mistaken for Ultron's hint
    assert ws.inbox_hint_id is None
    assert fake.children[mine] == [my_callout, ws.processed_id]


def test_deleted_processed_toggle_is_restored(fake, state):
    first = ensure_workspace(fake, state, fake.root).workspace
    fake.objects[first.processed_id]["archived"] = True
    result = ensure_workspace(fake, state, fake.root)
    assert result.created == ["✓ Processed section in the Inbox"]
    assert result.workspace.processed_id != first.processed_id


def test_deleted_hint_is_not_re_added(fake, state):
    first = ensure_workspace(fake, state, fake.root).workspace
    fake.objects[first.inbox_hint_id]["archived"] = True
    result = ensure_workspace(fake, state, fake.root)
    assert result.created == []


def test_root_page_not_shared(fake, state):
    from fake_notion import not_found

    with pytest.raises(type(not_found("x"))):
        ensure_workspace(fake, state, "missing-root")


async def test_setup_workspace_announces_new_pages(monkeypatch, fake, state):
    sent = []

    class Notifier:
        async def send(self, text, space=Space.BRIEFS):
            sent.append((text, space))

    monkeypatch.setattr(notion, "make_client", lambda settings: _Closable(fake))
    settings = make_settings(notion_token="n", notion_root_page_id=fake.root)
    monkeypatch.setattr(notion, "root_page_id", lambda s: fake.root)

    await workspace_feature.setup_workspace(settings, state, Notifier())
    assert len(sent) == 1 and sent[0][1] is Space.CHAT
    assert "📥 Inbox: https://www.notion.so/" in sent[0][0]

    await workspace_feature.setup_workspace(settings, state, Notifier())
    assert len(sent) == 1  # nothing new, no second message


class _Closable:
    """Wrap the fake so ensure_workspace_for can close() it."""

    def __init__(self, fake):
        self._fake = fake

    def __getattr__(self, name):
        return getattr(self._fake, name)

    def close(self):
        pass
