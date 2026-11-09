"""The activity log: a running record of everything that happens, kept forever.

Every message in and out, button tap, item change, Notion write, card, email read, job and
Claude call gets one row in `activity`. The owner reads it with `python -m ultron.log`;
Ultron reads it in chat with the `read_log` tool, as its memory of what actually happened.

- `record()` never raises: a log that can't be written must not break the thing it logs.
- `caused_by()` tags what's running ("you: /sync", "job: sync", "you, on the Board"); nested
  causes join with " › ", and rows written meanwhile carry it.
- Rows are append-only (SQLite triggers refuse deletes and edits). The one exception: when a
  🧠 Reflect session is deleted (they're kept 90 days by design), its message text here is
  replaced with REMOVED, so the log doesn't keep what the owner's Reflect rules drop.
- Text is scrubbed of the configured secrets and anything shaped like an API token, and
  capped in size. Emails are logged as their stored summary, never their body.
"""

import contextvars
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

SUMMARY_LIMIT = 300
TEXT_LIMIT = 20_000
DETAIL_LIMIT = 20_000
REMOVED = "🧠 Reflect message (removed with its session)"  # also in the migration's trigger
DEFAULT_CAUSE = "ultron"

KINDS = (
    "message_in",  # the owner wrote to Ultron
    "button",  # the owner tapped a button
    "message_out",  # Ultron sent or edited a message
    "item",  # a to-do, habit, goal, schedule entry... added or changed
    "habit",  # a habit kept or unkept for a day
    "plan",  # a block on the 📅 Schedule plan added, ticked, dropped or renamed
    "notion",  # a write to Notion
    "inbox",  # a 📥 Inbox note or Board line read
    "email",  # an email triaged (summary only)
    "card",  # a suggestion card proposed, applied, dismissed, undone
    "focus",  # focus mode switched
    "profile",  # planning settings changed
    "reflect",  # a 🧠 Reflect session opened, closed or removed
    "tool",  # a tool Ultron's chat used
    "claude",  # a Claude API call (model, purpose, cost)
    "job",  # a scheduled job finished or failed
    "system",  # startup and the like
)

_cause: contextvars.ContextVar[str] = contextvars.ContextVar("activity_cause", default="")
_sink: sqlite3.Connection | None = None
_secrets: list[str] = []
TOKEN_SHAPES = re.compile(
    r"\b\d{6,}:[A-Za-z0-9_-]{30,}"  # Telegram bot token
    r"|\bsk-ant-[A-Za-z0-9_-]{10,}"  # Anthropic key
    r"|\b(?:secret|ntn)_[A-Za-z0-9]{20,}"  # Notion token
)


def attach(db: sqlite3.Connection, secrets: Iterable[str | None] = ()) -> None:
    """Send records without their own connection here, and scrub these values from them."""
    global _sink, _secrets
    _sink = db
    _secrets = sorted((s for s in secrets if s and len(s) >= 6), key=len, reverse=True)


def detach() -> None:
    global _sink, _secrets
    _sink, _secrets = None, []


def sink() -> sqlite3.Connection | None:
    """Where records without their own connection go (None: not logging)."""
    return _sink


def cause() -> str:
    return _cause.get() or DEFAULT_CAUSE


@contextmanager
def caused_by(what: str) -> Iterator[None]:
    parent = _cause.get()
    token = _cause.set(f"{parent} › {what}" if parent else what)
    try:
        yield
    finally:
        _cause.reset(token)


def scrub(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, "[secret]")
    return TOKEN_SHAPES.sub("[secret]", text)


def one_line(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def record(
    kind: str,
    summary: str,
    *,
    text: str | None = None,
    detail: Any = None,
    db: sqlite3.Connection | None = None,
) -> None:
    """Add one row. `summary` is one line; `text` the full words (a message, a note);
    `detail` anything JSON-able (ids, before → after)."""
    target = db if db is not None else _sink
    if target is None:
        return
    try:
        if detail is not None:
            blob = scrub(json.dumps(detail, default=str, ensure_ascii=False))
            if len(blob) > DETAIL_LIMIT:
                blob = json.dumps({"truncated": blob[:DETAIL_LIMIT]}, ensure_ascii=False)
        else:
            blob = None
        target.execute(
            "INSERT INTO activity (ts, kind, cause, summary, text, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                kind,
                cause(),
                scrub(one_line(summary, SUMMARY_LIMIT)),
                scrub(text)[:TEXT_LIMIT] if text else None,
                blob,
            ),
        )
    except Exception:  # noqa: BLE001 - logging must never break what it logs
        log.exception("Couldn't write to the activity log: %s", summary[:100])


