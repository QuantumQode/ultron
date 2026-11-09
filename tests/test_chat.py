from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import make_settings

from ultron.brain import chat as chat_module
from ultron.brain import prompts
from ultron.brain.chat import History, chat
from ultron.features.items import ItemStore
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=LONDON)


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool(name, **args):
    return SimpleNamespace(type="tool_use", id=f"tu_{name}", name=name, input=args)


def response(*blocks, stop=None, model="claude-haiku-5-5"):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    usage = SimpleNamespace(input_tokens=2000, output_tokens=100)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, model=model)


class FakeLLM:
    """Replays scripted responses; records each request and which endpoint it hit."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create("messages"))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create("beta")))

    def _create(self, endpoint):
        async def create(**kwargs):
            self.requests.append({"endpoint": endpoint, **kwargs})
            return self.responses.pop(0)

        return create


@pytest.fixture
def env():
    state = State(":memory:")

    async def notify(_):
        pass

    guard = CostGuard(state, cap_usd=20, zone=LONDON, notify=notify)
    yield SimpleNamespace(
        state=state, guard=guard, store=ItemStore(state), settings=make_settings()
    )
    state.close()


async def ask(env, llm, message, space=Space.CHAT):
    @asynccontextmanager
    async def make_llm():
        yield llm

    return await chat(env.settings, env.state, env.guard, make_llm, message, space, now=NOW)


def add_task(store, title, **kw):
    values = dict(
        kind="task", title=title, details=None, project=None, area="Admin", due=None, question=None
    )
    values.update(kw)
    return store.add(**values)


async def test_simple_answer_on_haiku_with_context(env):
    env.store.add(
        kind="schedule",
        title="Gym",
        details=None,
        project=None,
        area="Health",
        due=None,
        question=None,
        days="mon",
        start_time="07:00",
        end_time="08:00",
    )
    llm = FakeLLM(response(text("Morning! Gym is at 7.")))
    reply = await ask(env, llm, "what's on today?")
    assert reply.text == "Morning! Gym is at 7." and not reply.escalated
    request = llm.requests[0]
    assert request["endpoint"] == "messages" and request["model"] == "claude-haiku-5-5"
    assert request["output_config"] == {"effort": "low"}
    assert prompts.ESCALATE.strip() in request["system"][0]["text"]
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    context = request["system"][1]["text"]
    assert "Tuesday 20 October 2026" in context and "Gym" in context
    assert "think_harder" in [t["name"] for t in request["tools"]]
    assert History(env.state).recent("chat") == [
        {"role": "user", "content": "what's on today?"},
        {"role": "assistant", "content": "Morning! Gym is at 7."},
    ]


async def test_tool_use_round_trip(env):
    add_task(env.store, "Call builders", project="Acme")
    llm = FakeLLM(
        response(
            tool(
                "find_items",
                query="builders",
                kind=None,
                project=None,
                area=None,
                due_before=None,
                include_done=False,
            )
        ),
        response(text("You've got: call builders (Acme).")),
    )
    reply = await ask(env, llm, "anything about builders?")
    assert "call builders" in reply.text
    second = llm.requests[1]["messages"]
    [result] = second[-1]["content"]
    assert result["type"] == "tool_result" and "Call builders" in result["content"]
    assert not result["is_error"]
    assert second[-2]["role"] == "assistant"  # full assistant content kept between rounds


async def test_ticking_off_and_pushing_back_a_task(env):
    item = add_task(env.store, "Book marathon", due="2026-11-01")
    llm = FakeLLM(
        response(
            tool("update_item", id=item.id, status=None, title=None, due="2026-11-20", project=None)
        ),
        response(
            tool("update_item", id=item.id, status="done", title=None, due=None, project=None)
        ),
        response(text("Done and dusted.")),
    )
    reply = await ask(env, llm, "move marathon to the 20th, actually no I did it")
    updated = env.store.get(item.id)
    assert updated.due == "2026-11-20" and updated.times_moved == 1
    assert updated.status == "done" and reply.effects.items_changed


async def test_add_task_and_remember(env):
    llm = FakeLLM(
        response(
            tool(
                "add_task",
                title="Call mum",
                area="Relationships",
                project=None,
                due="2026-10-23",
                on_day=True,
            ),
            tool("remember", fact="Doesn't work Fridays"),
        ),
        response(text("Added, and noted.")),
    )
    reply = await ask(env, llm, "remind me to call mum friday, and remember I don't work fridays")
    kinds = {(i.kind, i.title) for i in env.store.all()}
    assert ("task", "Call mum") in kinds and ("fact", "Doesn't work Fridays") in kinds
    [mum] = [i for i in env.store.all() if i.title == "Call mum"]
    assert mum.due_on  # "call mum friday" happens on Friday, not by it
    assert len(reply.effects.remembered) == 1
    # both results go back in ONE user message
    assert len(llm.requests[1]["messages"][-1]["content"]) == 2


async def test_bad_tool_call_returns_an_error_result(env):
    llm = FakeLLM(
        response(tool("update_item", id=999, status="done", title=None, due=None, project=None)),
        response(text("I couldn't find that one.")),
    )
    await ask(env, llm, "tick off thing")
    [result] = llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] and "find_items" in result["content"]


async def test_think_harder_escalates_to_sonnet_with_fallback(env):
    llm = FakeLLM(
        response(tool("think_harder", reason="career decision")),
        response(text("Let's weigh it up…"), model="claude-sonnet-5-5"),
    )
    reply = await ask(env, llm, "should I quit and go full time on Acme?")
    assert reply.escalated and reply.model == "claude-sonnet-5-5"
    sonnet = llm.requests[1]
    assert sonnet["endpoint"] == "beta" and sonnet["model"] == "claude-sonnet-5-5"
    assert sonnet["betas"] == ["server-side-fallback-2026-07-01"]
    assert sonnet["extra_body"] == {"fallbacks": "default"}
    assert sonnet["output_config"] == {"effort": "medium"}
    assert "think_harder" not in [t["name"] for t in sonnet["tools"]]
    assert prompts.ESCALATE.strip() not in sonnet["system"][0]["text"]
    assert reply.cost_usd > 0


async def test_bang_prefix_goes_straight_to_sonnet(env):
    llm = FakeLLM(response(text("Here's my view."), model="claude-sonnet-5-5"))
    await ask(env, llm, "! what should I focus on this week")
    request = llm.requests[0]
    assert request["model"] == "claude-sonnet-5-5"
    assert request["messages"][-1]["content"] == "what should I focus on this week"


async def test_reflect_uses_sonnet_and_its_own_history(env):
    llm = FakeLLM(response(text("What's making you feel that way?"), model="claude-sonnet-5-5"))
    await ask(env, llm, "feeling stuck lately", space=Space.REFLECT)
    request = llm.requests[0]
    assert request["model"] == "claude-sonnet-5-5"
    assert request["system"][0]["text"] == prompts.REFLECT
    assert History(env.state).recent("reflect") and not History(env.state).recent("chat")


async def test_budget_used_up_stays_on_haiku(env):
    env.state.record_usage(
        model="m",
        purpose="x",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_usd=25.0,
    )
    llm = FakeLLM(response(text("Quick answer.")))
    reply = await ask(env, llm, "! big question")
    request = llm.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert "think_harder" not in [t["name"] for t in request["tools"]]
    assert reply.note and "budget" in reply.note


async def test_refusal_gets_a_polite_reply(env):
    llm = FakeLLM(response(text(""), stop="refusal"))
    reply = await ask(env, llm, "something")
    assert reply.text == "I can't help with that one."


async def test_history_is_folded_into_a_summary(env, monkeypatch):
    monkeypatch.setattr(chat_module, "FOLD_AT", 6)
    monkeypatch.setattr(chat_module, "HISTORY", 2)
    history = History(env.state)
    for n in range(2):
        history.add("chat", "user", f"old question {n}")
        history.add("chat", "assistant", f"old answer {n}")
    llm = FakeLLM(response(text("Answer.")), response(text("Owner asked two old questions.")))
    await ask(env, llm, "new question")
    assert history.summary("chat") == "Owner asked two old questions."
    assert [m["content"] for m in history.recent("chat", 10)] == ["new question", "Answer."]
    summary_request = llm.requests[1]
    assert "old question 0" in summary_request["messages"][0]["content"]
    # the next reply sees the summary in its context
    llm2 = FakeLLM(response(text("ok")))
    await ask(env, llm2, "and now?")
    assert "Owner asked two old questions." in llm2.requests[0]["system"][1]["text"]


def test_history_always_starts_with_the_owner(env):
    history = History(env.state)
    history.add("chat", "assistant", "orphan reply")
    history.add("chat", "user", "hi")
    assert history.recent("chat")[0]["role"] == "user"


async def test_email_search_and_waiting_replies_reach_the_chat(env):
    from datetime import timedelta

    from ultron.features.triage import EmailStore, Triaged
    from ultron.integrations.mail import Email

    env.settings = make_settings(yahoo_user="me@yahoo.co.uk", yahoo_app_password="app")
    mum = Email(1, "<1@x>", "mum@x.com", "Mum", "Sunday", NOW - timedelta(days=1), "")
    EmailStore(env.state).save([Triaged(mum, "important", "person", "Lunch Sunday?", True)])
    llm = FakeLLM(
        response(
            tool("search_email", query="mum", days_back=None, category=None, needs_reply=None)
        ),
        response(text("Mum asked about Sunday lunch.")),
    )
    await ask(env, llm, "did mum email?")
    first = llm.requests[0]
    assert "Lunch Sunday?" in first["system"][1]["text"]  # waiting-for-reply context
    [result] = llm.requests[1]["messages"][-1]["content"]
    assert result["content"].startswith("<emails>") and "Mum <mum@x.com>" in result["content"]


async def test_focus_reaches_the_chat_context(env):
    from datetime import date

    from ultron.features.focus import set_focus

    set_focus(env.state, env.store, "wealth", date(2026, 10, 20))
    llm = FakeLLM(response(text("ok")))
    await ask(env, llm, "what should I do?")
    context = llm.requests[0]["system"][1]["text"]
    assert "Focus: Wealth" in context and "Must-dos: Deep-work block on income work" in context


async def test_a_crashing_tool_gives_an_error_result_not_a_failed_reply(env, monkeypatch):
    def broken(*_args, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(chat_module, "run_tool", broken)
    llm = FakeLLM(
        response(tool("find_items", query="gym")),
        response(text("I couldn't look that up just now.")),
    )
    reply = await ask(env, llm, "when's gym?")
    assert reply.text == "I couldn't look that up just now."
    [result] = llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] and "RuntimeError" in result["content"]


async def test_job_health_reaches_the_chat_context(env):
    env.state.record_job_failure("sync", "APIResponseError: text too long")
    llm = FakeLLM(response(text("ok")))
    await ask(env, llm, "why did sync fail?")
    context = llm.requests[0]["system"][1]["text"]
    assert "Ultron's own jobs:" in context
    assert "sync: FAILING" in context and "text too long" in context
