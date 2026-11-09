"""Safeguards: Claude cost tracking with a monthly cap, and job retries with alerts.

- Every Claude call goes through `CostGuard.record()` so spend is always known.
- At 80% of the monthly cap you get one warning; at 100% non-essential work
  stops (`CostGuard.allow(essential=False)` returns False) until next month.
- Scheduled jobs run through `run_job()`, which retries with backoff, records
  health for `/status`, and messages you if the job still fails.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ultron.ops import activity
from ultron.state import State

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]  # e.g. a bound Notifier.send


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    long_input: float | None = None  # rate above `long_threshold` prompt tokens
    long_output: float | None = None
    long_threshold: int = 100_000


CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute cache writes cost 1.25x input

# Anthropic first-party prices, checked 2026-10-19. Update when prices change.
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(
        input=0.10, output=0.50, cache_read=0.01, long_input=0.50, long_output=2.50
    ),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.20),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
}
# Unknown models are costed at the most expensive known price, so the cap errs safe.
FALLBACK_PRICE = max(PRICES.values(), key=lambda p: p.output)


@dataclass(frozen=True)
class Tokens:
    input: int
    output: int
    cache_write: int
    cache_read: int

    @classmethod
    def from_usage(cls, usage: Any) -> "Tokens":
        """Read an Anthropic SDK `response.usage` object (cache fields may be None)."""
        return cls(
            input=getattr(usage, "input_tokens", 0) or 0,
            output=getattr(usage, "output_tokens", 0) or 0,
            cache_write=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )


def cost_usd(model: str, tokens: Tokens) -> float:
    price = PRICES.get(model)
    if price is None:
        log.warning("No price for model %s; costing at the highest known rate", model)
        price = FALLBACK_PRICE
    in_rate, out_rate = price.input, price.output
    prompt = tokens.input + tokens.cache_write + tokens.cache_read
    if price.long_input is not None and prompt > price.long_threshold:
        in_rate, out_rate = price.long_input, price.long_output or out_rate
    return (
        tokens.input * in_rate
        + tokens.cache_write * in_rate * CACHE_WRITE_MULTIPLIER
        + tokens.cache_read * price.cache_read
        + tokens.output * out_rate
    ) / 1_000_000


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class CostGuard:
    WARN_AT = 0.8

    def __init__(
        self,
        state: State,
        *,
        cap_usd: float,
        zone: ZoneInfo,
        notify: Notify,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state = state
        self.cap = cap_usd
        self.zone = zone
        self.notify = notify
        self._clock = clock or (lambda: datetime.now(self.zone))

    def month(self) -> datetime:
        return month_start(self._clock().astimezone(self.zone))

    def spent_this_month(self) -> float:
        return self.state.cost_since(self.month())

    def allow(self, *, essential: bool) -> bool:
        """Essential work (briefs, planning) always runs; the rest stops at the cap."""
        return essential or self.spent_this_month() < self.cap

    async def record(self, *, model: str, purpose: str, usage: Any) -> float:
        """Log one Claude call and send threshold alerts. Returns the call's cost."""
        tokens = Tokens.from_usage(usage)
        cost = cost_usd(model, tokens)
        self.state.record_usage(
            model=model,
            purpose=purpose,
            input_tokens=tokens.input,
            output_tokens=tokens.output,
            cache_write_tokens=tokens.cache_write,
            cache_read_tokens=tokens.cache_read,
            cost_usd=cost,
        )
        tokens_in = tokens.input + tokens.cache_write + tokens.cache_read
        activity.record(
            "claude",
            f"{purpose} on {model}: ${cost:.4f} ({tokens_in} in, {tokens.output} out)",
            db=self.state.db,
        )
        await self._check_thresholds()
        return cost

    async def _check_thresholds(self) -> None:
        spent = self.spent_this_month()
        month = self.month().strftime("%Y-%m")
        for level, message in (
            (
                1.0,
                f"🛑 API spend hit the ${self.cap:.2f} monthly cap (${spent:.2f}). "
                "Only briefs and planning will run until next month.",
            ),
            (
                self.WARN_AT,
                f"⚠️ API spend is ${spent:.2f}, {spent / self.cap:.0%} of the "
                f"${self.cap:.2f} monthly cap.",
            ),
        ):
            if spent >= self.cap * level:
                key = f"cost_alert:{month}:{level}"
                if self.state.get(key) is None:
                    self.state.set(key, "sent")
                    await self.notify(message)
                return  # only the highest threshold reached is relevant


async def run_job(
    name: str,
    job: Callable[[], Awaitable[None]],
    *,
    state: State,
    notify: Notify,
    attempts: int = 3,
    base_delay: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bool:
    """Run a job with retries (delays: base, 2×base, ...). Returns True on success.

    After the final failure the error is recorded and you get a Telegram alert; the
    exception is not re-raised so the scheduler keeps going. A job that keeps failing the
    same way alerts again at most every REMIND_AFTER, and says when it works again.
    """
    with activity.caused_by(f"job: {name}"):
        return await _run_job(name, job, state, notify, attempts, base_delay, sleep, clock)


async def _run_job(name, job, state, notify, attempts, base_delay, sleep, clock) -> bool:
    before = next((r for r in state.job_runs() if r.job == name), None)
    failing = before.consecutive_failures if before else 0
    alert_key = f"job_alert:{name}"
    for attempt in range(1, attempts + 1):
        try:
            await job()
        except Exception as exc:  # noqa: BLE001 - jobs must never crash the scheduler
            error = f"{type(exc).__name__}: {exc}"
            log.warning("Job %s failed (attempt %d/%d): %s", name, attempt, attempts, error)
            if attempt < attempts:
                await sleep(base_delay * 2 ** (attempt - 1))
                continue
            state.record_job_failure(name, error)
            activity.record("job", f"Job '{name}' failed: {error}", db=state.db)
            repeat = failing > 0 and before is not None and before.last_error == error[:500]
            last_alert = state.get(alert_key)
            due = last_alert is None or clock() - datetime.fromisoformat(last_alert) >= REMIND_AFTER
            if not repeat or due:
                still = f" (still failing, {failing + 1} runs in a row)" if repeat else ""
                state.set(alert_key, clock().isoformat())
                await _tell(
                    notify,
                    f"❌ Job '{name}' failed after {attempts} tries{still}: {error}\n"
                    "Ask me why, or check /status.",
                )
            return False
        else:
            state.record_job_success(name)
            activity.record("job", f"Job '{name}' finished", db=state.db)
            if failing:
                runs = "run" if failing == 1 else "runs"
                await _tell(
                    notify, f"✅ Job '{name}' is working again after {failing} failed {runs}."
                )
            return True
    return False


REMIND_AFTER = timedelta(hours=6)  # a job failing the same way: at most one alert per 6 hours


async def _tell(notify: Notify, text: str) -> None:
    try:
        await notify(text)
    except Exception:  # noqa: BLE001 - an alert that can't be sent must not fail the job
        log.exception("Could not send job alert: %s", text)
