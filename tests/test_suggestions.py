from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import CONFIGURED, make_settings
from test_chat import FakeLLM as ChatLLM
from test_chat import response, text, tool

from ultron.brain import chat as chat_module
from ultron.brain.chat import History, chat
from ultron.errors import UserFacingError
from ultron.features import focus, habits, rituals, suggestions
from ultron.features.board import render_item
from ultron.features.items import ItemStore
from ultron.features.suggestions import Proposal
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 21, 19, 0, tzinfo=LONDON)
TODAY = NOW.date()


@pytest.fixture
def env():
    state = State(":memory:")

    async def notify(_):
        pass

    yield SimpleNamespace(
        state=state,
        store=ItemStore(state),
        settings=make_settings(),
        guard=CostGuard(state, cap_usd=20, zone=LONDON, notify=notify),
    )
    state.close()


def add(store, kind, title, **kw):
    values = dict(details=None, project=None, area="Health", due=None, question=None)
    values.update(kw)
    return store.add(kind=kind, title=title, **values)


def p(**kw):
    kw.setdefault("summary", "A change")
    kw.setdefault("why", "They said so")
    return Proposal(**kw)


def propose(env, at=NOW, **kw):
    return suggestions.propose(env.state, p(**kw), "reflect", at)


# --- checking ---


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        (dict(kind="focus", action="add"), "can be: switch, create"),
        (dict(kind="habit", action="pause", target_id=99, until="2026-11-03"), "no open item"),
        (dict(kind="habit", action="add", text="Stretch", days="mon,funday"), "Unknown day"),
        (dict(kind="habit", action="add"), "needs its text"),
        (dict(kind="focus", action="switch", focus="zen"), "don't know a mode"),
        (dict(kind="focus", action="create", focus="Health", text="x"), "already a focus mode"),
        (dict(kind="ritual", action="set", ritual="lunch", time="12:00"), "ritual must be"),
        (dict(kind="ritual", action="set", ritual="morning_brief", time="7am"), "does not match"),
        (
            dict(kind="ritual", action="set", ritual="morning_brief", time="07:00", day="sat"),
            "Only",
        ),
        (dict(kind="tone", action="set"), "Describe the coaching style"),
    ],
)
def test_bad_proposals_say_what_to_fix(env, fields, error):
    with pytest.raises(ValueError, match=error):
        propose(env, **fields)


def test_target_must_be_the_right_kind_and_pause_in_the_future(env):
    goal = add(env.store, "goal", "Run a 10k")
    with pytest.raises(ValueError, match="is a goal, not a habit"):
        propose(env, kind="habit", action="drop", target_id=goal.id)
    habit = add(env.store, "habit", "Stretch")
    with pytest.raises(ValueError, match="after today"):
        propose(env, kind="habit", action="pause", target_id=habit.id, until="2026-10-21")


def test_card_text_and_buttons(env):
    gym = add(env.store, "habit", "Workout", days="mon,tue,fri,sat")
    change = propose(
        env,
        kind="habit",
        action="change",
        target_id=gym.id,
        days="tue, MON,fri",
        summary="Train Mon, Tue, Fri only",
        why="Saturdays keep slipping",
    )
    assert change.proposal.days == "mon,tue,fri"  # tidied
    assert suggestions.card_text(env.settings, env.state, change).splitlines() == [
        "💡 Train Mon, Tue, Fri only",
        "Changes Workout · days: mon, tue, fri",
        "Why: Saturdays keep slipping",
    ]
    assert [k for k, _ in suggestions.choices(change)] == [
        f"{change.id}:apply",
        f"{change.id}:tweak",
        f"{change.id}:dismiss",
    ]


def test_ritual_card_shows_the_old_and_new_time(env):
    change = propose(env, kind="ritual", action="set", ritual="evening_review", time="20:30")
    assert "Evening review: 21:00 → 20:30" in suggestions.card_text(env.settings, env.state, change)


# --- limits ---


def test_three_a_day_and_no_repeats(env):
    propose(env, kind="fact", action="add", text="Hates early meetings", summary="One")
    with pytest.raises(ValueError, match="already waiting"):
        propose(env, kind="fact", action="add", text="Hates early meetings", summary="one")
    propose(env, kind="fact", action="add", text="Two", summary="Two")
    third = propose(env, kind="fact", action="add", text="Three", summary="Three")
    with pytest.raises(ValueError, match="Already 3 suggestions today"):
        propose(env, kind="fact", action="add", text="Four", summary="Four")
    assert "Suggestions left today: 0 of 3" in suggestions.context_lines(env.state, NOW)
    suggestions.dismiss(env.state, third.id, NOW)
    tomorrow = NOW + timedelta(days=1)
    with pytest.raises(ValueError, match="dismissed this on 21 Oct"):
        propose(env, at=tomorrow, kind="fact", action="add", text="Three", summary="Three")
    later = NOW + timedelta(days=31)
    assert propose(env, at=later, kind="fact", action="add", text="Three", summary="Three")
    lines = suggestions.context_lines(env.state, tomorrow)
    assert "- Three" in lines[lines.index(next(x for x in lines if "dismissed" in x)) + 1 :]


