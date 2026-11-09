from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import CONFIGURED, configure

from ultron.features import nightly, planner
from ultron.features.focus import set_focus
from ultron.features.items import ItemStore
from ultron.features.planner import LaterItem, PlannedBlock, PlanResult
from ultron.features.schedule import PlanStore
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
WED_9PM = datetime(2026, 10, 21, 21, 0, tzinfo=LONDON)
THU = date(2026, 10, 22)


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


@pytest.fixture
def env():
    state = State(":memory:")
    configure(state)  # the sport rule, a 35-min routine and a wind-down note

    async def notify(_):
        pass

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
        end_time="07:00",
    )
    workout = add("schedule", "Workout", days="thu", details="After work, from about 18:30")
    gp = add("task", "Book GP appointment", due="2026-10-21", area="Health")
    yield SimpleNamespace(
        state=state,
        store=store,
        gp=gp,
        workout=workout,
        guard=CostGuard(state, cap_usd=20, zone=LONDON, notify=notify),
    )
    state.close()


def plan(*blocks, later=(), notes="Workout first, then the GP call."):
    """Blocks are (start, end, title, item_id) on Thu, or (…, day) for another day."""
    return PlanResult(
        blocks=[
            PlannedBlock(
                day=(rest[0] if rest else THU.isoformat()), start=s, end=e, title=t, item_id=i
            )
            for s, e, t, i, *rest in blocks
        ],
        later=[LaterItem(day=d, title=t, item_id=None) for d, t in later],
        notes=notes,
    )


async def draft(env, llm, days=(THU,), priorities="Gym, then book the GP", feedback=None):
    return await planner.draft_plan(
        env.state,
        env.guard,
        llm,
        model="claude-sonnet-5-5",
        days=list(days),
        now=WED_9PM,
        priorities=priorities,
        feedback=feedback,
    )


async def test_a_plan_that_fits_is_kept_as_drafted(env):
    llm = FakeLLM(
        plan(
            ("18:30", "19:45", "Workout: weights", None),
            ("19:55", "20:15", "Book GP appointment", env.gp.id),
            later=[("2026-10-23", "Recover Agent Opus project"), ("2026-11-30", "too far")],
        )
    )
    d = await draft(env, llm)
    assert [b["title"] for b in d.blocks] == ["Workout: weights", "Book GP appointment"]
    assert d.blocks[1]["item_id"] == env.gp.id and d.cut == []
    assert [x["title"] for x in d.later] == ["Recover Agent Opus project"]  # in-window only
    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert "Thu 126 min" in request["system"] and "Thursday 22 October" in request["system"]
    content = request["messages"][0]["content"]
    assert "The owner's priorities (their words): Gym, then book the GP" in content
    assert "Later days:" in content and "Fri 23 Oct (2026-10-23)" in content


async def test_a_plan_that_breaks_the_rules_gets_one_retry(env):
    llm = FakeLLM(
        plan(("12:00", "13:00", "Lunch-time calls", None)),  # during work
        plan(("18:30", "19:30", "Workout", None)),
    )
    d = await draft(env, llm)
    assert [b["title"] for b in d.blocks] == ["Workout"]
    retry = llm.requests[1]["messages"][-1]["content"]
    assert "isn't inside free time" in retry


async def test_still_broken_after_the_retry_is_trimmed(env):
    bad = plan(
        ("18:30", "20:00", "Workout", None),
        ("20:05", "20:30", "Too close", None),  # needs a 10-minute gap
        ("20:40", "21:20", "Over budget", None),  # 90 + 40 > 126
        ("25:00", "26:00", "Nonsense", None),
    )
    d = await draft(env, FakeLLM(bad, bad))
    assert [b["title"] for b in d.blocks] == ["Workout"]
    assert set(d.cut) == {"Too close", "Over budget", "Nonsense"}


async def test_over_budget_cuts_the_least_important_not_the_latest(env):
    listed = plan(  # most important first, as the prompt asks
        ("20:00", "21:00", "Send 3 job applications", None),
        ("18:30", "19:45", "Tidy the flat", None),  # 60 + 75 > 126
    )
    d = await draft(env, FakeLLM(listed, listed))
    assert [b["title"] for b in d.blocks] == ["Send 3 job applications"]
    assert d.cut == ["Tidy the flat"]


