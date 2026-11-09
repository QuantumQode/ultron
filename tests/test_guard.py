from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ultron.ops.guard import CostGuard, Tokens, cost_usd, run_job
from ultron.state import State

LONDON = ZoneInfo("Europe/London")


@pytest.fixture
def state():
    s = State(":memory:")
    yield s
    s.close()


class Recorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


def usage(input=0, output=0, cache_write=None, cache_read=None):
    return SimpleNamespace(
        input_tokens=input,
        output_tokens=output,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
    )


# --- cost maths ---


def test_sonnet_cost():
    # 1M in at $2 + 1M out at $10
    assert cost_usd("claude-sonnet-5-5", Tokens(1_000_000, 1_000_000, 0, 0)) == pytest.approx(12)


def test_cache_tokens_priced_separately():
    tokens = Tokens(input=0, output=0, cache_write=1_000_000, cache_read=1_000_000)
    # write 1.25 × $4 + read $0.20
    assert cost_usd("claude-opus-5-5", tokens) == pytest.approx(5.20)


def test_haiku_long_prompt_tier():
    short = cost_usd("claude-haiku-5-5", Tokens(100_000, 1_000, 0, 0))
    long = cost_usd("claude-haiku-5-5", Tokens(100_001, 1_000, 0, 0))
    assert short == pytest.approx((100_000 * 0.10 + 1_000 * 0.50) / 1e6)
    assert long == pytest.approx((100_001 * 0.50 + 1_000 * 2.50) / 1e6)


def test_unknown_model_uses_highest_price():
    assert cost_usd("claude-mystery", Tokens(0, 1_000_000, 0, 0)) == pytest.approx(20)


def test_tokens_from_usage_handles_none_cache_fields():
    assert Tokens.from_usage(usage(10, 5)) == Tokens(10, 5, 0, 0)


# --- monthly cap ---


async def test_warns_once_at_80_percent_then_caps(state):
    notify = Recorder()
    now = datetime(2026, 10, 19, 12, tzinfo=LONDON)
    guard = CostGuard(state, cap_usd=10, zone=LONDON, notify=notify, clock=lambda: now)

    # $8 of Sonnet output = 80%
    await guard.record(model="claude-sonnet-5-5", purpose="chat", usage=usage(output=800_000))
    assert len(notify.messages) == 1 and "80%" in notify.messages[0]
    assert guard.allow(essential=False)

    # a tiny extra call doesn't repeat the warning
    await guard.record(model="claude-sonnet-5-5", purpose="chat", usage=usage(output=1_000))
    assert len(notify.messages) == 1

    # push past the cap
    await guard.record(model="claude-sonnet-5-5", purpose="chat", usage=usage(output=300_000))
    assert len(notify.messages) == 2 and "cap" in notify.messages[1]
    assert not guard.allow(essential=False)
    assert guard.allow(essential=True)


async def test_spend_resets_next_month(state):
    notify = Recorder()
    clock = {"now": datetime(2026, 10, 31, 23, 30, tzinfo=LONDON)}
    guard = CostGuard(state, cap_usd=1, zone=LONDON, notify=notify, clock=lambda: clock["now"])
    await guard.record(model="claude-sonnet-5-5", purpose="chat", usage=usage(output=200_000))
    assert not guard.allow(essential=False)

    # usage rows are stamped with the real time; move "now" well into the next month
    clock["now"] = datetime(2099, 1, 1, 0, 30, tzinfo=LONDON)
    assert guard.spent_this_month() == 0
    assert guard.allow(essential=False)


def test_cost_by_purpose(state):
    for purpose, cost in (("chat", 1.0), ("triage", 0.5), ("chat", 0.25)):
        state.record_usage(
            model="m",
            purpose=purpose,
            input_tokens=0,
            output_tokens=0,
            cache_write_tokens=0,
            cache_read_tokens=0,
            cost_usd=cost,
        )
    start = datetime(2000, 1, 1, tzinfo=UTC)
    assert state.cost_by_purpose_since(start) == {"chat": 1.25, "triage": 0.5}


# --- retries ---


async def test_job_succeeds_after_retry(state):
    calls, delays, notify = [], [], Recorder()

    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("boom")

    async def fake_sleep(seconds):
        delays.append(seconds)

    ok = await run_job("poll", flaky, state=state, notify=notify, sleep=fake_sleep)
    assert ok and len(calls) == 2 and delays == [2.0]
    assert notify.messages == []
    [run] = state.job_runs()
    assert run.last_success is not None and run.consecutive_failures == 0


async def test_job_alerts_after_final_failure(state):
    delays, notify = [], Recorder()

    async def broken():
        raise ValueError("nope")

    async def fake_sleep(seconds):
        delays.append(seconds)

    ok = await run_job("poll", broken, state=state, notify=notify, sleep=fake_sleep)
    assert not ok and delays == [2.0, 4.0]
    assert len(notify.messages) == 1 and "ValueError: nope" in notify.messages[0]
    [run] = state.job_runs()
    assert run.consecutive_failures == 1 and run.last_error == "ValueError: nope"


def test_state_migrations_are_idempotent(tmp_path):
    path = tmp_path / "ultron.db"
    State(path).close()
    s = State(path)  # reopening must not re-run migrations
    s.set("focus", "health")
    assert s.get("focus") == "health"
    s.close()


async def test_repeated_failure_alerts_once_then_reminds_then_says_recovered(state):
    notify = Recorder()
    outcome = {"fail": True}

    async def job():
        if outcome["fail"]:
            raise ValueError("Notion said no")

    async def no_sleep(_):
        pass

    hour = {"n": 0}
    start = datetime(2026, 10, 21, 21, 40, tzinfo=UTC)

    def clock():
        return start + timedelta(hours=hour["n"])

    async def hourly():
        await run_job("sync", job, state=state, notify=notify, sleep=no_sleep, clock=clock)
        hour["n"] += 1

    for _ in range(8):  # an hourly job failing the same way for 8 hours
        await hourly()
    assert len(notify.messages) == 2  # the first failure, then one reminder 6 hours on
    assert "still failing, 7 runs in a row" in notify.messages[1]

    outcome["fail"] = False
    await hourly()
    assert notify.messages[-1] == "✅ Job 'sync' is working again after 8 failed runs."

    await hourly()
    assert len(notify.messages) == 3  # healthy runs stay quiet


async def test_a_daily_job_failing_the_same_way_still_alerts_every_day(state):
    notify = Recorder()
    day = {"n": 0}

    async def job():
        raise ValueError("no brief today")

    async def no_sleep(_):
        pass

    def clock():
        return datetime(2026, 10, 21, 7, 0, tzinfo=UTC) + timedelta(days=day["n"])

    for day["n"] in range(3):
        await run_job("brief", job, state=state, notify=notify, sleep=no_sleep, clock=clock)
    assert len(notify.messages) == 3


async def test_a_different_error_alerts_straight_away(state):
    notify = Recorder()
    errors = iter(["first problem", "second problem"])

    async def job():
        raise RuntimeError(next(errors))

    async def no_sleep(_):
        pass

    for _ in range(2):
        await run_job("mail", job, state=state, notify=notify, sleep=no_sleep, attempts=1)
    assert len(notify.messages) == 2 and "second problem" in notify.messages[1]
