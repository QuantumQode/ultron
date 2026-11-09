"""The hourly mail step: read new mail → triage → Board items → pings.

Runs before the notes sync, so new to-dos reach the Board in the same hour.
- Up to 100 new emails per run (the rest wait for the next hour).
- The cursor only moves on after results are saved, so a failure never skips mail;
  emails already stored are recognised by Message-ID and not triaged twice.
- The Sent folder is read for headers only (never sent to Claude), to clear
  "waiting for your reply" once the owner has answered.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ultron.config import Settings
from ultron.features.email_board import add_email_items
from ultron.features.email_report import ping_important
from ultron.features.items import ItemStore
from ultron.features.triage import EmailStore, triage_emails
from ultron.integrations import mail
from ultron.notify import Notifier
from ultron.ops.guard import CostGuard
from ultron.state import State

log = logging.getLogger(__name__)

KEY_INBOX_CURSOR = "mail.inbox_cursor"
KEY_SENT_CURSOR = "mail.sent_cursor"

_lock = asyncio.Lock()


@dataclass
class MailReport:
    read: int = 0  # new emails triaged
    blocked: int = 0
    important: int = 0
    added: int = 0  # new Board to-dos
    merged: int = 0
    asked: int = 0
    replied: int = 0  # emails no longer waiting for a reply
    pinged: int = 0
    more: bool = False
    cost_usd: float = 0.0

    def summary(self) -> str:
        if not (self.read or self.blocked or self.replied):
            return "No new email."
        parts = [f"read {self.read} new email(s)"]
        if self.important:
            parts.append(f"{self.important} important")
        if self.added or self.merged or self.asked:
            parts.append(f"{self.added + self.merged + self.asked} Board update(s)")
        if self.blocked:
            parts.append(f"skipped {self.blocked} from blocked senders")
        text = "Email: " + ", ".join(parts) + "."
        if self.more:
            text += " More waiting; I'll read the next 100 within the hour."
        return text


def _load(state: State, key: str) -> mail.Cursor | None:
    raw = state.get(key)
    return mail.Cursor(**json.loads(raw)) if raw else None


def _save(state: State, key: str, cursor: mail.Cursor) -> None:
    state.set(key, json.dumps({"uidvalidity": cursor.uidvalidity, "last_uid": cursor.last_uid}))


async def run_mail(
    settings: Settings,
    state: State,
    guard: CostGuard,
    notifier: Notifier,
    make_llm: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    now: datetime | None = None,
) -> MailReport:
    async with _lock:
        return await _run(settings, state, guard, notifier, make_llm, now)


async def _run(settings, state, guard, notifier, make_llm, now) -> MailReport:
    now = now or datetime.now(settings.zone)
    today = now.date()
    report = MailReport()
    emails = EmailStore(state)

    batch = await asyncio.to_thread(
        mail.fetch_new, settings, _load(state, KEY_INBOX_CURSOR), today=today
    )
    report.blocked, report.more = batch.blocked, batch.more
    known = emails.known(e.message_id for e in batch.emails)
    fresh = [e for e in batch.emails if not e.message_id or e.message_id not in known]
    if fresh:
        async with make_llm() as llm:
            outcome = await triage_emails(
                llm, guard, fresh, model=settings.model_fast, today=today, zone=settings.zone
            )
        report.cost_usd = outcome.cost_usd
        saved = emails.save(outcome.triaged)
        board = add_email_items(ItemStore(state), saved, now=now)
        report.read = len(saved)
        report.important = sum(1 for _, t in saved if t.category == "important")
        report.added, report.merged = len(board.added), len(board.merged)
        report.asked = len(board.asked)
    _save(state, KEY_INBOX_CURSOR, batch.cursor)  # only after the results are stored

    sent = await asyncio.to_thread(
        mail.fetch_sent, settings, _load(state, KEY_SENT_CURSOR), today=today
    )
    if sent.folder is None:
        log.warning("No Sent folder found; 'waiting for your reply' won't clear by itself")
    else:
        report.replied = emails.mark_replied(sent.sent)
        _save(state, KEY_SENT_CURSOR, sent.cursor)

    emails.prune(now)
    report.pinged = await ping_important(
        emails,
        notifier,
        now=now,
        quiet_start=settings.quiet_hours_start,
        quiet_end=settings.quiet_hours_end,
    )
    log.info("Mail: %s", report.summary())
    return report
