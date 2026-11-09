"""`python -m ultron.check`: test every configured service. Read-only, writes nothing.

Prints ✅ / ❌ / ⚪ (skipped, not configured yet) per service and exits 1 if any fail.
"""

import asyncio
import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from ultron.bot.probe import check_bot
from ultron.brain import llm
from ultron.config import Settings, get_settings
from ultron.errors import UserFacingError
from ultron.integrations import calendar, mail, notion

TIMEOUT_SECONDS = 30


class Outcome(StrEnum):
    OK = "✅"
    FAIL = "❌"
    SKIP = "⚪"


@dataclass(frozen=True)
class Check:
    name: str
    probe: Callable[[Settings], Any]  # sync or async; returns a detail string, raises on failure
    enabled: bool
    skip_reason: str = ""


@dataclass(frozen=True)
class Result:
    name: str
    outcome: Outcome
    detail: str


def checks_for(settings: Settings) -> list[Check]:
    return [
        Check("Telegram", check_bot, True),
        Check("Claude", llm.check_access, True),
        Check(
            "Notion",
            notion.check_access,
            settings.notion_enabled,
            "set NOTION_TOKEN and NOTION_ROOT_PAGE_ID",
        ),
        Check(
            "Email",
            mail.check_login,
            settings.mail_configured,  # an unsupported provider shows ❌ with what to do
            "set EMAIL_ADDRESS and EMAIL_APP_PASSWORD (and EMAIL_PROVIDER if it isn't guessed)",
        ),
        Check(
            "iCloud Calendar",
            calendar.check_login,
            settings.calendar_enabled,
            "set ICLOUD_USER and ICLOUD_APP_PASSWORD",
        ),
    ]


async def run_check(check: Check, settings: Settings, timeout: float = TIMEOUT_SECONDS) -> Result:
    if not check.enabled:
        return Result(check.name, Outcome.SKIP, f"not configured: {check.skip_reason}")
    try:
        if inspect.iscoroutinefunction(check.probe):
            call = check.probe(settings)
        else:
            call = asyncio.to_thread(check.probe, settings)  # sync libraries run off the event loop
        detail = await asyncio.wait_for(call, timeout)
    except TimeoutError:
        return Result(check.name, Outcome.FAIL, f"no answer within {timeout:.0f}s (network?)")
    except Exception as exc:  # noqa: BLE001 - report every failure, never crash the check
        detail = str(exc) if isinstance(exc, UserFacingError) else f"{type(exc).__name__}: {exc}"
        return Result(check.name, Outcome.FAIL, detail)
    return Result(check.name, Outcome.OK, detail)


async def run_all(settings: Settings) -> list[Result]:
    return list(await asyncio.gather(*(run_check(c, settings) for c in checks_for(settings))))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # emoji on Windows consoles
    try:
        settings = get_settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        sys.exit(f"Can't run checks. Fix these settings in .env: {missing}")

    results = asyncio.run(run_all(settings))
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.outcome} {r.name:<{width}}  {r.detail}")
    sys.exit(1 if any(r.outcome is Outcome.FAIL for r in results) else 0)


if __name__ == "__main__":
    main()
