import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import CONFIGURED, make_settings
from fake_notion import FakeNotion

from ultron.features import accountability, review, roadmap
from ultron.features.board import apply_changes, read_changes, snapshot, write_board
from ultron.features.focus import current, resolve
from ultron.features.items import ItemStore, apply_sort, board_context
from ultron.features.planner import PlannedBlock, PlanResult, draft_plan
from ultron.features.ranking import rank
from ultron.features.schedule import PlanStore, ScheduleEdits, apply_edits
from ultron.features.sorter import SortedItem, SortOutcome, SortResult
from ultron.features.sync import run_sync
from ultron.integrations import notion
from ultron.integrations.notion import ensure_workspace
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
SAT = date(2026, 10, 24)  # before week 1
MON = date(2026, 10, 26)  # week 1 starts
THU = date(2026, 10, 29)  # a work day

ROADMAP = {
    "project": "AI career",
    "title": "🚀 AI career",
    "goal": "First AI governance role",
    "target": "by Oct 2027",
    "starts": "2026-10-26",
    "daily_minutes": 110,
    "short_minutes": 45,
    "short_below": 360,
    "notes": "Reading on weekday evenings",
    "phases": [
        {
            "phase": 1,
            "title": "Foundations",
            "span": "weeks 1–2",
            "starts": "2026-10-26",
            "ends": "2026-11-08",
            "goal": "Learn the language.",
            "checkpoint": "course started",
            "stages": [
                {
                    "title": "Week 1 — Set up",
                    "due": "2026-11-01",
                    "steps": [
                        {"title": "Apply to BlueDot", "link": "https://bluedot.org"},
                        {"title": "Rewrite LinkedIn headline"},
                    ],
                },
                {
                    "title": "Week 2 — Basics",
                    "due": "2026-11-08",
                    "steps": [{"title": "Read EU AI Act"}],
                },
            ],
        },
        {
            "phase": 2,
            "title": "Portfolio",
            "span": "weeks 3–4",
            "starts": "2026-11-09",
            "ends": "2026-11-22",
            "goal": "Public work.",
            "checkpoint": "Project 1 published",
            "stages": [
                {"title": "Project 1", "due": "2026-11-15", "steps": [{"title": "Pick models"}]},
                {"title": "Project 2", "due": "2026-11-22", "steps": [{"title": "Pick a tool"}]},
            ],
        },
        {
            "phase": 3,
            "title": "Network",
            "span": "months 2–6",
            "starts": "2026-11-23",
            "ends": "2027-04-25",
            "goal": "Be known.",
            "checkpoint": "a referral",
            "stages": [{"title": "Network", "steps": [{"title": "Find a mentor"}]}],
        },
    ],
    "habits": [
        {"title": "AI career: learning log", "starts": "2026-10-26"},
        {"title": "AI career: LinkedIn message", "days": "tue,sat", "starts": "2026-11-23"},
    ],
}


def load(state, data=None):
    return roadmap.load(state, roadmap.RoadmapIn.model_validate(data or ROADMAP))


def steps(store):
    return {i.title: i for i in store.all(include_dropped=True) if i.is_step}


@pytest.fixture
def env():
    fake = FakeNotion()
    state = State(":memory:")
    ws = ensure_workspace(fake, state, fake.root).workspace
    store = ItemStore(state)

    def add(kind, title, **kw):
        values = dict(details=None, project=None, area="Admin", due=None, question=None)
        values.update(kw)
        return store.add(kind=kind, title=title, **values)

    add("schedule", "Work", days="thu,sun", start_time="07:00", end_time="18:00", travel_minutes=30)
    add(
        "schedule",
        "Sleep",
        days="mon,tue,wed,thu,fri,sat,sun",
        start_time="22:00",
        end_time="06:00",
    )
    add("task", "Call GP", area="Health")
    load(state)
    yield SimpleNamespace(fake=fake, state=state, ws=ws, store=store, add=add)
    state.close()


def sync_board(env, today):
    snap = snapshot(env.fake, env.ws, env.store)
    changes = read_changes(env.fake, snap, env.ws, env.store, today)
    apply_changes(env.store, changes, today)
    roadmap.adopt(env.state, today, {j.block_id: j.group for j in changes.new_lines})
    snap = snapshot(env.fake, env.ws, env.store)
    write_board(
        env.fake, env.ws, env.store, snap, today, LONDON, roadmap.board_plan(env.state, today)
    )
    return changes


