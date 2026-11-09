"""How the owner hears about email: pings for important mail, the /inbox digest and
the chat search. Plain data and plain text; front ends decide how to show them."""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ultron.features.triage import EmailStore, StoredEmail
from ultron.notify import Notifier, Space

PING_WINDOW = timedelta(hours=24)  # older important mail (e.g. the first backfill) isn't pinged
MAX_PING_LINES = 10
ICONS = {"money": "💷", "deadline": "📅", "person": "👤"}


def in_quiet_hours(now: datetime, start: time, end: time) -> bool:
    """True between start and end, including windows that cross midnight (22:30–07:00)."""
    t = now.timetz().replace(tzinfo=None)
    return start <= t < end if start <= end else t >= start or t < end


def who(e: StoredEmail) -> str:
    return e.sender_name or e.sender


def ping_line(e: StoredEmail) -> str:
    text = f"{ICONS.get(e.reason or '', '📧')} {who(e)}: {e.summary or e.subject}"
    return text + (" (waiting for your reply)" if e.needs_reply else "")


def ping_text(emails: list[StoredEmail]) -> str:
    n = len(emails)
    lines = [ping_line(e) for e in emails[:MAX_PING_LINES]]
    if n > MAX_PING_LINES:
        lines.append(f"…and {n - MAX_PING_LINES} more. Send /inbox to see them all.")
    head = "📧 1 important email" if n == 1 else f"📧 {n} important emails"
    return f"{head}\n\n" + "\n".join(lines)


async def ping_important(
    store: EmailStore,
    notifier: Notifier,
    *,
    now: datetime,
    quiet_start: time,
    quiet_end: time,
) -> int:
    """Send one message for new important mail. Returns how many emails it covered.

    During quiet hours nothing is sent; the pings wait for the first sync after.
    Important mail older than a day (e.g. from the first 30-day read) is marked as
    handled without a ping: it's on the Board and in /inbox.
    """
    pending = store.unnotified_important()
    if not pending or in_quiet_hours(now, quiet_start, quiet_end):
        return 0
    fresh = [e for e in pending if now - e.sent_at <= PING_WINDOW]
    if fresh:
        await notifier.send(ping_text(fresh), Space.BRIEFS)
    store.mark_notified(e.id for e in pending)
    return len(fresh)


# --- /inbox ---


@dataclass
class InboxDigest:
    hours: int
    important: list[StoredEmail] = field(default_factory=list)
    fyi: list[StoredEmail] = field(default_factory=list)
    promos: int = 0
    ignored: int = 0

    @property
    def empty(self) -> bool:
        return not (self.important or self.fyi or self.promos or self.ignored)


def inbox_digest(store: EmailStore, now: datetime, hours: int = 24) -> InboxDigest:
    digest = InboxDigest(hours)
    for e in store.since(now - timedelta(hours=hours)):
        if e.category == "important":
            digest.important.append(e)
        elif e.category == "fyi":
            digest.fyi.append(e)
        elif e.category == "promo":
            digest.promos += 1
        else:
            digest.ignored += 1
    return digest


# --- chat search ---

MAX_RESULTS = 30


def search_emails(
    store: EmailStore,
    *,
    now: datetime,
    zone: ZoneInfo,
    query: str | None = None,
    days_back: int | None = None,
    category: str | None = None,
    needs_reply: bool | None = None,
) -> str:
    """Email summaries as lines for Claude, newest first."""
    days = max(1, min(days_back or 30, 90))
    words = (query or "").casefold().split()
    found = []
    for e in reversed(store.since(now - timedelta(days=days))):
        if category and e.category != category:
            continue
        if needs_reply is not None and e.needs_reply != needs_reply:
            continue
        haystack = f"{e.sender_name} {e.sender} {e.subject} {e.summary}".casefold()
        if words and not all(w in haystack for w in words):
            continue
        found.append(e)
    if not found:
        return f"No matching emails in the last {days} days."
    lines = [_result_line(e, zone) for e in found[:MAX_RESULTS]]
    if len(found) > MAX_RESULTS:
        lines.append(f"…and {len(found) - MAX_RESULTS} more; narrow the search.")
    return "\n".join(lines)


def _result_line(e: StoredEmail, zone: ZoneInfo) -> str:
    parts = [
        f"{e.sent_at.astimezone(zone):%a %d %b %H:%M}",
        f"{who(e)} <{e.sender}>",
        e.category + (f"/{e.reason}" if e.reason else ""),
        f"subject: {e.subject}",
    ]
    if e.summary:
        parts.append(e.summary)
    if e.needs_reply:
        parts.append("waiting for the owner's reply")
    for item in e.items:
        bits = [item["title"], item.get("amount") or "", item.get("due") or ""]
        parts.append("item: " + " ".join(b for b in bits if b))
    return " · ".join(parts)


def awaiting_reply(store: EmailStore, now: datetime, days: int = 14) -> list[StoredEmail]:
    """Emails from real people still waiting on the owner (for accountability)."""
    return [e for e in store.since(now - timedelta(days=days)) if e.needs_reply]


def context_lines(store: EmailStore, now: datetime, zone: Any) -> list[str]:
    return [
        f"{e.sent_at.astimezone(zone):%a %d %b}: {who(e)}: {e.summary or e.subject}"
        for e in awaiting_reply(store, now)[-10:]
    ]
