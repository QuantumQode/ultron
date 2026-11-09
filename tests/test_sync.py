import re
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import make_settings
from fake_notion import FakeNotion

from ultron.features.items import ItemStore
from ultron.features.sorter import SortedItem, SortResult
from ultron.features.sync import run_sync
from ultron.integrations import notion
from ultron.integrations.notion import ensure_workspace
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=LONDON)


class SortingLLM:
    """Fake Claude: every note line becomes a task, except lines containing 'MISS'."""

    def __init__(self):
        self.calls = 0
        self.messages = self

    async def parse(self, **kwargs):
        self.calls += 1
        notes = kwargs["messages"][0]["content"].split("<notes>\n")[1]
        items = []
        for n, text in re.findall(r"^\[(\d+)\] (.*)$", notes, re.MULTILINE):
            if "MISS" not in text:
                items.append(
                    SortedItem(
                        kind="task",
                        title=re.sub(r'^\(under "[^"]*"\) ', "", text).lstrip("- "),
                        details=None,
                        project=None,
                        area="Admin",
                        due=None,
                        on_day=False,
                        status="open",
                        when=None,
                        question=None,
                        same_as=None,
                        lines=[int(n)],
                    )
                )
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200)
        parsed = SortResult(projects=[], items=items, updates=[], labels=[])
        return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, usage=usage)


class Notifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, space=Space.BRIEFS):
        self.sent.append((text, space))


@pytest.fixture
def env():
    fake = FakeNotion()
    state = State(":memory:")
    ws = ensure_workspace(fake, state, fake.root).workspace
    settings = make_settings(notion_token="n", notion_root_page_id=fake.root)

    async def notify(_):
        pass

    guard = CostGuard(state, cap_usd=20, zone=LONDON, notify=notify)
    llm = SortingLLM()

    @asynccontextmanager
    async def make_llm():
        yield llm

    env = SimpleNamespace(
        fake=fake,
        state=state,
        ws=ws,
        settings=settings,
        guard=guard,
        llm=llm,
        make_llm=make_llm,
        notifier=Notifier(),
        store=ItemStore(state),
    )
    yield env
    state.close()


async def sync(env, **kw):
    original = notion.root_page_id
    notion.root_page_id = lambda s: env.fake.root
    try:
        return await run_sync(
            env.settings,
            env.state,
            env.guard,
            env.notifier,
            env.make_llm,
            client=env.fake,
            today=NOW,
            **kw,
        )
    finally:
        notion.root_page_id = original


def board_tasks(env):
    return [t for t in env.fake.texts(env.ws.board_id) if t.startswith("[ ]")]


async def test_notes_are_sorted_onto_the_board_and_tidied_away(env):
    env.fake.add_text(env.ws.inbox_id, "paragraph", "change bedsheets")
    env.fake.add_text(env.ws.inbox_id, "paragraph", "call 3 to change plan")

    report = await sync(env)
    assert report.sorted_lines == 2 and report.items_saved == 2 and report.moved == 2
    assert board_tasks(env) == ["[ ] change bedsheets", "[ ] call 3 to change plan"]
    # The Inbox is clean: only the hint and ✓ Processed, with the originals inside it
    assert env.fake.texts(env.ws.inbox_id)[1:] == ["✓ Processed"]
    assert env.fake.texts(env.ws.processed_id) == ["change bedsheets", "call 3 to change plan"]
    assert env.llm.calls == 1

    # A later batch goes above the earlier one, still in the order it was written
    env.fake.add_text(env.ws.inbox_id, "paragraph", "book dentist")
    env.fake.add_text(env.ws.inbox_id, "paragraph", "renew passport")
    await sync(env)
    assert env.fake.texts(env.ws.processed_id) == [
        "book dentist",
        "renew passport",
        "change bedsheets",
        "call 3 to change plan",
    ]


async def test_quiet_hour_costs_no_claude_call_and_no_block_reads(env):
    env.fake.add_text(env.ws.inbox_id, "paragraph", "change bedsheets")
    await sync(env)
    env.fake.calls.clear()
    report = await sync(env)
    assert report.skipped
    assert env.llm.calls == 1  # still just the first one
    assert "blocks.children.append" not in env.fake.calls
    assert "blocks.update" not in env.fake.calls