async def test_recovery_focus_lowers_the_budget(env):
    set_focus(env.state, env.store, "recovery", WED_9PM.date())
    llm = FakeLLM(plan(("18:30", "19:30", "Walk", None)))
    await draft(env, llm)
    assert "Thu 90 min" in llm.requests[0]["system"]  # 50% of 180


async def test_no_free_time_is_a_readable_error(env):
    with pytest.raises(planner.PlanError, match="no free time"):
        now = datetime(2026, 10, 21, 21, 50, tzinfo=LONDON)
        await planner.draft_plan(
            env.state, env.guard, FakeLLM(), model="m", days=[now.date()], now=now, priorities=None
        )


async def test_feedback_redraft_includes_the_previous_plan(env):
    await draft(env, FakeLLM(plan(("18:30", "19:30", "Workout", None))))
    llm = FakeLLM(plan(("18:30", "19:00", "Short run", None)))
    d = await draft(env, llm, feedback=["shorter workout"])
    content = llm.requests[0]["messages"][0]["content"]
    assert "Owner's change request 1: shorter workout" in content
    assert "Previous draft: 2026-10-22 18:30–19:30 Workout" in content
    assert d.feedback == ["shorter workout"]


async def test_draft_text_shows_the_day_in_order(env):
    d = await draft(
        env,
        FakeLLM(
            plan(
                ("18:30", "19:45", "Workout: weights", env.workout.id),
                ("19:55", "20:15", "Book GP appointment", env.gp.id),
                later=[("2026-10-23", "Recover Agent Opus project")],
            )
        ),
    )
    text = planner.draft_text(env.state, d, WED_9PM)
    assert text.splitlines() == [
        "📝 Draft plan · Thu 22 Oct · focus: Balanced",
        "Your priorities: Gym, then book the GP",  # so a misread shows before Approve
        "🔒 06:30–18:30 Work · incl. commute",
        "18:30–19:45 Workout: weights (~1h 15m)",  # stands for the 🔒 Workout entry
        "19:55–20:15 Book GP appointment (~20m)",
        "🔒 21:30 Wind down · journal or meditate",
        "Planned 1h 35m of 3h free (53%)",
        "",
        "Later this week:",
        "• Fri 23: Recover Agent Opus project",
        "",
        "Workout first, then the GP call.",
    ]


async def test_draft_text_shows_flexible_entries_and_one_offs(env):
    env.store.add(
        kind="schedule",
        title="Dentist, 19:00",
        details=None,
        project=None,
        area="Health",
        due="2026-10-22",
        question=None,
    )
    d = await draft(env, FakeLLM(plan(("20:20", "20:50", "Book GP appointment", None))))
    lines = planner.draft_text(env.state, d, WED_9PM).splitlines()
    assert lines[2:6] == [
        "🔒 06:30–18:30 Work · incl. commute",
        "🔒 ~18:30 Workout",  # not planned yet, so still shown where it goes
        "🔒 19:00–20:00 Dentist, 19:00 · length guessed",  # a one-off blocks its time
        "20:20–20:50 Book GP appointment (~30m)",
    ]


async def test_approve_writes_the_schedule_and_replanning_replaces(env):
    plans = PlanStore(env.state)
    done = plans.add(THU, "Already done", start="18:30", end="18:40")
    plans.set_status(done.id, "done")
    plans.add(THU, "Old plan block", start="19:00", end="20:00")
    plans.add(date(2026, 10, 23), "Old later item")
    await draft(
        env,
        FakeLLM(
            plan(
                ("18:50", "19:50", "Workout", None),
                later=[("2026-10-23", "Recover Agent Opus project")],
            )
        ),
    )
    approved = planner.approve(env.state)
    assert approved.blocks == 1 and approved.later == 1
    assert [(b.title, b.status) for b in plans.for_day(THU)] == [
        ("Already done", "done"),  # done blocks survive a replan
        ("Workout", "planned"),
    ]
    assert [b.title for b in plans.for_day(date(2026, 10, 23))] == ["Recover Agent Opus project"]
    assert planner.load_draft(env.state) is None
    with pytest.raises(planner.PlanError, match="expired"):
        planner.approve(env.state)