def write_only(env, today):
    """Ultron's own write, as after the evening review ticks something."""
    snap = snapshot(env.fake, env.ws, env.store)
    plan = roadmap.board_plan(env.state, today)
    write_board(env.fake, env.ws, env.store, snap, today, LONDON, plan)


def page(env, parent=None, indent=""):
    """Page-level lines, with the folded roadmap's and a phase toggle's children indented
    under them."""
    out = []
    parent = parent or env.ws.board_id
    for block, text in zip(env.fake.child_blocks(parent), env.fake.texts(parent), strict=True):
        out.append(indent + text)
        kind = block["type"]
        roadmap = kind == "heading_2" and text.startswith("🚀")
        if (kind == "heading_3" or roadmap) and block[kind].get("is_toggleable"):
            out += page(env, block["id"], indent + "  ")
    return out


def block_of(env, text):
    for parent in [env.ws.board_id] + list(env.fake.children):
        for block, t in zip(env.fake.child_blocks(parent), env.fake.texts(parent), strict=True):
            if t == text:
                return block
    raise AssertionError(f"no block {text!r}")


# --- loading ---


def test_loading_again_keeps_ticks_and_wording_and_adds_new_steps(env):
    s = steps(env.store)
    env.store.set_status(s["Apply to BlueDot"].id, "done")
    env.store.update(s["Read EU AI Act"].id, title="Read the EU AI Act summary")
    data = {**ROADMAP, "phases": [dict(p) for p in ROADMAP["phases"]]}
    week2 = {"title": "Week 2 — Basics", "due": "2026-11-08"}
    week2["steps"] = [{"title": "Read EU AI Act", "details": "risk tiers"}, {"title": "NIST RMF"}]
    data["phases"][0] = {**data["phases"][0], "stages": [data["phases"][0]["stages"][0], week2]}
    result = load(env.state, data)
    assert (result.steps_added, result.steps_kept, result.habits_added) == (1, 6, 0)
    s = steps(env.store)
    assert s["Apply to BlueDot"].status == "done"
    assert s["Read the EU AI Act summary"].details == "risk tiers"  # notes refresh, words kept
    assert (
        s["NIST RMF"].step == 2
        and s["NIST RMF"].stage_id == s["Read the EU AI Act summary"].stage_id
    )
    habits = [i for i in env.store.all() if i.kind == "habit"]
    assert [h.paused_until for h in habits] == ["2026-10-26", "2026-11-23"]
    assert all(h.project == "AI career" for h in habits)


# --- what's under way ---


def test_the_earliest_phase_with_steps_left_is_open_and_later_ones_once_they_start(env):
    assert roadmap.view(env.state, SAT).open_phases == [1]
    assert roadmap.view(env.state, date(2026, 11, 10)).open_phases == [1, 2]  # 1 is late
    for item in steps(env.store).values():
        if roadmap.view(env.state, SAT).stage_of(item).phase == 1:
            env.store.set_status(item.id, "done")
    assert roadmap.view(env.state, SAT).open_phases == [2]  # early: nothing else left


def test_daily_time_is_shorter_on_a_work_day_and_none_before_the_start(env):
    v = roadmap.view(env.state, MON)
    items = env.store.all()
    assert roadmap.day_target(v, items, SAT, CONFIGURED) == 0
    assert roadmap.day_target(v, items, MON, CONFIGURED) == 110
    assert roadmap.day_target(v, items, THU, CONFIGURED) == 45
    assert roadmap.week_target(v, items, MON, CONFIGURED) == 5 * 110 + 2 * 45


def test_time_done_today_counts_against_what_is_still_needed(env):
    v = roadmap.view(env.state, MON)
    step = steps(env.store)["Apply to BlueDot"]
    block = PlanStore(env.state).add(
        MON, "AI career: BlueDot", start="07:00", end="08:00", item_id=step.id
    )
    PlanStore(env.state).set_status(block.id, "done")
    now = datetime(2026, 10, 26, 12, 0, tzinfo=LONDON)
    assert roadmap.need_on(env.state, v, MON, now) == 50
    assert roadmap.spent(env.state, v, MON, MON) == 60
    assert roadmap.sessions(env.state, [step.id]) == {step.id: (1, 60)}