def redact_reflect(
    db: sqlite3.Connection, texts: Iterable[str], start: datetime, end: datetime
) -> int:
    """A Reflect session is being deleted: drop its words from the log too. Matches the
    messages by their text (a long reply is sent in pieces) and the session's tool calls,
    between `start` and `end`."""
    window = (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat())
    gone = "UPDATE activity SET summary = ?, text = NULL, detail = NULL WHERE ts BETWEEN ? AND ? "
    count = db.execute(
        gone + "AND kind = 'tool' AND cause LIKE '%reflect%'", (REMOVED, *window)
    ).rowcount
    for body in {t for t in texts if t and t.strip()}:
        count += db.execute(
            gone + "AND kind IN ('message_in', 'message_out') AND summary != ? "
            "AND text IS NOT NULL AND (text = ? OR instr(?, text) > 0 OR instr(text, ?) > 0)",
            (REMOVED, *window, REMOVED, body, body, body),
        ).rowcount
    return count


# --- reading it ---


@dataclass(frozen=True)
class Entry:
    id: int
    ts: datetime
    kind: str
    cause: str
    summary: str
    text: str | None
    detail: str | None


def entries(
    db: sqlite3.Connection,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    kinds: Iterable[str] | None = None,
    skip_kinds: Iterable[str] = (),
    query: str | None = None,
    limit: int = 200,
) -> list[Entry]:
    """The newest `limit` matching rows, oldest first."""
    where, args = [], []
    if start is not None:
        where.append("ts >= ?")
        args.append(start.astimezone(UTC).isoformat())
    if end is not None:
        where.append("ts < ?")
        args.append(end.astimezone(UTC).isoformat())
    for column, values, op in (("kind", kinds, "IN"), ("kind", skip_kinds, "NOT IN")):
        values = list(values or [])
        if values:
            where.append(f"{column} {op} ({','.join('?' * len(values))})")
            args += values
    if query and query.strip():
        like = "%" + re.sub(r"([%_\\])", r"\\\1", query.strip()) + "%"
        where.append(
            "(summary LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\' OR cause LIKE ? ESCAPE '\\')"
        )
        args += [like, like, like]
    sql = "SELECT * FROM activity" + (" WHERE " + " AND ".join(where) if where else "")
    rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [
        Entry(
            id=r["id"],
            ts=datetime.fromisoformat(r["ts"]),
            kind=r["kind"],
            cause=r["cause"],
            summary=r["summary"],
            text=r["text"],
            detail=r["detail"],
        )
        for r in reversed(rows)
    ]


def line(entry: Entry, zone: ZoneInfo, *, full: bool = False) -> str:
    when = entry.ts.astimezone(zone).strftime("%a %d %b %H:%M:%S")
    head = f"{when} · {entry.kind} · {entry.cause} · {entry.summary}"
    if not full:
        return head
    extra = [f"    {ln}" for ln in (entry.text or "").splitlines()]
    if entry.detail:
        extra.append(f"    detail: {entry.detail}")
    return "\n".join([head, *extra])


CHAT_LINES = 60  # most lines read_log hands Claude at once (about 3k tokens)
CHAT_SKIP = ("claude",)  # API calls are bookkeeping, not memory, unless asked for


def for_chat(
    db: sqlite3.Connection,
    zone: ZoneInfo,
    now: datetime,
    *,
    day: str = "",
    days: int = 1,
    kind: str = "any",
    query: str = "",
) -> str:
    """What `read_log` shows Ultron: one line per event, newest last, wrapped as data. A
    "<" in the log can't open or close tags in the prompt."""
    if day:
        start = datetime.combine(date.fromisoformat(day), time.min, zone)  # ValueError if bad
        end = start + timedelta(days=1)
    else:
        days = max(1, min(int(days), 31))
        start = datetime.combine(now.astimezone(zone).date(), time.min, zone) - timedelta(
            days=days - 1
        )
        end = None
    if kind not in ("any", *KINDS):
        raise ValueError(f"kind must be any or one of: {', '.join(KINDS)}.")
    found = entries(
        db,
        start=start,
        end=end,
        kinds=None if kind == "any" else [kind],
        skip_kinds=CHAT_SKIP if kind == "any" else (),
        query=query or None,
        limit=CHAT_LINES + 1,
    )
    more = len(found) > CHAT_LINES
    lines = [line(e, zone).replace("<", "‹") for e in found[-CHAT_LINES:]]
    head = f"{len(lines)} event(s) from {start:%a %d %b}" + (
        " (older ones cut: narrow it with kind, query or day)" if more else ""
    )
    body = "\n".join(lines) or "(nothing logged)"
    return f"<activity_log>\n{head}\n{body}\n</activity_log>"