async def test_skip_discards(env):
    await draft(env, FakeLLM(plan(("18:30", "19:30", "Workout", None))))
    planner.discard(env.state)
    assert planner.load_draft(env.state) is None and PlanStore(env.state).for_day(THU) == []


async def test_plan_and_offer_sends_buttons(env):
    offered = []

    class Notifier:
        async def offer(self, text, topic, choices, space=Space.BRIEFS):
            offered.append((text, topic, [k for k, _ in choices], space))

    from contextlib import asynccontextmanager

    llm = FakeLLM(plan(("18:30", "19:30", "Workout", None)))

    @asynccontextmanager
    async def make_llm():
        yield llm

    settings = SimpleNamespace(zone=LONDON, model_smart="claude-sonnet-5-5")
    await planner.plan_and_offer(
        settings,
        env.state,
        env.guard,
        Notifier(),
        make_llm,
        days=[THU],
        priorities=None,
        now=WED_9PM,
    )
    [(text, topic, keys, space)] = offered
    assert topic == planner.OFFER_TOPIC and keys == ["approve", "edit", "skip"]
    assert text.startswith("📝 Draft plan · Thu 22 Oct") and space is Space.BRIEFS


def test_pick_days():
    morning = datetime(2026, 10, 21, 9, 0, tzinfo=LONDON)
    evening = datetime(2026, 10, 21, 18, 0, tzinfo=LONDON)
    wed, thu, fri = date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)
    assert planner.pick_days("", morning) == ([wed, thu], "")  # today + tomorrow
    assert planner.pick_days("gym first", evening) == ([thu, fri], "gym first")
    assert planner.pick_days("today calls", evening) == ([wed], "calls")  # today only
    assert planner.pick_days("Tomorrow", morning) == ([thu, fri], "")


async def test_tonights_suggestions_reach_the_planner(env):
    nightly._save(
        env.state,
        nightly.Session(
            day="2026-10-22",
            sent_at="x",
            suggestions=[{"title": "Call builders (45 min)", "why": "blocker", "item_ids": []}],
        ),
    )
    llm = FakeLLM(plan(("18:30", "19:30", "Workout", None)))
    await draft(env, llm)
    assert "- Call builders (45 min): blocker" in llm.requests[0]["messages"][0]["content"]


async def test_replan_keeps_done_and_missed_blocks(env):
    plans = PlanStore(env.state)
    today = date(2026, 10, 21)
    done = plans.add(today, "Morning routine", start="06:00", end="06:30")
    plans.set_status(done.id, "done")
    missed = plans.add(today, "Admin", start="09:00", end="09:30")
    later = plans.add(today, "Deep work", start="16:00", end="18:00")
    now = datetime(2026, 10, 21, 13, 4, tzinfo=LONDON)
    llm = FakeLLM(plan(("13:10", "14:40", "Deep work (moved)", None, "2026-10-21")))
    d = await planner.draft_plan(
        env.state,
        env.guard,
        llm,
        model="m",
        days=[today],
        now=now,
        priorities="meeting overran",
        replan=True,
    )
    assert d.replan_from == "13:10"
    content = llm.requests[0]["messages"][0]["content"]
    assert "Replanning the rest of today from 13:10." in content
    assert "Done already: Morning routine" in content
    assert "Planned earlier but not done: Admin" in content
    assert "Still planned for later today: Deep work" in content
    assert planner.draft_text(env.state, d, now).startswith("📝 Replan from 13:10 · Wed 21 Oct")
    planner.approve(env.state)
    statuses = {b.title: b.status for b in plans.for_day(today)}
    assert statuses == {
        "Morning routine": "done",
        "Admin": "planned",  # a miss, left for the evening review
        "Deep work (moved)": "planned",
    }
    assert plans.get(later.id).status == "dropped" and plans.get(missed.id).status == "planned"


# --- promises: chat commitments, carry-overs and "First thing" blocks survive plans ---

FRI = date(2026, 10, 23)


async def test_approving_a_plan_never_wipes_a_promise_on_a_later_day(env):
    from ultron.features.daylog import commit_to

    item, promise = commit_to(env.store, PlanStore(env.state), "Call Nina", FRI)
    await draft(
        env, FakeLLM(plan(("18:30", "19:30", "Workout", None), later=[("2026-10-23", "Admin")]))
    )
    planner.approve(env.state)
    fri = {b.title: (b.status, b.source) for b in PlanStore(env.state).for_day(FRI)}
    assert fri == {"Call Nina": ("planned", "commit"), "Admin": ("planned", "plan")}