# --- the Board ---


def test_the_board_shows_the_roadmap_above_to_do_with_later_phases_folded(env):
    env.add("habit", "Meditate")
    sync_board(env, SAT)
    lines = page(env)
    assert lines[1:4] == ["☑️ Today's habits", "[ ] Meditate", "🚀 AI career"]
    assert lines[4].strip().splitlines() == [
        "First AI governance role · by Oct 2027",
        "Now: Phase 1 — Foundations · starts Mon 26 Oct · 0 of 3 steps done",
        "Checkpoint: course started",
        "Daily time starts Mon 26 Oct: 1h 50m a day, 45m on short days",
    ]
    assert lines[5:15] == [  # all inside the folded 🚀 section
        "  Week 1 — Set up · by Sun 01 Nov",
        "  [ ] Apply to BlueDot",
        "  [ ] Rewrite LinkedIn headline",
        "  Week 2 — Basics · by Sun 08 Nov",
        "  [ ] Read EU AI Act",
        "  Phase 2 — Portfolio · weeks 3–4 · starts Mon 09 Nov",
        "    Goal: Public work. Checkpoint: Project 1 published",
        "    Project 1 · by Sun 15 Nov",
        "    [ ] Pick models",
        "    Project 2 · by Sun 22 Nov",
    ]
    assert lines[15:19] == [
        "    [ ] Pick a tool",
        "  Phase 3 — Network · months 2–6 · starts Mon 23 Nov",
        "    Goal: Be known. Checkpoint: a referral",
        "    [ ] Find a mentor",  # one stage: no label
    ]
    assert lines[19:22] == ["✅ To-do", "Health", "[ ] Call GP"]
    link = block_of(env, "[ ] Apply to BlueDot")["to_do"]["rich_text"][0]["text"]["link"]
    assert link == {"url": "https://bluedot.org"}
    calls = len(env.fake.calls)
    sync_board(env, SAT)  # nothing changed: no writes
    assert not [c for c in env.fake.calls[calls:] if c.endswith(("append", "update", "delete"))]


def test_ticks_and_rewording_on_the_board_are_read_back_even_inside_a_folded_phase(env):
    sync_board(env, SAT)
    block_of(env, "[ ] Apply to BlueDot")["to_do"]["checked"] = True
    folded = block_of(env, "[ ] Pick models")
    folded["to_do"]["rich_text"] = [{"type": "text", "text": {"content": "Pick 3 models"}}]
    changes = sync_board(env, SAT)
    s = steps(env.store)
    assert s["Apply to BlueDot"].status == "done" and "Pick 3 models" in s
    assert changes.new_lines == []
    status = page(env)[2].splitlines()[1]
    assert status.endswith("1 of 3 steps done")


def test_deleting_a_step_drops_it_but_deleting_a_folded_phase_redraws_it(env):
    sync_board(env, SAT)
    env.fake.blocks.delete(block_id=block_of(env, "[ ] Rewrite LinkedIn headline")["id"])
    env.fake.blocks.delete(
        block_id=block_of(env, "Phase 2 — Portfolio · weeks 3–4 · starts Mon 09 Nov")["id"]
    )
    sync_board(env, SAT)
    s = steps(env.store)
    assert s["Rewrite LinkedIn headline"].status == "dropped"
    assert s["Pick models"].status == "open" and s["Pick a tool"].status == "open"
    assert "    [ ] Pick models" in page(env)


