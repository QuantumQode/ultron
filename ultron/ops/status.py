"""Health and spend reports as plain data, for `/status` and `/usage` in any front end."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from ultron.config import Settings
from ultron.ops.guard import CostGuard
from ultron.state import JobRun, State


@dataclass(frozen=True)
class Integration:
    name: str
    configured: bool
    phase: int  # build phase that switches it on


@dataclass(frozen=True)
class StatusReport:
    integrations: list[Integration]
    jobs: list[JobRun]
    spent_usd: float
    cap_usd: float


@dataclass(frozen=True)
class UsageReport:
    month: datetime
    spent_usd: float
    cap_usd: float
    by_purpose: dict[str, float]

    @property
    def fraction(self) -> float:
        return self.spent_usd / self.cap_usd


def build_status(settings: Settings, state: State, guard: CostGuard) -> StatusReport:
    return StatusReport(
        integrations=[
            Integration("Telegram", True, 1),
            Integration("Claude", True, 1),
            Integration("Notion", settings.notion_enabled, 2),
            Integration(
                f"Email ({settings.mail_provider.name})" if settings.mail_configured else "Email",
                settings.mail_enabled,
                3,
            ),
            Integration("iCloud Calendar", settings.calendar_enabled, 4),
        ],
        jobs=state.job_runs(),
        spent_usd=guard.spent_this_month(),
        cap_usd=guard.cap,
    )


RECENT_FAILURE = timedelta(hours=24)


def health_lines(state: State, now: datetime) -> list[str]:
    """Ultron's own job health in plain lines, so chat can answer "why did the sync fail?"
    from the real error instead of guessing. Failures over a day old that have recovered
    are left out."""
    lines = []
    for job in state.job_runs():
        if job.last_failure is None:
            continue
        when = job.last_failure.astimezone(now.tzinfo)
        if job.consecutive_failures:
            lines.append(
                f"- {job.job}: FAILING ({job.consecutive_failures} run(s) in a row), last at "
                f"{when:%a %d %b %H:%M}: {job.last_error}"
            )
        elif now - job.last_failure < RECENT_FAILURE:
            ok = job.last_success.astimezone(now.tzinfo) if job.last_success else None
            since = f", working again since {ok:%a %d %b %H:%M}" if ok else ""
            lines.append(
                f"- {job.job}: recovered. It failed at {when:%a %d %b %H:%M} "
                f"({job.last_error}){since}"
            )
    return lines or ["- All scheduled jobs (email check, Notion sync, rituals) are running fine."]


def build_usage(state: State, guard: CostGuard) -> UsageReport:
    month = guard.month()
    return UsageReport(
        month=month,
        spent_usd=state.cost_since(month),
        cap_usd=guard.cap,
        by_purpose=state.cost_by_purpose_since(month),
    )