async def test_nested_lines_move_with_their_parent(env):
    parent = env.fake.add_text(env.ws.inbox_id, "bulleted_list_item", "mum bday 12th")
    env.fake.add_text(parent, "bulleted_list_item", "get gift")
    await sync(env)
    [copy] = env.fake.child_blocks(env.ws.processed_id)
    assert env.fake.texts(copy["id"]) == ["get gift"]
    assert env.fake.objects[parent]["archived"]


async def test_missed_line_is_retried_then_parked_and_owner_told(env):
    env.fake.add_text(env.ws.inbox_id, "paragraph", "buy milk")
    missed = env.fake.add_text(env.ws.inbox_id, "paragraph", "MISS 2,000 calories/day")

    first = await sync(env)
    assert first.missed == ["MISS 2,000 calories/day"] and first.parked == []
    assert not env.fake.objects[missed]["archived"]  # stays in the Inbox

    second = await sync(env)  # retried even though the Inbox didn't change
    assert second.sorted_lines == 1 and second.parked == ["MISS 2,000 calories/day"]
    [(text, space)] = env.notifier.sent
    assert "MISS 2,000 calories/day" in text and space is Space.CHAT

    calls = env.llm.calls
    third = await sync(env, force=True)
    assert env.llm.calls == calls and third.sorted_lines == 0  # parked: not re-sent


async def test_rewording_a_parked_line_gives_it_a_fresh_chance(env):
    missed = env.fake.add_text(env.ws.inbox_id, "paragraph", "MISS thing")
    await sync(env)
    await sync(env)
    env.fake.objects[missed]["paragraph"]["rich_text"] = [{"plain_text": "eat 2000 calories a day"}]
    env.fake.touch(env.ws.inbox_id)
    report = await sync(env)
    assert report.sorted_lines == 1 and report.moved == 1
    assert "[ ] eat 2000 calories a day" in board_tasks(env)


async def test_line_typed_on_the_board_is_sorted_and_moved(env):
    env.fake.add_text(env.ws.inbox_id, "paragraph", "change bedsheets")
    await sync(env)
    typed = env.fake.add_text(env.ws.board_id, "paragraph", "get NAS for photos")

    report = await sync(env)
    assert report.sorted_lines == 1
    assert env.fake.objects[typed]["archived"]  # removed from where it was typed
    assert "[ ] get NAS for photos" in board_tasks(env)  # and placed properly
    assert "get NAS for photos" in env.fake.texts(env.ws.processed_id)


async def test_ticks_on_the_board_are_saved(env):
    env.fake.add_text(env.ws.inbox_id, "paragraph", "change bedsheets")
    await sync(env)
    [item] = env.store.all()
    env.fake.objects[item.board_block_id]["to_do"]["checked"] = True
    env.fake.touch(env.ws.board_id)
    report = await sync(env)
    assert report.edits == 1 and env.store.get(item.id).status == "done"


async def test_failed_copy_leaves_the_original_in_place(env, monkeypatch):
    note = env.fake.add_text(env.ws.inbox_id, "paragraph", "change bedsheets")
    real_copy = notion.copy_block

    def broken_copy(client, block, parent, depth=0, **kw):
        copy_id = real_copy(client, block, parent, depth, **kw)
        env.fake.objects[copy_id]["paragraph"]["rich_text"] = [{"plain_text": "garbled"}]
        return copy_id

    monkeypatch.setattr(notion, "copy_block", broken_copy)
    report = await sync(env)
    assert report.move_failures == 1 and report.moved == 0
    assert not env.fake.objects[note]["archived"]
    assert env.fake.texts(env.ws.processed_id) == []  # the bad copy was removed

    monkeypatch.setattr(notion, "copy_block", real_copy)
    report = await sync(env)  # next sync retries the move without re-sorting
    assert report.moved == 1 and report.sorted_lines == 0


