from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ultron.features import daylog, habits, nightly, review
from ultron.features.items import ItemStore
from ultron.features.nightly import NightlyResult, Suggestion
from ultron.features.schedule import Block, PlanStore
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
WED = date(2026, 10, 21)
NINE = datetime(2026, 10, 21, 21, 0, tzinfo=LONDON)


class FakeNotifier:
    def __init__(self):
        self.messages = []  # (kind, text, topic, buttons)

    async def send(self, text, space=Space.BRIEFS):
        self.messages.append(("send", text, None, None))

    async def ask(self, text, topic, space=Space.BRIEFS, placeholder=None):
        self.messages.append(("ask", text, topic, None))

    async def offer(self, text, topic, choices, space=Space.BRIEFS):
        self.messages.append(("offer", text, topic, choices))


@pytest.fixture
def env():
    state = State(":memory:")
    store = ItemStore(state)
    plans = PlanStore(state)

    async def notify(_):
        pass

    def add(kind, title, **kw):
        values = dict(details=None, project=None, area="Admin", due=None, question=None)
        values.update(kw)
        return store.add(kind=kind, title=title, **values)

    builders = add("task", "Call builders", area="Wealth", due="2026-10-21")
    spanish = add("habit", "Learn Spanish 30 mins")
    read = add("habit", "Read 5 mins")
    gym = plans.add(WED, "Workout", start="07:00", end="08:30")
    plans.set_status(gym.id, "done")
    calls = plans.add(WED, "Call builders", start="09:00", end="09:45", item_id=builders.id)
    admin = plans.add(WED, "Admin", start="10:00", end="10:30")
    yield SimpleNamespace(
        state=state,
        store=store,
        plans=plans,
        notifier=FakeNotifier(),
        guard=CostGuard(state, cap_usd=20, zone=LONDON, notify=notify),
        builders=builders,
        spanish=spanish,
        read=read,
        calls=calls,
        admin=admin,
    )
    state.close()


async def test_start_sends_blocks_habits_and_the_reflection_question(env):
    session = await review.start(env.state, env.notifier, NINE)
    assert session.day == "2026-10-21"
    blocks, habit_msg, reflect = env.notifier.messages
    assert blocks[0] == "offer" and blocks[1].splitlines() == [
        "🌙 Today in review · Wed 21 Oct",
        "Already done: Workout",
        "How did these go?",
    ]
    assert [row[0][1] for row in blocks[3][::2]] == ["✅ Call builders", "✅ Admin"]
    assert [k for row in blocks[3][:2] for k, _ in row] == [
        f"b:{env.calls.id}:done",
        f"b:{env.calls.id}:partial",
        f"b:{env.calls.id}:skipped",
    ]
    assert habit_msg[0] == "offer" and [r[0][1] for r in habit_msg[3]] == [
        "✓ Learn Spanish 30 mins",
        "✓ Read 5 mins",
        "None of the rest",
    ]
    assert reflect[0] == "ask" and reflect[2] == review.REFLECT_TOPIC
    assert await review.start(env.state, env.notifier, NINE) is None  # once a day


def test_each_block_title_gets_a_full_row():
    title = "Deep work: Acme proposal, pricing and the cover email"
    block = Block(7, "2026-10-23", "09:00", "11:00", title, None, 120, "planned")
    assert review.block_rows([block]) == [
        [("b:7:done", "✅ Deep work: Acme proposal, pricing and t…")],
        [("b:7:partial", "◐ Partly"), ("b:7:skipped", "❌ Missed")],
    ]


async def test_ticking_done_ticks_the_todo_and_removes_the_row(env):
    await review.start(env.state, env.notifier, NINE)
    result = await review.tap(env.state, env.notifier, f"b:{env.calls.id}:done", NINE)
    assert result.ack == "✅ Call builders"
    assert [[label for _, label in row] for row in result.rows] == [
        ["✅ Admin"],
        ["◐ Partly", "❌ Missed"],
    ]
    assert env.plans.get(env.calls.id).status == "done"
    assert env.store.get(env.builders.id).status == "done"


async def test_a_miss_asks_what_next_then_what_got_in_the_way(env):
    await review.start(env.state, env.notifier, NINE)
    env.notifier.messages.clear()
    await review.tap(env.state, env.notifier, f"b:{env.calls.id}:skipped", NINE)
    [(kind, text, _, choices)] = env.notifier.messages
    assert kind == "offer" and text == "❌ Call builders: what now?"
    assert [label for _, label in choices] == ["➡️ Tomorrow", "✂️ Shrink", "🗑 Drop"]

    result = await review.tap(env.state, env.notifier, f"m:{env.calls.id}:tomorrow", NINE)
    assert result.ack == "Moved to tomorrow: Call builders" and result.rows == []
    tomorrow = env.plans.for_day(WED + timedelta(days=1))
    assert [(b.title, b.item_id) for b in tomorrow] == [("Call builders", env.builders.id)]
    todo = env.store.get(env.builders.id)
    assert todo.due == "2026-10-22" and todo.times_moved == 1  # it slipped: counts
    kind, text, topic, _ = env.notifier.messages[-1]
    assert kind == "ask" and topic == f"{review.WHY_TOPIC}{env.calls.id}"
    assert review.note_why(env.state, env.calls.id, "builder never called back") == "Noted."


