import logging

import pytest
from conftest import make_settings
from fake_notion import FakeNotion
from notion_client import APIResponseError

from ultron.integrations.notion import make_client, plain_text, rich_text, split_text


def test_split_text_keeps_pieces_within_the_limit():
    assert split_text("") == [""]
    assert split_text("a" * 2000) == ["a" * 2000]
    assert split_text("a" * 2001) == ["a" * 2000, "a"]


def test_split_text_never_cuts_an_emoji_in_half():
    # An emoji is 2 UTF-16 units: it can't start at unit 2,000 of a 2,000-unit piece
    pieces = split_text("a" * 1999 + "🔥" + "b")
    assert pieces == ["a" * 1999, "🔥b"]


def test_rich_text_splits_long_text_and_joins_back():
    text = "word " * 1000
    parts = rich_text(text)
    assert len(parts) == 3
    assert plain_text(parts) == text


def test_rich_text_stays_under_notions_part_limit():
    parts = rich_text("x" * 2000 * 150)
    assert len(parts) == 99  # one left for a "(?) question" part


def test_fake_notion_refuses_text_over_the_limit():
    fake = FakeNotion()
    page = fake.add_page(fake.root, "Board")
    with pytest.raises(APIResponseError, match="should be ≤ `2000`"):
        fake.add_text(page, "paragraph", "x" * 2001)
    assert fake.children[page] == []


def test_making_clients_does_not_add_log_handlers():
    settings = make_settings(notion_token="secret", notion_root_page_id="root")
    logger = logging.getLogger("notion_client")
    before = len(logger.handlers)
    for _ in range(3):
        make_client(settings).close()
    assert len(logger.handlers) == before