async def test_summary_reads_well(env):
    env.fake.add_text(env.ws.inbox_id, "paragraph", "change bedsheets")
    report = await sync(env)
    assert report.summary().startswith("Sorted 1 new line(s) into 1 item(s)")
    assert (await sync(env)).summary() == "Nothing new since the last sync."


async def test_items_saved_outside_a_sync_reach_the_board(env):
    env.fake.add_text(env.ws.inbox_id, "paragraph", "change bedsheets")
    await sync(env)
    assert (await sync(env)).skipped
    # e.g. onboarding saves schedule entries straight into the item records
    env.store.add(
        kind="schedule",
        title="Gym",
        details=None,
        project=None,
        area="Health",
        due=None,
        question=None,
        days="mon,wed,fri",
        start_time="07:00",
        end_time="08:00",
    )
    report = await sync(env)
    assert not report.skipped and report.board_created >= 1
    assert "📌 My schedule" in env.fake.texts(env.ws.board_id)
    assert (await sync(env)).skipped  # and quiet again afterwards


async def test_ticking_a_block_on_the_schedule_ticks_the_board_in_the_same_sync(env):
    from ultron.features.items import ItemStore
    from ultron.features.schedule import PlanStore

    store = ItemStore(env.state)
    todo = store.add(
        kind="task",
        title="Call builders",
        details=None,
        project=None,
        area="Wealth",
        due=None,
        question=None,
    )
    PlanStore(env.state).add(
        NOW.date(), "Call builders", start="09:00", end="10:00", item_id=todo.id
    )
    report = await sync(env)
    assert report.schedule_changed
    page = env.ws.schedule_id
    block = next(b for b in env.fake.child_blocks(page) if "Call builders" in str(b))
    block["to_do"]["checked"] = True
    env.fake.touch(page)  # Notion bumps the page's edit time
    report = await sync(env)
    assert report.edits >= 1 and store.get(todo.id).status == "done"
    # no longer an open to-do on the Board (ticked items show only on the real day ticked)
    assert "[ ] Call builders" not in env.fake.texts(env.ws.board_id)


def _to_do(text):
    rich = [{"type": "text", "text": {"content": text}, "plain_text": text}]
    return {"type": "to_do", "to_do": {"rich_text": rich, "checked": False}}


async def test_a_tick_box_typed_on_the_board_stays_where_it_was_typed(env):
    # Owner's choice, 25 Oct: typed lines aren't moved to the bottom of their group
    env.fake.add_text(env.ws.inbox_id, "paragraph", "change bedsheets")
    env.fake.add_text(env.ws.inbox_id, "paragraph", "call builders")
    await sync(env)
    first = env.store.all()[0].board_block_id
    typed = env.fake.add_block(env.ws.board_id, _to_do("get NAS for photos"), after=first)

    await sync(env)
    assert not env.fake.objects[typed]["archived"]  # the typed block is the to-do now
    [item] = [i for i in env.store.all() if i.title == "get NAS for photos"]
    assert item.board_block_id == typed
    assert board_tasks(env) == [
        "[ ] change bedsheets",
        "[ ] get NAS for photos",
        "[ ] call builders",
    ]
    assert "get NAS for photos" not in env.fake.texts(env.ws.processed_id)

    report = await sync(env, force=True)
    assert report.sorted_lines == 0 and report.board_created == 0


async def test_a_plain_line_typed_between_to_dos_becomes_a_to_do_in_that_spot(env):
    env.fake.add_text(env.ws.inbox_id, "paragraph", "change bedsheets")
    env.fake.add_text(env.ws.inbox_id, "paragraph", "call builders")
    await sync(env)
    first = env.store.all()[0].board_block_id
    typed = env.fake.add_block(
        env.ws.board_id,
        {"type": "paragraph", "paragraph": {"rich_text": _to_do("get NAS")["to_do"]["rich_text"]}},
        after=first,
    )

    await sync(env)
    assert env.fake.objects[typed]["archived"]  # moved to ✓ Processed…
    assert "get NAS" in env.fake.texts(env.ws.processed_id)
    assert board_tasks(env) == ["[ ] change bedsheets", "[ ] get NAS", "[ ] call builders"]