async def test_promises_must_be_timed_and_are_retried_then_reported(env):
    plans = PlanStore(env.state)
    plans.add(THU, "First thing: book the GP", item_id=env.gp.id, source="callout")
    without = plan(("18:30", "19:30", "Workout", None))
    llm = FakeLLM(without, without)
    d = await draft(env, llm)
    content = llm.requests[0]["messages"][0]["content"]
    assert (
        f"Must be timed:\n- Thu 2026-10-22: [#{env.gp.id}] First thing: book the GP (first thing)"
        in content
    )
    retry = llm.requests[1]["messages"][-1]["content"]
    assert "'First thing: book the GP' is a promise and must be in the plan" in retry
    assert d.cut == ["First thing: book the GP (a promise, still untimed)"]


async def test_a_timed_promise_replaces_its_untimed_line_on_approve(env):
    plans = PlanStore(env.state)
    promise = plans.add(THU, "Book GP appointment", item_id=env.gp.id, source="carry")
    await draft(env, FakeLLM(plan(("18:30", "18:50", "Book the GP", env.gp.id))))
    planner.approve(env.state)
    assert plans.get(promise.id).status == "dropped"
    [timed] = plans.for_day(THU)
    assert (timed.title, timed.start) == ("Book the GP", "18:30")


async def test_recent_slips_reach_the_planner(env):
    from ultron.features import daylog

    entry = env.store.add(
        kind="log",
        title="Day review",
        details="Done: Workout\nSkipped: Send applications\nSend applications: skipped (too tired)"
        "\nReflection: long day",
        project=None,
        area="Admin",
        due=None,
        question=None,
    )
    env.state.set(f"{daylog.KEY_PREFIX}2026-10-21", str(entry.id))
    llm = FakeLLM(plan(("18:30", "19:30", "Workout", None)))
    await draft(env, llm)
    content = llm.requests[0]["messages"][0]["content"]
    assert (
        "Recent days (what slipped, and why):\n- Wed 21: Skipped: Send applications\n"
        "- Wed 21: Send applications: skipped (too tired)\n"
    ) in content
    assert "long day" not in content


# --- two-day plans ---


async def test_two_days_are_planned_checked_and_approved_together(env):
    plans = PlanStore(env.state)
    rough = plans.add(FRI, "Old rough item")  # from an earlier plan's "later" list
    promise = plans.add(FRI, "Call Nina", source="commit")
    two_days = plan(
        ("18:30", "19:30", "Workout", None),
        ("09:00", "11:00", "Job applications", None, "2026-10-23"),
        ("11:10", "11:30", "Call Nina", None, "2026-10-23"),
        later=[("2026-10-24", "Builders")],
    )
    llm = FakeLLM(two_days)
    d = await draft(env, llm, days=(THU, FRI))
    system = llm.requests[0]["system"]
    assert "Thursday 22 October and Friday 23 October" in system
    assert "Thu 126 min, Fri 609 min" in system  # each day its own 70% budget
    content = llm.requests[0]["messages"][0]["content"]
    assert "Friday (Friday 23 October, 2026-10-23):" in content
    assert "Already planned: (untimed) Old rough item" in content
    assert d.days == ["2026-10-22", "2026-10-23"] and d.cut == []

    text = planner.draft_text(env.state, d, WED_9PM).splitlines()
    assert text[0] == "📝 Draft plan · Thu 22 Oct + Fri 23 Oct · focus: Balanced"
    assert "— Thursday 22 Oct —" in text and "— Friday 23 Oct —" in text
    assert "09:00–11:00 Job applications (~2h)" in text

    approved = planner.approve(env.state)
    assert approved.days == [THU, FRI] and approved.blocks == 3
    fri = {b.title: (b.start, b.status) for b in plans.for_day(FRI)}
    assert fri == {"Job applications": ("09:00", "planned"), "Call Nina": ("11:10", "planned")}
    assert plans.get(rough.id).status == "dropped"  # the rough list is replaced by times
    assert plans.get(promise.id).status == "dropped"  # timed now, so not shown twice