def test_a_card_expires_after_a_week(env):
    change = propose(env, kind="fact", action="add", text="Likes tea")
    with pytest.raises(UserFacingError, match="expired"):
        suggestions.apply(env.settings, env.state, change.id, NOW + timedelta(days=7))
    assert suggestions.get(env.state, change.id).status == "expired"


# --- applying ---


def apply(env, change):
    return suggestions.apply(env.settings, env.state, change.id, NOW)


def test_apply_facts_goals_and_tone(env):
    out = apply(env, propose(env, kind="fact", action="add", text="Prefers calls to texts"))
    assert out.text == "✅ Applied: A change" and out.items_changed and not out.reschedule
    fact = next(i for i in env.store.all() if i.kind == "fact")
    assert fact.title == "Prefers calls to texts"
    with pytest.raises(UserFacingError, match="Already applied"):
        apply(env, suggestions.get(env.state, 1))

    goal = add(env.store, "goal", "Run a 10k")
    change = propose(env, kind="goal", action="drop", target_id=goal.id, summary="Drop 10k")
    apply(env, change)
    assert env.store.get(goal.id).status == "dropped"
    assert suggestions.get(env.state, change.id).before["status"] == "open"

    apply(env, propose(env, kind="tone", action="set", text="Blunt, numbers first", summary="T1"))
    later = NOW + timedelta(days=1)  # a second tone change replaces the first
    second = suggestions.propose(
        env.state, p(kind="tone", action="set", text="Gentler", summary="T2"), "chat", later
    )
    suggestions.apply(env.settings, env.state, second.id, later)
    tones = [i.title for i in env.store.all() if i.title.startswith("Coaching style")]
    assert tones == ["Coaching style: Gentler"]
    assert (
        suggestions.get(env.state, second.id).before["title"]
        == "Coaching style: Blunt, numbers first"
    )


def test_apply_habits_add_change_and_pause(env):
    apply(
        env, propose(env, kind="habit", action="add", text="Stretch", days="mon,thu", area="Health")
    )
    stretch = next(i for i in env.store.all() if i.kind == "habit")
    assert (stretch.days, stretch.area) == ("mon,thu", "Health")

    change = propose(
        env, kind="habit", action="pause", target_id=stretch.id, until="2026-11-03", summary="P"
    )
    apply(env, change)
    paused = env.store.get(stretch.id)
    assert not habits.due_on(paused, date(2026, 10, 26))  # a Monday, while paused
    assert habits.due_on(paused, date(2026, 11, 5))  # Thursday after the pause
    block, line = render_item(paused, TODAY, LONDON, routine_minutes=CONFIGURED.routine_minutes)
    assert line == "Stretch · paused until Tue 03 Nov"
    assert suggestions.get(env.state, change.id).before == {
        "item": stretch.id,
        "title": "Stretch",
        "status": "open",
        "days": "mon,thu",
        "paused_until": None,
    }


def test_apply_focus_switch_and_create(env):
    apply(env, propose(env, kind="focus", action="switch", focus="health 2w", summary="F1"))
    assert focus.current(env.state).label.startswith("Health")
    change = propose(
        env, kind="focus", action="create", focus="Calm", text="mindfulness first", summary="F2"
    )
    apply(env, change)
    assert "calm" in focus.custom_modes(env.store)
    assert suggestions.get(env.state, change.id).before["added"]


def test_apply_a_ritual_time_and_it_survives_a_restart(env):
    out = apply(
        env, propose(env, kind="ritual", action="set", ritual="evening_review", time="20:30")
    )
    assert out.reschedule and env.settings.nightly_time == time(20, 30)
    weekly = propose(
        env,
        kind="ritual",
        action="set",
        ritual="weekly_review",
        time="18:00",
        day="sat",
        summary="W",
    )
    apply(env, weekly)
    assert (env.settings.weekly_review_day, env.settings.weekly_review_time) == ("sat", time(18))
    assert suggestions.get(env.state, weekly.id).before == {
        "ritual": {"weekly_review_time": "", "weekly_review_day": ""}
    }
    fresh = make_settings()
    rituals.load(fresh, env.state)  # what happens on the next start
    assert fresh.nightly_time == time(20, 30) and fresh.weekly_review_day == "sat"