def test_a_phase_unfolds_when_it_starts_and_finished_stages_disappear(env):
    sync_board(env, SAT)
    for title in ("Apply to BlueDot", "Rewrite LinkedIn headline"):
        env.store.set_status(steps(env.store)[title].id, "done")
    write_only(env, SAT)
    day = date(2026, 11, 10)
    sync_board(env, day)
    lines = page(env)
    start = lines.index("🚀 AI career")
    assert lines[start + 2 : start + 10] == [
        "  Week 2 — Basics · was due Sun 08 Nov",
        "  [ ] Read EU AI Act",
        "  Project 1 · by Sun 15 Nov",
        "  [ ] Pick models",
        "  Project 2 · by Sun 22 Nov",
        "  [ ] Pick a tool",
        "  Phase 3 — Network · months 2–6 · starts Mon 23 Nov",
        "    Goal: Be known. Checkpoint: a referral",
    ]
    assert not any(t.strip().startswith(("Week 1", "Phase 2")) for t in lines)
    status = lines[start + 1].strip().splitlines()
    assert status[1] == "Now: Phase 1 — Foundations · past its end (Sun 08 Nov) · 2 of 3 steps done"
    assert status[3] == "Also under way: Phase 2 — Portfolio"
    assert status[-1] == "⚠️ Behind: Week 2 — Basics was due Sun 08 Nov (1 step left)"


def test_a_line_typed_under_a_stage_heading_joins_that_stage(env):
    sync_board(env, SAT)
    heading = block_of(env, "Week 2 — Basics · by Sun 08 Nov")
    typed = env.fake.add_block(
        block_of(env, "🚀 AI career")["id"],  # inside the folded section
        {
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Read NIST"}}]},
        },
        after=heading["id"],
    )
    snap = snapshot(env.fake, env.ws, env.store)
    [line] = read_changes(env.fake, snap, env.ws, env.store, SAT).new_lines
    assert line.heading == "🚀 AI career › Week 2 — Basics · by Sun 08 Nov"
    assert line.group.startswith("stage:")
    item = env.store.add(
        kind="task",
        title="Read NIST",
        details=None,
        project="Week 2",
        area="Learning",
        due=None,
        question=None,
        source_blocks=(typed,),
    )  # as the sorter would file it
    roadmap.adopt(env.state, SAT, {typed: line.group})
    item = env.store.get(item.id)
    assert item.is_step and item.project == "AI career"
    assert item.stage_id == steps(env.store)["Read EU AI Act"].stage_id and item.step == 2


def test_a_new_to_do_in_the_project_joins_the_first_stage_with_steps_left(env):
    item = env.add("task", "Email Sam about AISI", project="AI career")
    assert roadmap.adopt(env.state, SAT) == 1
    item = env.store.get(item.id)
    assert item.stage_id == steps(env.store)["Apply to BlueDot"].stage_id and item.step == 3
    sync_board(env, SAT)
    lines = page(env)
    assert (
        lines.index("  [ ] Email Sam about AISI")
        == lines.index("  [ ] Rewrite LinkedIn headline") + 1
    )


def test_a_redrawn_folded_phase_keeps_a_line_the_owner_typed_there(env):
    sync_board(env, SAT)
    toggle = block_of(env, "Phase 2 — Portfolio · weeks 3–4 · starts Mon 09 Nov")
    env.fake.add_text(toggle["id"], "paragraph", "ask Sam about evals")
    env.fake.blocks.delete(block_id=block_of(env, "  Project 1 · by Sun 15 Nov".strip())["id"])
    changes = sync_board(env, SAT)
    assert [j.text for j in changes.new_lines] == ["ask Sam about evals"]
    tool = steps(env.store)["Pick a tool"]
    assert changes.new_lines[0].group == f"stage:{tool.stage_id}"  # typed under Project 2
    inside = env.fake.texts(toggle["id"])
    assert inside[0] == "ask Sam about evals"  # left in place, for the sorter to move
    assert inside[1:5] == [
        "Goal: Public work. Checkpoint: Project 1 published",
        "Project 1 · by Sun 15 Nov",
        "[ ] Pick models",
        "Project 2 · by Sun 22 Nov",
    ]


def test_without_an_active_roadmap_its_section_goes(env):
    sync_board(env, SAT)
    env.state.db.execute("UPDATE roadmaps SET status = 'paused'")
    sync_board(env, SAT)
    assert page(env)[1:4] == ["✅ To-do", "Health", "[ ] Call GP"]


# --- elsewhere: ranking, sorting, Schedule ticks ---


def test_steps_are_not_ranked_with_the_to_dos(env):
    resolved = resolve(current(env.state), env.store)
    assert [r.item.title for r in rank(env.store, resolved, SAT)] == ["Call GP"]