async def test_each_day_is_checked_against_its_own_free_time(env):
    wrong = plan(
        ("09:00", "10:00", "Gym", None),  # Thu 09:00 is work
        ("09:00", "10:00", "Gym", None, "2026-10-23"),
        ("12:00", "13:00", "Lunch walk", None, "2026-10-30"),  # not a planned day
    )
    llm = FakeLLM(wrong, wrong)
    d = await draft(env, llm, days=(THU, FRI))
    retry = llm.requests[1]["messages"][-1]["content"]
    assert "Thu: 'Gym' (09:00–10:00) isn't inside free time" in retry
    assert "'Lunch walk' is on 2026-10-30, which isn't one of the days being planned" in retry
    assert [(b["day"], b["title"]) for b in d.blocks] == [("2026-10-23", "Gym")]
    assert set(d.cut) == {"Gym", "Lunch walk"}


async def test_a_day_with_no_time_left_is_skipped(env):
    late = datetime(2026, 10, 21, 21, 50, tzinfo=LONDON)
    llm = FakeLLM(plan(("18:30", "19:30", "Workout", None)))
    d = await planner.draft_plan(
        env.state,
        env.guard,
        llm,
        model="m",
        days=[late.date(), THU],
        now=late,
        priorities=None,
    )
    assert d.days == ["2026-10-22"]


def test_a_one_day_draft_from_before_the_upgrade_is_dropped(env):
    import json

    old = {"day": "2026-10-22", "priorities": None, "blocks": [], "later": [], "notes": ""}
    env.state.set(planner.KEY_DRAFT, json.dumps(old))
    assert planner.load_draft(env.state) is None


async def test_the_draft_names_top_todos_left_out_of_the_week(env):
    d = await draft(env, FakeLLM(plan(("18:30", "19:30", "Workout", None))))
    assert planner.draft_text(env.state, d, WED_9PM).splitlines()[-1] == (
        "⚠️ Not in this plan or the week: Book GP appointment"
    )


async def test_an_intro_goes_above_the_draft(env):
    from contextlib import asynccontextmanager

    offered = []

    class Notifier:
        async def offer(self, text, topic, choices, space):
            offered.append(text)

    @asynccontextmanager
    async def make_llm():
        yield FakeLLM(plan(("18:30", "19:30", "Workout", None)))

    settings = SimpleNamespace(zone=LONDON, model_smart="m")
    await planner.plan_and_offer(
        settings,
        env.state,
        env.guard,
        Notifier(),
        make_llm,
        days=[THU],
        priorities=None,
        now=WED_9PM,
        intro=nightly.AUTO_DRAFT_INTRO,
    )
    assert offered[0].startswith("No priorities from you, so this is drafted")
    assert "\n\n📝 Draft plan · Thu 22 Oct" in offered[0]


async def test_a_set_day_to_do_never_goes_on_another_day(env):
    gp = env.store.update(env.gp.id, due="2026-10-26", due_on=True)
    early = plan(
        ("18:30", "19:30", "Workout", None),
        ("19:40", "20:00", "Call GP", gp.id),
        later=[("2026-10-24", "Call GP early"), ("2026-10-26", "Call GP")],
    )
    early.later[0].item_id = early.later[1].item_id = gp.id
    llm = FakeLLM(early, early)
    d = await draft(env, llm)
    content = llm.requests[0]["messages"][0]["content"]
    assert "Book GP appointment · ON 2026-10-26" in content
    assert "never earlier or later" in llm.requests[0]["system"]
    retry = llm.requests[1]["messages"][-1]["content"]
    assert "'Call GP' is set for 2026-10-26 only, not 2026-10-22" in retry
    assert [b["title"] for b in d.blocks] == ["Workout"]
    assert d.cut == ["Call GP (not its day)"]
    assert [x["day"] for x in d.later] == ["2026-10-26"]


# --- fewer, bigger blocks; sport; batched admin (2026-10-25) ---

FRI = date(2026, 10, 23)


