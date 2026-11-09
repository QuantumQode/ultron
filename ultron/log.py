"""Read the activity log (read-only).

    python -m ultron.log                      today
    python -m ultron.log --day 2026-11-04     one day
    python -m ultron.log --days 7 --kind item
    python -m ultron.log --grep workout --full
On the server: docker exec ultron python -m ultron.log ...
"""

import argparse
from datetime import date, datetime, time, timedelta

from ultron.config import get_settings
from ultron.ops import activity
from ultron.state import State


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read Ultron's activity log.")
    parser.add_argument("--day", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=1, help="days back from today (default 1)")
    parser.add_argument("--kind", choices=activity.KINDS, action="append", help="repeatable")
    parser.add_argument("--grep", help="words to look for")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--full", action="store_true", help="show full text and details")
    args = parser.parse_args(argv)

    settings = get_settings()
    zone = settings.zone
    if args.day:
        start = datetime.combine(date.fromisoformat(args.day), time.min, zone)
        end = start + timedelta(days=1)
    else:
        today = datetime.now(zone).date()
        start = datetime.combine(today - timedelta(days=max(1, args.days) - 1), time.min, zone)
        end = None
    state = State(settings.db_path)
    try:
        found = activity.entries(
            state.db, start=start, end=end, kinds=args.kind, query=args.grep, limit=args.limit
        )
    finally:
        state.close()
    for entry in found:
        print(activity.line(entry, zone, full=args.full))
    print(f"-- {len(found)} event(s)")


if __name__ == "__main__":
    main()