def test_a_repeated_note_doesnt_reword_or_refile_a_step(env):
    context = board_context(env.store, SAT)
    target = steps(env.store)["Apply to BlueDot"]
    same_as = context.ids.index(target.id) + 1
    outcome = SortOutcome(
        SortResult(
            projects=[],
            items=[
                SortedItem(
                    kind="task",
                    title="BlueDot application",
                    details="due soon",
                    project=None,
                    area="Learning",
                    due=None,
                    on_day=False,
                    status="open",
                    when=None,
                    question=None,
                    same_as=same_as,
                    lines=[1],
                )
            ],
            updates=[],
            labels=[],
        ),
        {1: "b1"},
        [],
        0.0,
    )
    apply_sort(env.store, outcome, context, today=SAT)
    item = env.store.get(target.id)
    assert (item.title, item.project, item.is_step) == ("Apply to BlueDot", "AI career", True)
    assert item.details == "due soon"


def test_ticking_a_session_on_the_schedule_keeps_the_step_open(env):
    step = steps(env.store)["Apply to BlueDot"]
    block = PlanStore(env.state).add(
        MON, "AI career: BlueDot", start="07:00", end="08:00", item_id=step.id
    )
    apply_edits(env.state, ScheduleEdits(ticked=[block.id]))
    assert PlanStore(env.state).get(block.id).status == "done"
    assert env.store.get(step.id).status == "open"


# --- the planner ---


class FakeLLM:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []
        self.messages = self

    async def parse(self, **kwargs):
        self.requests.append(kwargs)
        usage = SimpleNamespace(input_tokens=6000, output_tokens=800)
        return SimpleNamespace(
            stop_reason="end_turn", parsed_output=self.results.pop(0), usage=usage
        )


def plan(*blocks):
    return PlanResult(
        blocks=[
            PlannedBlock(day=d, start=s, end=e, title=t, item_id=i) for d, s, e, t, i in blocks
        ],
        later=[],
        notes="Plan.",
    )


async def draft(env, llm, days):
    async def notify(_):
        pass

    guard = CostGuard(env.state, cap_usd=20, zone=LONDON, notify=notify)
    now = datetime(2026, 10, 25, 21, 0, tzinfo=LONDON)
    return await draft_plan(env.state, guard, llm, model="m", days=days, now=now, priorities=None)


async def test_a_plan_short_on_roadmap_time_gets_one_retry(env):
    step = steps(env.store)["Apply to BlueDot"]
    short = plan(("2026-10-26", "07:00", "07:30", "AI career: BlueDot", step.id))
    enough = plan(
        ("2026-10-26", "07:00", "08:00", "AI career: BlueDot", step.id),
        (
            "2026-10-26",
            "18:00",
            "18:50",
            "AI career: LinkedIn",
            steps(env.store)["Rewrite LinkedIn headline"].id,
        ),
    )
    llm = FakeLLM(short, enough)
    d = await draft(env, llm, [MON])
    assert len(d.blocks) == 2 and "AI career" not in d.notes
    content = llm.requests[0]["messages"][0]["content"]
    assert "AI career: at least 110 min this day, on its next steps" in content
    assert f"- [#{step.id}] Apply to BlueDot · Week 1 — Set up · stage due 2026-11-01" in content
    assert "Checkpoint: course started" in content
    retry = llm.requests[1]["messages"][-1]["content"]
    assert "AI career gets 30 of the 110 min it needs" in retry


async def test_still_short_after_the_retry_is_said_under_the_plan(env):
    step = steps(env.store)["Apply to BlueDot"]
    short = plan(("2026-10-26", "07:00", "07:30", "AI career: BlueDot", step.id))
    d = await draft(env, FakeLLM(short, short), [MON])
    assert d.notes.endswith("⚠️ AI career gets less than it needs: Mon 30m of 1h 50m")


async def test_a_work_day_needs_less_and_the_day_before_the_start_none(env):
    llm = FakeLLM(
        plan(
            (
                "2026-10-29",
                "18:40",
                "19:25",
                "AI career: read",
                steps(env.store)["Read EU AI Act"].id,
            )
        )
    )
    await draft(env, llm, [THU])
    assert "AI career: at least 45 min this day" in llm.requests[0]["messages"][0]["content"]
    llm = FakeLLM(plan())
    await draft(env, llm, [date(2026, 10, 25)])
    assert len(llm.requests) == 1  # nothing required on Sunday the 25th: no retry