async def test_shrink_and_drop(env):
    await review.start(env.state, env.notifier, NINE)
    await review.tap(env.state, env.notifier, f"b:{env.admin.id}:partial", NINE)
    result = await review.tap(env.state, env.notifier, f"m:{env.admin.id}:shrink", NINE)
    assert result.ack == "Shrunk to tomorrow: Admin: first 15 min"
    [small] = env.plans.for_day(WED + timedelta(days=1))
    assert small.minutes == 15
    await review.tap(env.state, env.notifier, f"b:{env.calls.id}:skipped", NINE)
    await review.tap(env.state, env.notifier, f"m:{env.calls.id}:drop", NINE)
    assert env.store.get(env.builders.id).status == "dropped"


async def test_habit_taps_keep_them_for_today(env):
    await review.start(env.state, env.notifier, NINE)
    result = await review.tap(env.state, env.notifier, f"h:{env.spanish.id}", NINE)
    assert result.ack == "Kept: Learn Spanish 30 mins"
    assert [r[0][1] for r in result.rows] == ["✓ Read 5 mins", "None of the rest"]
    assert env.store.get(env.spanish.id).kept_on == "2026-10-21"
    assert (await review.tap(env.state, env.notifier, "h:none", NINE)).rows == []


def make_llm_for(llm):
    @asynccontextmanager
    async def make_llm():
        yield llm

    return make_llm


class PlanLLM:
    def __init__(self):
        self.messages = self
        self.calls = 0

    async def parse(self, **kwargs):
        self.calls += 1
        parsed = NightlyResult(suggestions=[Suggestion(title="x", why="y", item_ids=[])])
        usage = SimpleNamespace(input_tokens=100, output_tokens=50)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, usage=usage)


async def test_finish_logs_the_day_and_moves_on_to_planning(env):
    await review.start(env.state, env.notifier, NINE)
    await review.tap(env.state, env.notifier, f"b:{env.calls.id}:skipped", NINE)
    await review.tap(env.state, env.notifier, f"m:{env.calls.id}:tomorrow", NINE)
    review.note_why(env.state, env.calls.id, "builder never called back")
    await review.tap(env.state, env.notifier, f"b:{env.admin.id}:done", NINE)
    env.notifier.messages.clear()
    llm = PlanLLM()
    settings = SimpleNamespace(zone=LONDON, model_smart="claude-sonnet-5-5")
    done = await review.finish(
        settings,
        env.state,
        env.guard,
        env.notifier,
        make_llm_for(llm),
        reflection="Tired, but the gym helped",
        now=NINE.replace(minute=10),
    )
    assert done and llm.calls == 1
    logged, planning = env.notifier.messages
    assert (
        logged[1] == "📓 Logged: Day review: 2 of 3 done, 1 skipped · Balanced · reflection noted"
    )
    assert planning[0] == "ask" and planning[2] == nightly.TOPIC  # tomorrow's priorities
    entry = env.store.get(int(env.state.get(f"{daylog.KEY_PREFIX}2026-10-21")))
    assert "In the way: Call builders: tomorrow (builder never called back)" in entry.details
    assert entry.details.endswith("Reflection: Tired, but the gym helped")
    # finishing twice does nothing
    assert not await review.finish(
        settings, env.state, env.guard, env.notifier, make_llm_for(llm), reflection=None
    )


async def test_skip_means_no_reflection(env):
    await review.start(env.state, env.notifier, NINE)
    settings = SimpleNamespace(zone=LONDON, model_smart="m")
    await review.finish(
        settings,
        env.state,
        env.guard,
        env.notifier,
        make_llm_for(PlanLLM()),
        reflection="skip",
        now=NINE,
    )
    assert daylog.reflection_of(env.state, WED) is None


async def test_unfinished_review_is_closed_after_30_minutes(env):
    await review.start(env.state, env.notifier, NINE)
    assert not review.overdue(env.state, NINE.replace(minute=20))
    assert review.overdue(env.state, NINE.replace(minute=31))


async def test_startup_catch_up(env):
    at, quiet = time(21, 0), time(22, 30)
    assert review.due_now(env.state, NINE.replace(minute=40), at, quiet)
    assert not review.due_now(env.state, NINE.replace(hour=20), at, quiet)
    await review.start(env.state, env.notifier, NINE)
    assert not review.due_now(env.state, NINE.replace(minute=40), at, quiet)


async def test_no_plan_and_no_habits_still_asks_for_a_reflection(env):
    for b in env.plans.for_day(WED):
        env.plans.set_status(b.id, "dropped")
    for h in (env.spanish, env.read):
        habits.keep(env.store, h.id, WED)
    await review.start(env.state, env.notifier, NINE)
    kinds = [m[0] for m in env.notifier.messages]
    assert kinds == ["send", "ask"] and env.notifier.messages[0][1].endswith("No plan today.")
