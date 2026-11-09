import pytest
from fake_notion import FakeNotion

from ultron.features.inbox import mark_synced, read_inbox
from ultron.integrations.notion import ensure_workspace
from ultron.state import State


@pytest.fixture
def state():
    s = State(":memory:")
    yield s
    s.close()


@pytest.fixture
def fake():
    return FakeNotion()


@pytest.fixture
def ws(fake, state):
    return ensure_workspace(fake, state, fake.root).workspace


def texts(read):
    return [j.text for j in read.jottings]


def test_empty_inbox_has_no_jottings(fake, state, ws):
    read = read_inbox(fake, state, ws)
    assert read.changed and read.jottings == [] and read.unsupported == []


def test_reads_mixed_blocks_above_and_below_processed(fake, state, ws):
    # The Inbox already has the hint and ✓ Processed; typing at the bottom lands below it.
    fake.add_text(ws.inbox_id, "paragraph", "dentist tues??")
    fake.add_text(ws.inbox_id, "to_do", "gym 3x this week", checked=False)
    fake.add_text(ws.inbox_id, "to_do", "renew passport", checked=True)
    fake.add_text(ws.inbox_id, "heading_2", "Ideas")
    fake.add_text(ws.inbox_id, "bulleted_list_item", "budgeting app side project")
    fake.add_text(ws.inbox_id, "quote", "feeling tired after late shifts")
    assert texts(read_inbox(fake, state, ws)) == [
        "dentist tues??",
        "[ ] gym 3x this week",
        "[x] renew passport",
        "## Ideas",
        "- budgeting app side project",
        "> feeling tired after late shifts",
    ]


def test_nested_lines_stay_with_their_parent(fake, state, ws):
    parent = fake.add_text(ws.inbox_id, "bulleted_list_item", "mum bday 12th")
    fake.add_text(parent, "bulleted_list_item", "get gift")
    child = fake.add_text(parent, "bulleted_list_item", "ideas:")
    fake.add_text(child, "paragraph", "scarf or book")
    [jotting] = read_inbox(fake, state, ws).jottings
    assert jotting.block_id == parent
    assert jotting.text == "- mum bday 12th\n  - get gift\n  - ideas:\n    scarf or book"


def test_skips_blank_lines_dividers_and_processed_contents(fake, state, ws):
    fake.add_text(ws.inbox_id, "paragraph", "   ")
    fake.add_block(ws.inbox_id, {"type": "divider", "divider": {}})
    fake.add_text(ws.processed_id, "paragraph", "an old note already sorted")
    fake.add_text(ws.inbox_id, "paragraph", "call the accountant")
    assert texts(read_inbox(fake, state, ws)) == ["call the accountant"]


def test_owner_callouts_are_read_but_the_hint_is_not(fake, state, ws):
    fake.add_block(
        ws.inbox_id,
        {
            "type": "callout",
            "callout": {"rich_text": [{"plain_text": "big decision: move flat?"}]},
        },
    )
    assert texts(read_inbox(fake, state, ws)) == ["big decision: move flat?"]


def test_images_and_files_are_reported_not_read(fake, state, ws):
    fake.add_block(ws.inbox_id, {"type": "image", "image": {}})
    note = fake.add_text(ws.inbox_id, "paragraph", "receipt below")
    fake.add_block(note, {"type": "pdf", "pdf": {}})
    read = read_inbox(fake, state, ws)
    assert texts(read) == ["receipt below"]
    assert sorted(read.unsupported) == ["image", "pdf"]


def test_unchanged_inbox_is_not_read_again(fake, state, ws):
    fake.add_text(ws.inbox_id, "paragraph", "buy milk")
    first = read_inbox(fake, state, ws)
    mark_synced(state, first)

    fake.calls.clear()
    second = read_inbox(fake, state, ws)
    assert not second.changed and second.jottings == []
    assert fake.calls == ["pages.retrieve"]  # one cheap lookup, no block reads


def test_handled_jottings_are_skipped_even_if_still_in_the_inbox(fake, state, ws):
    fake.add_text(ws.inbox_id, "paragraph", "buy milk")
    mark_synced(state, read_inbox(fake, state, ws))  # sorted, but (say) moving it failed
    fake.add_text(ws.inbox_id, "paragraph", "book flights")
    assert texts(read_inbox(fake, state, ws)) == ["book flights"]


def test_force_reads_even_when_unchanged(fake, state, ws):
    fake.add_text(ws.inbox_id, "paragraph", "buy milk")
    read = read_inbox(fake, state, ws)
    state.set("inbox.last_seen_edit", read.last_edited)  # seen, but not handled
    assert texts(read_inbox(fake, state, ws, force=True)) == ["buy milk"]


def test_trashed_blocks_are_ignored(fake, state, ws):
    gone = fake.add_text(ws.inbox_id, "paragraph", "deleted thought")
    fake.objects[gone]["archived"] = True
    assert read_inbox(fake, state, ws).jottings == []
