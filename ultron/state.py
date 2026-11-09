"""Internal state in SQLite: job health, API usage and small key-value settings.

User-visible data (notes, tasks, goals...) lives in Notion, not here.
Schema changes are numbered migrations applied in order and tracked with
SQLite's `user_version`, so existing databases upgrade in place.

Calls are synchronous: queries are tiny and local, so running them directly
from async handlers is fine.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: Phase 1 tables
    """
    CREATE TABLE job_runs (
        job TEXT PRIMARY KEY,
        last_success TEXT,
        last_failure TEXT,
        last_error TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE api_usage (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        model TEXT NOT NULL,
        purpose TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cache_write_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL
    );
    CREATE INDEX api_usage_ts ON api_usage (ts);
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2: Inbox blocks already sorted, so nothing is handled twice if moving them fails
    """
    CREATE TABLE inbox_handled (
        block_id TEXT PRIMARY KEY,
        handled_at TEXT NOT NULL
    );
    """,
    # 3: the structure behind the Board
    """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        details TEXT,
        project TEXT,
        area TEXT NOT NULL,
        due TEXT,
        status TEXT NOT NULL DEFAULT 'open',      -- open | done | dropped
        question TEXT,
        source_blocks TEXT NOT NULL DEFAULT '[]', -- JSON list of Inbox block ids
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        done_at TEXT,
        times_moved INTEGER NOT NULL DEFAULT 0,   -- due date pushed back (accountability)
        board_block_id TEXT,                      -- the item's block on the Board
        board_place TEXT,                         -- section/group it's rendered in
        board_text TEXT                           -- text as last rendered, to spot owner edits
    );
    CREATE TABLE projects (
        name TEXT PRIMARY KEY,
        area TEXT NOT NULL,
        note TEXT
    );
    CREATE TABLE board_blocks (
        key TEXT PRIMARY KEY,                     -- e.g. "section:todo", "group:todo:Acme"
        block_id TEXT NOT NULL
    );
    """,
    # 4: lines sorting didn't account for; after two misses they're parked, not re-sent
    """
    CREATE TABLE sort_misses (
        block_id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        misses INTEGER NOT NULL,
        told_owner INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 5: exact days and times for schedule entries (work, workouts, commitments)
    """
    ALTER TABLE items ADD COLUMN days TEXT;        -- e.g. "mon,tue,wed"
    ALTER TABLE items ADD COLUMN start_time TEXT;  -- "HH:MM"
    ALTER TABLE items ADD COLUMN end_time TEXT;    -- "HH:MM"
    """,
    # 6: chat history (recent messages; older ones are folded into a running summary)
    """
    CREATE TABLE chat_messages (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        space TEXT NOT NULL,                      -- chat | reflect
        role TEXT NOT NULL,                       -- user | assistant
        text TEXT NOT NULL
    );
    CREATE INDEX chat_messages_space ON chat_messages (space, id);
    """,
    # 7: email triage results: summaries and extracted details only, never email text
    """
    CREATE TABLE emails (
        id INTEGER PRIMARY KEY,
        message_id TEXT NOT NULL,                 -- '' if the email had none
        sender TEXT NOT NULL,
        sender_name TEXT NOT NULL,
        subject TEXT NOT NULL,
        sent_at TEXT NOT NULL,                    -- UTC ISO
        category TEXT NOT NULL,                   -- important | fyi | promo | ignore
        reason TEXT,                              -- money | deadline | person
        summary TEXT NOT NULL,
        needs_reply INTEGER NOT NULL,
        items TEXT NOT NULL DEFAULT '[]',         -- JSON: extracted to-dos, bills, dates
        triaged_at TEXT NOT NULL,
        notified INTEGER NOT NULL DEFAULT 0       -- Telegram ping sent
    );
    CREATE UNIQUE INDEX emails_message_id ON emails (message_id) WHERE message_id != '';
    CREATE INDEX emails_sent_at ON emails (sent_at);
    """,
    # 8: to-dos that came from email: who sent it and how much
    """
    ALTER TABLE items ADD COLUMN amount TEXT;      -- e.g. "£120.00"
    ALTER TABLE items ADD COLUMN source TEXT;      -- shown on the Board, e.g. "Council"
    ALTER TABLE items ADD COLUMN source_key TEXT;  -- sender domain, for matching reminders
    ALTER TABLE items ADD COLUMN email_id INTEGER; -- emails.id it came from
    """,
    # 9: email threads, so replies and forwards update one to-do instead of adding more
    """
    ALTER TABLE items ADD COLUMN thread_ids TEXT;   -- Message-IDs in its thread, space-separated
    ALTER TABLE items ADD COLUMN subject_key TEXT;  -- subject without Re:/Fwd:, lower case
    """,
    # 10: planned blocks shown on the 📅 Schedule page
    """
    CREATE TABLE plan_blocks (
        id INTEGER PRIMARY KEY,
        day TEXT NOT NULL,                        -- YYYY-MM-DD
        start_time TEXT,                          -- HH:MM; null = untimed (later this week)
        end_time TEXT,
        title TEXT NOT NULL,
        item_id INTEGER,                          -- the Board item it schedules, if any
        minutes INTEGER,                          -- estimated duration
        status TEXT NOT NULL DEFAULT 'planned',   -- planned | done | skipped | dropped
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX plan_blocks_day ON plan_blocks (day);
    """,
    # 11: travel time either side of a fixed commitment (e.g. a 30-minute commute)
    """
    ALTER TABLE items ADD COLUMN travel_minutes INTEGER;
    """,
    # 12: habit ticks, one row per habit per day it was kept (streaks come from these)
    """
    CREATE TABLE habit_log (
        day TEXT NOT NULL,                        -- YYYY-MM-DD
        item_id INTEGER NOT NULL,
        ticked_at TEXT NOT NULL,
        PRIMARY KEY (day, item_id)
    );
    ALTER TABLE items ADD COLUMN streak INTEGER NOT NULL DEFAULT 0;  -- habits: days in a row
    ALTER TABLE items ADD COLUMN kept_on TEXT;    -- habits: last day it was kept
    """,
    # 13: 🧠 Reflect sessions; their messages are tagged so a session can be read whole
    """
    CREATE TABLE reflect_sessions (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        last_at TEXT NOT NULL,                    -- the owner's last message
        ended_at TEXT,
        end_reason TEXT,                          -- done | idle
        log_item_id INTEGER                       -- its 📓 journal entry, once written
    );
    ALTER TABLE chat_messages ADD COLUMN session_id INTEGER;
    CREATE INDEX chat_messages_session ON chat_messages (session_id, id);
    """,
    # 14: suggestion cards and the change log behind /undo-change; habits can be paused
    """
    CREATE TABLE changes (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        source TEXT NOT NULL,                     -- chat | reflect | tweak
        kind TEXT NOT NULL,                       -- fact, habit, goal, focus, ritual, tone
        action TEXT NOT NULL,                     -- add, change, pause, drop, switch, ...
        summary TEXT NOT NULL,                    -- the card's line
        why TEXT,
        payload TEXT NOT NULL,                    -- JSON: exactly what Apply does
        status TEXT NOT NULL,                     -- proposed, applied, dismissed, undone, ...
        decided_at TEXT,
        before TEXT                               -- JSON: what Apply replaced, for undo
    );
    ALTER TABLE items ADD COLUMN paused_until TEXT;  -- habits: not due before this day
    """,
    # Planned blocks know where they came from, so Approve only replaces its own:
    # plan (a draft) | commit (chat) | carry (review follow-up) | callout (First thing)
    """
    ALTER TABLE plan_blocks ADD COLUMN source TEXT NOT NULL DEFAULT 'plan';
    UPDATE plan_blocks SET source = 'callout'
        WHERE title LIKE 'First thing: %' OR title LIKE 'First step towards: %';
    """,
    # A date can be the day it happens ("call GP Monday"), not only a deadline ("by Friday")
    """
    ALTER TABLE items ADD COLUMN due_on INTEGER NOT NULL DEFAULT 0;  -- 1: due is that day
    """,
    # Roadmaps: a long plan (e.g. the AI career one) worked through step by step. Its steps
    # are to-dos with a stage; it gets its own Board section and daily time on the Schedule.
    """
    CREATE TABLE roadmaps (
        id INTEGER PRIMARY KEY,
        project TEXT NOT NULL UNIQUE,             -- items.project of its steps and habits
        title TEXT NOT NULL,                      -- Board section heading, e.g. "🚀 AI career"
        goal TEXT NOT NULL,
        target TEXT,                              -- when the goal should be reached, as text
        starts TEXT NOT NULL,                     -- YYYY-MM-DD: Monday of week 1
        daily_minutes INTEGER NOT NULL,           -- planned each day...
        short_minutes INTEGER NOT NULL,           -- ...or this on a short day
        short_below INTEGER NOT NULL,             -- a day with fewer free minutes is short
        notes TEXT,                               -- how to spread the work, for the planner
        status TEXT NOT NULL DEFAULT 'active',    -- active | paused | done
        created_at TEXT NOT NULL
    );
    CREATE TABLE roadmap_phases (
        roadmap_id INTEGER NOT NULL,
        phase INTEGER NOT NULL,
        title TEXT NOT NULL,                      -- "Foundations"
        span TEXT NOT NULL,                       -- "weeks 1–4"
        starts TEXT NOT NULL,                     -- YYYY-MM-DD
        ends TEXT NOT NULL,                       -- YYYY-MM-DD: when it should be finished
        goal TEXT NOT NULL,
        checkpoint TEXT NOT NULL,                 -- the gate to the next phase
        PRIMARY KEY (roadmap_id, phase)
    );
    CREATE TABLE roadmap_stages (
        id INTEGER PRIMARY KEY,
        roadmap_id INTEGER NOT NULL,
        phase INTEGER NOT NULL,
        seq INTEGER NOT NULL,                     -- order within the phase
        title TEXT NOT NULL,                      -- "Week 1 — Set up"
        due TEXT,                                 -- YYYY-MM-DD: its steps should be done by
        UNIQUE (roadmap_id, phase, seq)
    );
    ALTER TABLE items ADD COLUMN stage_id INTEGER;  -- a roadmap step: roadmap_stages.id
    ALTER TABLE items ADD COLUMN step INTEGER;      -- its order within the stage
    ALTER TABLE items ADD COLUMN link TEXT;         -- a URL the Board links the title to
    """,
    # A routine that starts when the owner wakes (its time follows the Sleep entry), and
    # planned blocks that cover several to-dos at once (a batched admin block)
    """
    ALTER TABLE items ADD COLUMN anchor TEXT;        -- 'wake': starts on waking
    ALTER TABLE items ADD COLUMN minutes INTEGER;    -- how long an anchored routine takes
    ALTER TABLE plan_blocks ADD COLUMN also_ids TEXT; -- JSON: other to-do ids it covers
    """,
    # A to-do's notes show as a grey line under it on the Board: what's shown there now
    """
    ALTER TABLE items ADD COLUMN board_note TEXT;
    """,
    # 📓 Log by week: when a to-do was dropped, and items kept out of the Log on purpose
    """
    ALTER TABLE items ADD COLUMN dropped_at TEXT;
    ALTER TABLE items ADD COLUMN log_hidden INTEGER NOT NULL DEFAULT 0;  -- 1: not in 📓 Log
    UPDATE items SET dropped_at = updated_at WHERE status = 'dropped';
    """,
    # The activity log (ultron/ops/activity.py): append-only, kept forever. The only edit
    # allowed blanks a deleted 🧠 Reflect session's words (activity.REMOVED).
    """
    CREATE TABLE activity (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,                         -- UTC ISO
        kind TEXT NOT NULL,                       -- activity.KINDS
        cause TEXT NOT NULL,                      -- what was running: "you: /sync", "job: sync"
        summary TEXT NOT NULL,                    -- one line
        text TEXT,                                -- full words: a message, a note
        detail TEXT                               -- JSON: ids, before → after
    );
    CREATE INDEX activity_ts ON activity (ts);
    CREATE INDEX activity_kind ON activity (kind, ts);
    CREATE TRIGGER activity_no_delete BEFORE DELETE ON activity
    BEGIN SELECT RAISE(ABORT, 'the activity log is append-only'); END;
    CREATE TRIGGER activity_only_redact BEFORE UPDATE ON activity
    WHEN NEW.id IS NOT OLD.id OR NEW.ts IS NOT OLD.ts OR NEW.kind IS NOT OLD.kind
        OR NEW.cause IS NOT OLD.cause OR NEW.text IS NOT NULL OR NEW.detail IS NOT NULL
        OR NEW.summary IS NOT '🧠 Reflect message (removed with its session)'
    BEGIN SELECT RAISE(ABORT, 'the activity log is append-only'); END;
    """,
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class JobRun:
    job: str
    last_success: datetime | None
    last_failure: datetime | None
    last_error: str | None
    consecutive_failures: int


class State:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: sync integrations run via asyncio.to_thread and record
        # state from worker threads; this sqlite build is serialized (threadsafety 3).
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            self.db.executescript(f"BEGIN; {sql}; PRAGMA user_version = {number}; COMMIT;")

    def close(self) -> None:
        self.db.close()

    # --- job health ---

    def record_job_success(self, job: str) -> None:
        self.db.execute(
            """INSERT INTO job_runs (job, last_success, consecutive_failures) VALUES (?, ?, 0)
               ON CONFLICT (job) DO UPDATE
               SET last_success = excluded.last_success, consecutive_failures = 0""",
            (job, _now()),
        )

    def record_job_failure(self, job: str, error: str) -> None:
        self.db.execute(
            """INSERT INTO job_runs (job, last_failure, last_error, consecutive_failures)
               VALUES (?, ?, ?, 1)
               ON CONFLICT (job) DO UPDATE
               SET last_failure = excluded.last_failure, last_error = excluded.last_error,
                   consecutive_failures = consecutive_failures + 1""",
            (job, _now(), error[:500]),
        )

    def job_runs(self) -> list[JobRun]:
        rows = self.db.execute("SELECT * FROM job_runs ORDER BY job").fetchall()
        parse = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
        return [
            JobRun(
                job=r["job"],
                last_success=parse(r["last_success"]),
                last_failure=parse(r["last_failure"]),
                last_error=r["last_error"],
                consecutive_failures=r["consecutive_failures"],
            )
            for r in rows
        ]

    # --- API usage ---

    def record_usage(
        self,
        *,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int,
        cache_read_tokens: int,
        cost_usd: float,
    ) -> None:
        self.db.execute(
            """INSERT INTO api_usage (ts, model, purpose, input_tokens, output_tokens,
                                      cache_write_tokens, cache_read_tokens, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now(),
                model,
                purpose,
                input_tokens,
                output_tokens,
                cache_write_tokens,
                cache_read_tokens,
                cost_usd,
            ),
        )

    def cost_since(self, start: datetime) -> float:
        """Total API cost since `start` (any timezone; compared in UTC)."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE ts >= ?",
            (start.astimezone(UTC).isoformat(),),
        ).fetchone()
        return float(row[0])

    def cost_by_purpose_since(self, start: datetime) -> dict[str, float]:
        rows = self.db.execute(
            """SELECT purpose, SUM(cost_usd) FROM api_usage WHERE ts >= ?
               GROUP BY purpose ORDER BY SUM(cost_usd) DESC""",
            (start.astimezone(UTC).isoformat(),),
        ).fetchall()
        return {r[0]: float(r[1]) for r in rows}

    # --- inbox ---

    def mark_inbox_handled(self, block_ids: list[str]) -> None:
        now = _now()
        self.db.executemany(
            "INSERT OR IGNORE INTO inbox_handled (block_id, handled_at) VALUES (?, ?)",
            [(block_id, now) for block_id in block_ids],
        )

    def inbox_handled(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT block_id FROM inbox_handled")}

    # --- key-value ---

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