# --- the evening review ---


class Notifier:
    def __init__(self):
        self.messages = []

    async def send(self, text, space=Space.BRIEFS):
        self.messages.append(("send", text, None))

    async def ask(self, text, topic, space=Space.BRIEFS, placeholder=None):
        self.messages.append(("ask", text, None))

    async def offer(self, text, topic, choices, space=Space.BRIEFS):
        self.messages.append(("offer", text, choices))


async def test_review_offers_progress_for_a_step_and_asks_about_sessions_ticked_earlier(env):
    s = steps(env.store)
    plans = PlanStore(env.state)
    worked = plans.add(
        MON, "AI career: BlueDot", start="07:00", end="08:00", item_id=s["Apply to BlueDot"].id
    )
    plans.set_status(worked.id, "done")  # ticked on the Schedule
    later = plans.add(
        MON,
        "AI career: headline",
        start="18:00",
        end="18:45",
        item_id=s["Rewrite LinkedIn headline"].id,
    )
    other = plans.add(MON, "Call GP", start="09:00", end="09:15")
    n = Notifier()
    await review.start(env.state, n, datetime(2026, 10, 26, 21, 0, tzinfo=LONDON))
    blocks, finished = n.messages[0][2], n.messages[1]
    assert blocks[3] == [
        (f"b:{later.id}:progress", "⏩ Progress"),
        (f"b:{later.id}:skipped", "❌ Missed"),
    ]
    assert blocks[1] == [
        (f"b:{other.id}:partial", "◐ Partly"),
        (f"b:{other.id}:skipped", "❌ Missed"),
    ]
    assert finished[1] == "🚀 You worked on these today. Finished any of them?"
    assert finished[2] == [
        [(f"f:{s['Apply to BlueDot'].id}", "✅ Apply to BlueDot")],
        [("f:none", "Not yet")],
    ]

    now = datetime(2026, 10, 26, 21, 1, tzinfo=LONDON)
    tap = await review.tap(env.state, n, f"b:{later.id}:progress", now)
    assert tap.ack.startswith("⏩ Session logged")
    assert plans.get(later.id).status == "done"
    assert env.store.get(s["Rewrite LinkedIn headline"].id).status == "open"
    tap = await review.tap(env.state, n, f"f:{s['Apply to BlueDot'].id}", now)
    assert env.store.get(s["Apply to BlueDot"].id).status == "done"
    assert tap.rows == [
        [(f"f:{s['Rewrite LinkedIn headline'].id}", "✅ Rewrite LinkedIn headline")],
        [("f:none", "Not yet")],
    ]


async def test_a_missed_step_offers_to_drop_the_step_by_name(env):
    s = steps(env.store)
    plans = PlanStore(env.state)
    block = plans.add(
        MON, "AI career: BlueDot", start="07:00", end="08:00", item_id=s["Apply to BlueDot"].id
    )
    n = Notifier()
    await review.start(env.state, n, datetime(2026, 10, 26, 21, 0, tzinfo=LONDON))
    await review.tap(
        env.state, n, f"b:{block.id}:skipped", datetime(2026, 10, 26, 21, 1, tzinfo=LONDON)
    )
    assert n.messages[-1][2][-1] == (f"m:{block.id}:drop", "🗑 Drop step")


# --- accountability and the weekly review ---


def test_a_stage_past_its_date_is_called_out_by_its_next_step(env):
    [callout] = accountability.callouts(env.state, date(2026, 11, 3))
    assert callout.item.title == "Apply to BlueDot"
    assert callout.text == (
        '"Week 1 — Set up" was due Sun 01 Nov and has 2 steps left, starting with '
        '"Apply to BlueDot". Do it first thing tomorrow, or drop it?'
    )