def fri(*blocks):
    """Blocks are (start, end, title, item_id[, also_ids]) on Friday."""
    return PlanResult(
        blocks=[
            PlannedBlock(
                day=FRI.isoformat(),
                start=s,
                end=e,
                title=t,
                item_id=i,
                also_ids=list(rest[0]) if rest else [],
            )
            for s, e, t, i, *rest in blocks
        ],
        later=[],
        notes="Plan.",
    )


async def test_too_many_blocks_are_sent_back_then_cut_to_the_limit(env):
    many = [(f"{h:02d}:00", f"{h:02d}:30", f"Thing {h}", None) for h in range(8, 18)]  # 10
    llm = FakeLLM(fri(*many), fri(*many))
    d = await draft(env, llm, days=(FRI,))
    retry = llm.requests[1]["messages"][-1]["content"]
    assert f"at most {CONFIGURED.max_blocks} a day" in retry
    assert len(d.blocks) == CONFIGURED.max_blocks
    assert d.cut == ["Thing 16", "Thing 17"]  # the least important (last listed) go


async def test_too_many_short_blocks_must_be_batched(env):
    short = [(f"{h:02d}:00", f"{h:02d}:15", f"Call {h}", None) for h in range(9, 14)]  # 5
    llm = FakeLLM(fri(*short), fri(("09:00", "10:00", "Admin: calls", None)))
    d = await draft(env, llm, days=(FRI,))
    assert "Batch them into one admin block" in llm.requests[1]["messages"][-1]["content"]
    assert [b["title"] for b in d.blocks] == ["Admin: calls"]
    assert "at most 8 blocks a day" in llm.requests[0]["system"]


async def test_on_a_sport_day_the_gym_goes_later(env):
    gym = add_item(env, "schedule", "Workout", days="fri", details="Morning, about 2 hours")
    add_item(env, "schedule", "Paddle", due=FRI.isoformat(), start_time="07:00", end_time="08:30")
    too_soon = fri(("08:40", "10:00", "Workout: weights", gym.id))
    llm = FakeLLM(too_soon, too_soon)
    d = await draft(env, llm, days=(FRI,))
    content = llm.requests[0]["messages"][0]["content"]
    assert "Fixed: Paddle 07:00–08:30 (one-off, sport)" in content
    retry = llm.requests[1]["messages"][-1]["content"]
    assert "too close to Paddle: the workout goes later that day, from 10:00" in retry
    assert d.blocks == [] and d.cut == ["Workout: weights (too soon after sport)"]


async def test_a_batched_block_covers_several_to_dos_and_ticks_them_all(env):
    chase = add_item(env, "task", "Chase Raj", area="Wealth")
    msg = add_item(env, "task", "Message Jo")
    llm = FakeLLM(fri(("10:00", "10:45", "Admin: chase Raj, message Jo", chase.id, [msg.id, 999])))
    d = await draft(env, llm, days=(FRI,))
    assert d.blocks[0]["also_ids"] == [msg.id]  # unknown ids dropped
    planner.approve(env.state)
    [block] = PlanStore(env.state).for_day(FRI)
    assert block.item_ids == [chase.id, msg.id]
    from ultron.features.schedule import ScheduleEdits, apply_edits

    apply_edits(env.state, ScheduleEdits(ticked=[block.id]))
    assert {env.store.get(i).status for i in (chase.id, msg.id)} == {"done"}


async def test_the_track_record_and_week_capacity_reach_the_planner(env):
    plans = PlanStore(env.state)
    for n, status in enumerate(["done", "partial", "skipped"]):
        b = plans.add(
            date(2026, 10, 18 + n), "Learn Spanish", start="19:00", end="19:30", item_id=env.gp.id
        )
        plans.set_status(b.id, status)
    llm = FakeLLM(fri(("10:00", "11:00", "Deep work", None)))
    await draft(env, llm, days=(FRI,))
    content = llm.requests[0]["messages"][0]["content"]
    assert "Track record (how planned blocks really went):" in content
    assert "3 timed blocks: 1 done, 1 partly, 1 missed" in content
    assert f"[#{env.gp.id}] Book GP appointment: 3 blocks of ~30m" in content
    assert "Week capacity:" in content and "plannable" in content


def add_item(env, kind, title, **kw):
    values = dict(details=None, project=None, area="Admin", due=None, question=None)
    values.update(kw)
    return env.store.add(kind=kind, title=title, **values)