def test_apply_checks_again_in_case_things_moved_on(env):
    habit = add(env.store, "habit", "Stretch")
    change = propose(env, kind="habit", action="drop", target_id=habit.id)
    env.store.set_status(habit.id, "dropped")
    with pytest.raises(UserFacingError, match="Can't apply that any more"):
        apply(env, change)


# --- tweak ---


class TweakLLM:
    def __init__(self, revised):
        self.messages = self
        self.revised, self.requests = revised, []

    async def parse(self, **kwargs):
        self.requests.append(kwargs)
        usage = SimpleNamespace(input_tokens=500, output_tokens=100)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.revised, usage=usage)


async def test_tweak_redrafts_the_card(env):
    habit = add(env.store, "habit", "Workout", days="mon,tue,fri,sat")
    old = propose(env, kind="habit", action="change", target_id=habit.id, days="mon,fri")
    revised = p(
        kind="habit",
        action="change",
        target_id=habit.id,
        days="tue,fri",
        summary="Tue and Fri",
        why="",
    )
    llm = TweakLLM(revised)

    @asynccontextmanager
    async def make_llm():
        yield llm

    for n in range(2):  # the daily limit doesn't count redrafts
        propose(env, kind="fact", action="add", text=f"F{n}", summary=f"F{n}")
    new = await suggestions.tweak(
        env.settings, env.state, env.guard, make_llm, old.id, "Tue not Mon", NOW
    )
    assert new.proposal.days == "tue,fri" and new.source == "tweak"
    assert new.proposal.why == "They said so"  # kept from the original
    assert suggestions.get(env.state, old.id).status == "tweaked"
    request = llm.requests[0]
    assert (
        request["model"] == env.settings.model_fast
        and "<tweak>\nTue not Mon" in (request["messages"][0]["content"])
    )
    with pytest.raises(UserFacingError, match="Already tweaked"):
        suggestions.start_tweak(env.state, old.id, NOW)


# --- from a chat reply ---


async def ask(env, llm, message, space=Space.REFLECT):
    @asynccontextmanager
    async def make_llm():
        yield llm

    return await chat(env.settings, env.state, env.guard, make_llm, message, space, now=NOW)


async def test_the_coach_proposes_a_card(env):
    args = dict(
        kind="ritual",
        action="set",
        summary="Evening review at 20:30",
        why="21:00 runs into wind-down",
        target_id=None,
        text=None,
        area=None,
        days=None,
        until=None,
        focus=None,
        ritual="evening_review",
        time="20:30",
        day=None,
    )
    llm = ChatLLM(
        response(tool("suggest_change", **args), tool("suggest_change", **args), model="s"),
        response(text("I've put a card up for that."), model="s"),
    )
    answer = await ask(env, llm, "21:00 is too late for the review")
    assert len(answer.effects.suggested) == 1
    results = llm.requests[1]["messages"][-1]["content"]
    assert results[0]["content"].startswith("Card sent: Evening review at 20:30")
    assert "One suggestion per reply" in results[1]["content"]
    assert "Suggestion cards waiting for a tap" in llm.requests[0]["system"][1]["text"]
    assert any(t["name"] == "suggest_change" for t in llm.requests[0]["tools"])
    assert env.settings.nightly_time == time(21, 0)  # nothing applied yet
    assert "⚠️" not in answer.text


def _waiting_card(env):
    proposal = Proposal(
        kind="tone", action="set", summary="Gentler on Sundays", why="asked", text="gentler"
    )
    return suggestions.propose(env.state, proposal, "chat", NOW)


async def test_a_claimed_card_that_was_never_made_gets_a_warning(env):
    llm = ChatLLM(response(text("I've sent a card to change your Workout days. Tap Apply.")))
    answer = await ask(env, llm, "move my workouts to Tue, Fri and Sat", Space.CHAT)
    assert answer.text.endswith(chat_module.NO_CARD)
    assert chat_module.NO_CARD in History(env.state).recent("chat")[-1]["content"]


async def test_a_claimed_apply_gets_a_warning(env):
    llm = ChatLLM(response(text("Applied. Your Workout entry is now Tue, Fri and Sat.")))
    answer = await ask(env, llm, "great, do it")
    assert chat_module.NOT_APPLIED in answer.text and chat_module.NO_CARD in answer.text