def test_weekly_lines_count_time_and_steps_against_the_plan(env):
    s = steps(env.store)
    plans = PlanStore(env.state)
    for day, minutes in ((MON, 60), (MON + timedelta(days=1), 90)):
        b = plans.add(
            day,
            "AI career",
            start="07:00",
            end=f"{7 + minutes // 60:02d}:{minutes % 60:02d}",
            item_id=s["Apply to BlueDot"].id,
        )
        plans.set_status(b.id, "done")
    env.store.update(s["Apply to BlueDot"].id, status="done", done_at="2026-10-27T10:00:00+00:00")
    sun = date(2026, 11, 1)
    lines = roadmap.weekly_lines(env.state, MON, sun)
    assert lines[0] == "🚀 AI career: 2h 30m of 10h 40m (23%) · 1 step finished"
    assert lines[1] == "  Now: Phase 1 — Foundations, week 1 of 2"
    late = roadmap.weekly_lines(env.state, date(2026, 12, 7), date(2026, 12, 13))
    assert (
        "  ⚠️ Phase 1 — Foundations is over 4 weeks behind its plan: cut scope rather than stop."
        in late
    )


# --- the hourly sync ---


class Quiet:
    async def send(self, text, space=Space.BRIEFS):
        pass


async def test_sync_draws_the_roadmap_and_refreshes_the_status_when_time_is_logged(env):
    settings = make_settings(notion_token="n", notion_root_page_id=env.fake.root)

    async def notify(_):
        pass

    @asynccontextmanager
    async def make_llm():
        yield None

    guard = CostGuard(env.state, cap_usd=20, zone=LONDON, notify=notify)
    original = notion.root_page_id
    notion.root_page_id = lambda s: env.fake.root
    now = datetime(2026, 10, 26, 12, 0, tzinfo=LONDON)
    try:
        await run_sync(settings, env.state, guard, Quiet(), make_llm, client=env.fake, today=now)
        assert "🚀 AI career" in page(env)
        assert re.search(r"This week: 0m of ", page(env)[page(env).index("🚀 AI career") + 1])
        report = await run_sync(
            settings, env.state, guard, Quiet(), make_llm, client=env.fake, today=now
        )
        assert report.skipped
        b = PlanStore(env.state).add(
            now.date(),
            "AI career",
            start="07:00",
            end="08:00",
            item_id=steps(env.store)["Apply to BlueDot"].id,
        )
        PlanStore(env.state).set_status(b.id, "done")
        report = await run_sync(
            settings, env.state, guard, Quiet(), make_llm, client=env.fake, today=now
        )
        assert not report.skipped
        assert "This week: 1h of " in page(env)[page(env).index("🚀 AI career") + 1]
    finally:
        notion.root_page_id = original


def test_a_line_typed_after_a_folded_phase_still_belongs_to_the_roadmap(env):
    sync_board(env, SAT)
    toggle = block_of(env, "Phase 3 — Network · months 2–6 · starts Mon 23 Nov")
    text = {"rich_text": [{"type": "text", "text": {"content": "Join AI safety Slack"}}]}
    env.fake.add_block(
        block_of(env, "🚀 AI career")["id"],
        {"type": "paragraph", "paragraph": text},
        after=toggle["id"],
    )
    snap = snapshot(env.fake, env.ws, env.store)
    [line] = read_changes(env.fake, snap, env.ws, env.store, SAT).new_lines
    assert (line.heading, line.group) == ("🚀 AI career", "roadmap:current")


def test_a_roadmap_drawn_open_is_redrawn_folded(env):
    sync_board(env, SAT)
    heading = block_of(env, "🚀 AI career")
    # As drawn before 25 Oct: an open heading, with everything after it at page level
    heading["heading_2"]["is_toggleable"] = False
    page_ids = env.fake.children[env.ws.board_id]
    inside, env.fake.children[heading["id"]] = env.fake.children[heading["id"]], []
    at = page_ids.index(heading["id"]) + 1
    page_ids[at:at] = inside
    sync_board(env, SAT)
    lines = page(env)
    assert lines.count("🚀 AI career") == 1 and "  [ ] Apply to BlueDot" in lines
    assert "[ ] Apply to BlueDot" not in lines  # nothing left at page level
    assert block_of(env, "🚀 AI career")["heading_2"]["is_toggleable"]
    assert steps(env.store)["Apply to BlueDot"].status == "open"
