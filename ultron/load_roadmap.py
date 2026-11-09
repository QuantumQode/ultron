"""`python -m ultron.load_roadmap FILE.json [--check]`: save a roadmap (roadmap.py).

Safe to run again: steps already there keep their ticks and the owner's wording; only
notes and links are refreshed, and anything new is added. `--check` only prints what the
file holds. The 🗂 Board shows it after the next sync (or `/sync` in Telegram).
"""

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ultron.config import get_settings
from ultron.features.roadmap import RoadmapIn, load
from ultron.ops import activity
from ultron.state import State


def summary(data: RoadmapIn) -> list[str]:
    lines = [
        f"{data.title} · {data.goal}" + (f" · {data.target}" if data.target else ""),
        f"Starts {data.starts:%a %d %b %Y}: {data.daily_minutes} min a day, "
        f"{data.short_minutes} min on days with under {data.short_below} min free",
    ]
    for p in data.phases:
        steps = sum(len(s.steps) for s in p.stages)
        lines.append(
            f"  Phase {p.phase} — {p.title} ({p.span}, {p.starts:%d %b %Y}–{p.ends:%d %b %Y}): "
            f"{len(p.stages)} stage(s), {steps} step(s)"
        )
    for h in data.habits:
        start = f" from {h.starts:%d %b %Y}" if h.starts else ""
        lines.append(f"  Habit: {h.title} ({h.days or 'every day'}{start})")
    return lines


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    check = "--check" in args
    files = [a for a in args if a != "--check"]
    if len(files) != 1:
        sys.exit("Usage: python -m ultron.load_roadmap FILE.json [--check]")
    try:
        data = RoadmapIn.model_validate(json.loads(Path(files[0]).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        sys.exit(f"❌ Can't read {files[0]}: {exc}")
    print("\n".join(summary(data)))
    if check:
        return
    try:
        settings = get_settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        sys.exit(f"Fix these settings in .env first: {missing}")
    state = State(settings.db_path)
    activity.attach(state.db, settings.secret_values())
    try:
        result = load(state, data)
    finally:
        state.close()
    print(
        f"✅ Saved: {result.steps_added} new step(s), {result.steps_kept} already there, "
        f"{result.habits_added} new habit(s). Send /sync in Telegram to see it on the Board."
    )


if __name__ == "__main__":
    main()
