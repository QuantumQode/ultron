"""🧠 Reflect sessions: a bounded conversation with the coach.

- `/reflect` opens a session (or writing in the 🧠 Reflect topic, if the group is set up).
  While one is open, the owner's DM messages go to the coach instead of the task assistant.
- `/done` closes it, and so do 30 minutes of silence.
- Each session starts fresh: the coach sees only this session's messages, plus the owner's
  history in tidy form (journal summaries, day reviews), not one endless thread.
- Full conversations stay in SQLite for 90 days, then they're deleted. Nothing here goes
  to Notion.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ultron.features.items import ItemStore
from ultron.notify import Space
from ultron.ops import activity
from ultron.state import State

IDLE = timedelta(minutes=30)
KEEP = timedelta(days=90)
LOOK_BACK = timedelta(days=14)  # day reviews, journal entries and call-outs the coach reads
PAST_SESSIONS = 5  # summaries of earlier sessions, however old
MAX_LOGS = 25
MAX_DETAIL = 400  # characters of each log entry's details
JOURNAL_PREFIX = "Reflect: "  # title of a session's 📓 journal entry
OFFER_MARKER = "[[reflect]]"  # chat ends a reply with this to offer a Reflect session
FROM_CHAT = "🧠 Reflect is open, carrying on from there. /done when you're finished."
OPENING = "🧠 Reflect is open. What's on your mind?\n/done when you're finished."
ALREADY_OPEN = "🧠 Reflect is already open. Go on."
CLOSED = "🧠 Reflect closed."
IDLE_CLOSED = "🧠 Reflect closed after 30 minutes quiet. /reflect to open it again."
NONE_OPEN = "No Reflect session is open. /reflect to start one."


@dataclass(frozen=True)
class Session:
    id: int
    started_at: datetime
    last_at: datetime
    ended_at: datetime | None
    end_reason: str | None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class Sessions:
    def __init__(self, state: State) -> None:
        self.db = state.db

    def _row(self, r) -> Session:
        parse = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
        return Session(
            id=r["id"],
            started_at=parse(r["started_at"]),
            last_at=parse(r["last_at"]),
            ended_at=parse(r["ended_at"]),
            end_reason=r["end_reason"],
        )

    def get(self, session_id: int) -> Session | None:
        r = self.db.execute("SELECT * FROM reflect_sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row(r) if r else None

    def open_one(self) -> Session | None:
        r = self.db.execute(
            "SELECT * FROM reflect_sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return self._row(r) if r else None

    def start(self, now: datetime) -> Session:
        stamp = _iso(now)
        cur = self.db.execute(
            "INSERT INTO reflect_sessions (started_at, last_at) VALUES (?, ?)", (stamp, stamp)
        )
        session = self.get(cur.lastrowid)
        assert session is not None
        activity.record("reflect", f"🧠 Reflect session {session.id} opened", db=self.db)
        return session

    def touch(self, session_id: int, now: datetime) -> None:
        self.db.execute(
            "UPDATE reflect_sessions SET last_at = ? WHERE id = ?", (_iso(now), session_id)
        )

    def end(self, session_id: int, now: datetime, reason: str) -> Session:
        self.db.execute(
            "UPDATE reflect_sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (_iso(now), reason, session_id),
        )
        session = self.get(session_id)
        assert session is not None
        activity.record("reflect", f"🧠 Reflect session {session_id} closed ({reason})", db=self.db)
        return session

    def unjournaled(self) -> list[Session]:
        rows = self.db.execute(
            "SELECT * FROM reflect_sessions WHERE ended_at IS NOT NULL AND log_item_id IS NULL "
            "ORDER BY id"
        ).fetchall()
        return [self._row(r) for r in rows]

    def set_entry(self, session_id: int, item_id: int) -> None:
        self.db.execute(
            "UPDATE reflect_sessions SET log_item_id = ? WHERE id = ?", (item_id, session_id)
        )

    def delete(self, session_id: int) -> None:
        # Its words leave the activity log too (Reflect sessions are deliberately not kept)
        session = self.get(session_id)
        if session is not None:
            texts = [
                r["text"]
                for r in self.db.execute(
                    "SELECT text FROM chat_messages WHERE session_id = ?", (session_id,)
                )
            ]
            end = session.ended_at or session.last_at
            removed = activity.redact_reflect(self.db, texts, session.started_at - IDLE, end + IDLE)
            activity.record(
                "reflect",
                f"🧠 Reflect session {session_id} removed ({removed} log lines blanked)",
                db=self.db,
            )
        self.db.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        self.db.execute("DELETE FROM reflect_sessions WHERE id = ?", (session_id,))

    def prune(self, now: datetime, keep: timedelta = KEEP) -> int:
        """Delete conversations from sessions that ended more than `keep` ago."""
        cutoff = _iso(now - keep)
        old = [
            r["id"]
            for r in self.db.execute(
                "SELECT id FROM reflect_sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
                (cutoff,),
            )
        ]
        for sid in old:
            self.delete(sid)
        return len(old)


def current(state: State, now: datetime) -> Session | None:
    """The open session, if the owner has written in it within the last 30 minutes."""
    session = Sessions(state).open_one()
    if session is None or now - session.last_at >= IDLE:
        return None
    return session


def start(state: State, now: datetime) -> tuple[Session, bool]:
    """Open a session. (session, True) if new, (session, False) if one was already open.
    A session left idle is closed first, so the new one starts fresh."""
    sessions = Sessions(state)
    open_now = current(state, now)
    if open_now:
        return open_now, False
    stale = sessions.open_one()
    if stale:
        sessions.end(stale.id, now, "idle")
    return sessions.start(now), True


def start_from_chat(state: State, now: datetime) -> tuple[Session, bool]:
    """The [🧠 Make this a Reflect session] button: open a session that begins with the
    last exchange from normal chat, so the coach picks up where that left off."""
    session, new = start(state, now)
    if new:
        rows = state.db.execute(
            "SELECT ts, space, role, text FROM chat_messages "
            "WHERE space = 'chat' AND session_id IS NULL ORDER BY id DESC LIMIT 2"
        ).fetchall()
        if len(rows) == 2 and rows[1]["role"] == "user":
            state.db.executemany(
                "INSERT INTO chat_messages (ts, space, role, text, session_id) "
                "VALUES (?, 'reflect', ?, ?, ?)",
                [(r["ts"], r["role"], r["text"], session.id) for r in reversed(rows)],
            )
    return session, new


def take_offer(text: str) -> tuple[str, bool]:
    """Strip the offer marker from a chat reply. (text, whether to offer Reflect)."""
    if OFFER_MARKER not in text:
        return text, False
    return text.replace(OFFER_MARKER, "").strip(), True


def finish(state: State, now: datetime, reason: str = "done") -> Session | None:
    """Close the open session (if any). `journal.write_pending` then writes its entry."""
    sessions = Sessions(state)
    session = sessions.open_one()
    if session is None:
        return None
    return sessions.end(session.id, now, reason)


def route(state: State, space: Space, now: datetime) -> tuple[Space, Session | None]:
    """Which space a message belongs in, and the session it joins.

    - In the 🧠 Reflect topic: always Reflect, opening a session if none is open.
    - Elsewhere in chat: Reflect while a session is open, else as it was.
    """
    if space is Space.REFLECT:
        session, _ = start(state, now)
    elif space is Space.CHAT:
        session = current(state, now)
    else:
        return space, None
    if session is None:
        return space, None
    Sessions(state).touch(session.id, now)
    return Space.REFLECT, session


def close_idle(state: State, now: datetime) -> Session | None:
    """For the 10-minute check: close a session left quiet for 30 minutes, and delete
    conversations past 90 days. Returns the session it closed."""
    sessions = Sessions(state)
    sessions.prune(now)
    session = sessions.open_one()
    if session is None or now - session.last_at < IDLE:
        return None
    return sessions.end(session.id, now, "idle")


# --- what the coach knows ---


def _clip(text: str, limit: int = MAX_DETAIL) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def coach_context(state: State, today: date, zone: ZoneInfo) -> list[str]:
    """History for the coach, in tidy form: the last two weeks of 📓 Log (day and week
    reviews, journal entries from the Inbox), pattern call-outs, and summaries of past
    Reflect sessions. Lines for the <context> block."""
    store = ItemStore(state)
    since = (today - LOOK_BACK).isoformat()
    logs = sorted(
        (i for i in store.all() if i.kind == "log"), key=lambda i: i.created_at, reverse=True
    )
    past = [i for i in logs if i.title.startswith(JOURNAL_PREFIX)][:PAST_SESSIONS]
    seen = {i.id for i in past}
    logs = [i for i in logs if i.created_at[:10] >= since and i.id not in seen]

    def entry(item) -> str:
        day = datetime.fromisoformat(item.created_at).astimezone(zone)
        text = f"- {day:%a %d %b}: {item.title}"
        return text + (f" | {_clip(item.details)}" if item.details else "")

    lines = ["📓 Log, last 14 days (newest first):"]
    lines += [entry(i) for i in logs[:MAX_LOGS]] or ["(nothing yet)"]
    lines += ["", "Pattern call-outs, last 14 days:"]
    called = state.db.execute(
        "SELECT key, value FROM kv WHERE key LIKE 'accountability.called.%' AND value >= ? "
        "ORDER BY value DESC",
        (since,),
    ).fetchall()
    callouts = []
    for r in called:
        item = store.get(int(r["key"].rsplit(".", 1)[1]))
        if item:
            what = (
                f"pushed back {item.times_moved} times"
                if item.kind == "task"
                else "goal with nothing done in over a week"
            )
            callouts.append(f"- {r['value']}: {item.title} ({what}; now {item.status})")
    lines += callouts or ["(none)"]
    lines += ["", "Earlier Reflect sessions (newest first):"]
    lines += [entry(i) for i in past] or ["(none yet)"]
    return lines