async def test_mentioning_a_waiting_card_is_fine(env):
    _waiting_card(env)
    llm = ChatLLM(response(text("That card is waiting below: tap Apply when you're happy.")))
    answer = await ask(env, llm, "did you do it?")
    assert "⚠️" not in answer.text


async def test_job_applications_are_not_card_claims(env):
    llm = ChatLLM(response(text("You applied for 3 roles this week; aim for 2 more.")))
    answer = await ask(env, llm, "how are applications going?")
    assert "⚠️" not in answer.text


async def test_typed_apply_points_at_the_buttons_without_calling_claude(env):
    llm = ChatLLM()  # any call would fail: no scripted responses
    answer = await ask(env, llm, "Apply")
    assert "No card is waiting" in answer.text
    change = _waiting_card(env)
    answer = await ask(env, llm, "apply.")
    assert answer.text.endswith(change.summary) and not llm.requests
    assert suggestions.get(env.state, change.id).status == "proposed"


# --- in the bot ---


async def test_apply_button(env):
    from ultron.bot.handlers import Deps, suggest_button

    # The button checks the card's age against the real clock, so it must be made now
    now = datetime.now(LONDON)
    change = propose(env, at=now, kind="ritual", action="set", ritual="morning_brief", time="06:30")
    rescheduled = []
    deps = Deps(
        settings=env.settings, state=env.state, guard=None, reschedule=lambda: rescheduled.append(1)
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}))
    replies, answers = [], []

    async def reply_text(body, **_):
        replies.append(body)

    async def answer(body=None):
        answers.append(body)

    async def edit(reply_markup):
        pass

    message = SimpleNamespace(
        chat=SimpleNamespace(id=1, type="private"),
        message_thread_id=None,
        is_topic_message=False,
        reply_text=reply_text,
    )
    query = SimpleNamespace(
        data=f"offer:suggest:{change.id}:apply",
        message=message,
        answer=answer,
        edit_message_reply_markup=edit,
    )
    await suggest_button(SimpleNamespace(callback_query=query), context)
    assert replies == ["✅ Applied: A change"] and rescheduled == [1]
    assert env.settings.morning_brief_time == time(6, 30)
    await suggest_button(SimpleNamespace(callback_query=query), context)
    assert replies[-1] == "Already applied."


# --- the schedule as the one place for times (2026-10-25) ---


def test_changing_sleep_moves_the_wake_time_and_the_wake_habit_follows(env):
    sleep = add(
        env.store,
        "schedule",
        "Sleep",
        days="mon,tue,wed,thu,fri,sat,sun",
        start_time="22:00",
        end_time="06:00",
    )
    habit = add(env.store, "habit", "Wake up 6am every day")
    change = propose(env, kind="schedule", action="change", target_id=sleep.id, when="22:00-07:00")
    text = suggestions.detail(env.settings, env.state, change.proposal)
    assert "22:00–06:00 → 22:00–07:00" in text and "you'd wake at 07:00" in text
    apply(env, change)
    assert (env.store.get(sleep.id).start_time, env.store.get(sleep.id).end_time) == (
        "22:00",
        "07:00",
    )
    assert env.store.get(habit.id).title == "Wake up 7am every day"
    stored = suggestions.get(env.state, change.id)
    assert stored.before["end_time"] == "06:00"
    assert stored.before["habits"] == {str(habit.id): "Wake up 6am every day"}


def test_a_wake_time_as_a_habit_is_turned_away(env):
    habit = add(env.store, "habit", "Wake up 6am every day")
    with pytest.raises(ValueError, match="lives in the Sleep entry"):
        propose(
            env, kind="habit", action="change", target_id=habit.id, text="Wake up 7am every day"
        )


def test_schedule_entries_can_change_days_or_be_dropped(env):
    gym = add(env.store, "schedule", "Workout", days="mon,tue,fri,sat")
    apply(env, propose(env, kind="schedule", action="change", target_id=gym.id, days="mon,wed,fri"))
    assert env.store.get(gym.id).days == "mon,wed,fri"
    apply(env, propose(env, kind="schedule", action="drop", target_id=gym.id, summary="No gym"))
    assert env.store.get(gym.id).status == "dropped"


def test_the_routine_follows_waking_so_its_time_cant_be_set(env):
    routine = add(env.store, "schedule", "Morning routine", days="mon", anchor="wake", minutes=35)
    with pytest.raises(ValueError, match="change the Sleep entry instead"):
        propose(env, kind="schedule", action="change", target_id=routine.id, when="06:00-06:35")
    text = render_item(
        env.store.get(routine.id), TODAY, LONDON, routine_minutes=CONFIGURED.routine_minutes
    )[1]
    assert text == "Mon · on waking, 35 min · Morning routine"
