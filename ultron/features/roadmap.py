"""Roadmaps: a long plan (the AI career one) worked through a little every day.

- A roadmap has phases ("Foundations", weeks 1–4), each split into stages ("Week 1 — Set
  up", due Sun 1 Nov). Its steps are ordinary to-dos with a stage and an order, so ticks,
  rewording, deletions and Schedule links work as for any to-do. Repeating parts ("message
  2 people a week") are habits in the roadmap's project, paused until their phase starts.
- 🗂 Board: its own always-open section above ✅ To-do (board.py). A status box at the top,
  then the phases under way as stage headings with their steps; later phases are folded.
- 📅 Schedule: each planned day must give it `daily_minutes` (`short_minutes` on a day with
  less free time than `short_below`), on the next unfinished steps. Code checks the draft
  (planner.py) and asks once more if it's short.
- A step can take several sessions: ticking its block on the Schedule logs a session; the
  step stays open until it's ticked on the Board (or ✅ at the evening review).
- Accountability: a stage past its date is called out, and the weekly review counts the
  time put in against the plan, warning when a phase runs over 4 weeks late.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from ultron.errors import UserFacingError
from ultron.features import profile
from ultron.features.freetime import shape_of, to_min
from ultron.features.items import Item, ItemStore
from ultron.features.profile import PlanSettings
from ultron.features.schedule import Block, PlanStore, fmt_minutes
from ultron.state import State

NEXT_STEPS = 6  # steps of the current phase shown to the planner
NEXT_STEPS_OTHER = 3  # ...and of each other phase under way
LATE_PHASE = timedelta(weeks=4)  # the roadmap's rule: past this, cut scope rather than stop


class RoadmapError(UserFacingError):
    pass


@dataclass(frozen=True)
class Roadmap:
    id: int
    project: str
    title: str
    goal: str
    target: str | None
    starts: date
    daily_minutes: int
    short_minutes: int
    short_below: int
    notes: str | None
    status: str


@dataclass(frozen=True)
class Phase:
    phase: int
    title: str
    span: str
    starts: date
    ends: date
    goal: str
    checkpoint: str

    @property
    def name(self) -> str:
        return f"Phase {self.phase} — {self.title}"

    def weeks(self) -> int:
        return (self.ends - self.starts).days // 7 + 1

    def week_of(self, day: date) -> int:
        return (day - self.starts).days // 7 + 1


@dataclass(frozen=True)
class Stage:
    id: int
    phase: int
    seq: int
    title: str
    due: date | None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class RoadmapStore:
    def __init__(self, state: State) -> None:
        self.db = state.db

    def _roadmap(self, r: Any) -> Roadmap:
        fields = {k: r[k] for k in r.keys() if k != "created_at"}
        return Roadmap(**{**fields, "starts": date.fromisoformat(r["starts"])})

    def active(self) -> Roadmap | None:
        r = self.db.execute(
            "SELECT * FROM roadmaps WHERE status = 'active' ORDER BY id LIMIT 1"
        ).fetchone()
        return self._roadmap(r) if r else None

    def by_project(self, project: str) -> Roadmap | None:
        r = self.db.execute("SELECT * FROM roadmaps WHERE project = ?", (project,)).fetchone()
        return self._roadmap(r) if r else None

    def phases(self, roadmap_id: int) -> list[Phase]:
        rows = self.db.execute(
            "SELECT phase, title, span, starts, ends, goal, checkpoint FROM roadmap_phases "
            "WHERE roadmap_id = ? ORDER BY phase",
            (roadmap_id,),
        ).fetchall()
        return [
            Phase(
                **{
                    **dict(r),
                    "starts": date.fromisoformat(r["starts"]),
                    "ends": date.fromisoformat(r["ends"]),
                }
            )
            for r in rows
        ]

    def stages(self, roadmap_id: int) -> list[Stage]:
        rows = self.db.execute(
            "SELECT id, phase, seq, title, due FROM roadmap_stages WHERE roadmap_id = ? "
            "ORDER BY phase, seq",
            (roadmap_id,),
        ).fetchall()
        return [Stage(**{**dict(r), "due": _date(r["due"])}) for r in rows]


# --- what's under way ---


@dataclass
class View:
    """A roadmap with its phases, stages, steps and habits, as of one day."""

    roadmap: Roadmap
    phases: list[Phase]
    stages: list[Stage]
    steps: list[Item]  # not dropped, in roadmap order
    habits: list[Item]  # open habits in the roadmap's project
    today: date
    open_phases: list[int] = field(default_factory=list)  # under way: shown open, planned from

    def __post_init__(self) -> None:
        self._stages = {s.id: s for s in self.stages}
        self._phases = {p.phase: p for p in self.phases}
        with_open = sorted({self.stage_of(i).phase for i in self.open_steps()})
        self.open_phases = [
            n
            for n in with_open
            if n == with_open[0] or self._phases[n].starts <= self.today  # the earliest, or begun
        ]

    def stage_of(self, item: Item) -> Stage:
        return self._stages[item.stage_id]

    def phase(self, number: int) -> Phase:
        return self._phases[number]

    def open_steps(self, phase: int | None = None) -> list[Item]:
        return [
            i
            for i in self.steps
            if i.status == "open" and (phase is None or self.stage_of(i).phase == phase)
        ]

    @property
    def current(self) -> Phase | None:
        """The earliest phase with steps left."""
        return self._phases[self.open_phases[0]] if self.open_phases else None

    def place(self, stage_id: int) -> str:
        """Where a stage's steps sit on the Board: under its own heading while its phase is
        under way, else folded inside its phase's toggle."""
        stage = self._stages[stage_id]
        return f"stage:{stage.id}" if stage.phase in self.open_phases else f"phase:{stage.phase}"

    def groups(self) -> dict[int, str]:
        return {s.id: self.place(s.id) for s in self.stages}

    def linked_ids(self) -> set[int]:
        """Items whose planned blocks count as time on the roadmap."""
        return {i.id for i in self.steps} | {h.id for h in self.habits}

    def stage_left(self, stage: Stage) -> list[Item]:
        return [i for i in self.open_steps() if i.stage_id == stage.id]

    def current_stage(self) -> Stage | None:
        """Where new to-dos for the roadmap go: the first stage with steps left."""
        for stage in self.stages:
            if self.stage_left(stage):
                return stage
        return self.stages[-1] if self.stages else None


def order_key(stages: dict[int, Stage], item: Item) -> tuple:
    stage = stages[item.stage_id]
    return (stage.phase, stage.seq, item.step or 0, item.id)


def view(state: State, today: date) -> View | None:
    rs = RoadmapStore(state)
    roadmap = rs.active()
    if roadmap is None:
        return None
    stages = rs.stages(roadmap.id)
    by_id = {s.id: s for s in stages}
    items = ItemStore(state).all()
    steps = sorted(
        (i for i in items if i.is_step and i.stage_id in by_id), key=lambda i: order_key(by_id, i)
    )
    habits = [
        i
        for i in items
        if i.kind == "habit" and i.status == "open" and i.project == roadmap.project
    ]
    return View(roadmap, rs.phases(roadmap.id), stages, steps, habits, today)


# --- time ---


def day_target(v: View, items: list[Item], day: date, settings: PlanSettings) -> int:
    """Minutes the roadmap needs on `day`: fewer on a short day (e.g. a work day), none
    before it starts or once every step is done."""
    rm = v.roadmap
    if day < rm.starts or not v.open_steps():
        return 0
    free = shape_of(day, items, settings=settings).free_minutes
    return rm.short_minutes if free < rm.short_below else rm.daily_minutes


def need_on(state: State, v: View, day: date, now: datetime) -> int:
    """Minutes still to plan for the roadmap on `day`: today, what's already done counts."""
    target = day_target(v, ItemStore(state).all(), day, profile.load(state))
    if target and day == now.date():
        target -= spent(state, v, day, day)
    return max(0, target)


def block_minutes(b: Block | dict[str, Any]) -> int:
    start, end = (b.start, b.end) if isinstance(b, Block) else (b["start"], b["end"])
    if start and end:
        return max(0, to_min(end) - to_min(start))
    return (b.minutes if isinstance(b, Block) else b.get("minutes")) or 0


def spent(state: State, v: View, start: date, end: date) -> int:
    """Minutes of roadmap blocks done from `start` to `end` (inclusive)."""
    ids = v.linked_ids()
    plans = PlanStore(state)
    total = 0
    day = start
    while day <= end:
        total += sum(
            block_minutes(b) for b in plans.for_day(day) if b.status == "done" and b.item_id in ids
        )
        day += timedelta(days=1)
    return total


def sessions(state: State, item_ids: list[int]) -> dict[int, tuple[int, int]]:
    """Sessions done on each step so far: item id -> (count, minutes)."""
    if not item_ids:
        return {}
    marks = ",".join("?" for _ in item_ids)
    rows = state.db.execute(
        f"SELECT item_id, start_time, end_time, minutes FROM plan_blocks "
        f"WHERE status = 'done' AND item_id IN ({marks})",
        item_ids,
    ).fetchall()
    out: dict[int, tuple[int, int]] = {}
    for r in rows:
        minutes = block_minutes(
            {"start": r["start_time"], "end": r["end_time"], "minutes": r["minutes"]}
        )
        n, m = out.get(r["item_id"], (0, 0))
        out[r["item_id"]] = (n + 1, m + minutes)
    return out


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_target(v: View, items: list[Item], start: date, settings: PlanSettings) -> int:
    return sum(day_target(v, items, start + timedelta(days=n), settings) for n in range(7))


# --- words ---


def stage_suffix(stage: Stage, today: date) -> str:
    if not stage.due:
        return ""
    if stage.due < today:
        return f" · was due {stage.due:%a %d %b}"
    return f" · by {stage.due:%a %d %b}" + (
        f" {stage.due.year}" if stage.due.year != today.year else ""
    )


def stage_heading(stage: Stage, today: date) -> str:
    return stage.title + stage_suffix(stage, today)


def phase_heading(phase: Phase, today: date) -> str:
    when = f"starts {phase.starts:%a %d %b}" + (
        f" {phase.starts.year}" if phase.starts.year != today.year else ""
    )
    return f"{phase.name} · {phase.span} · {when}"


def phase_progress(phase: Phase, today: date) -> str:
    """'week 2 of 4', 'starts Mon 26 Oct' or '3 weeks over'."""
    if today < phase.starts:
        return f"starts {phase.starts:%a %d %b}"
    if today > phase.ends:
        over = (today - phase.ends).days // 7
        weeks = f"{over} week{'s' if over != 1 else ''} " if over else ""
        return f"{weeks}past its end ({phase.ends:%a %d %b})"
    return f"week {phase.week_of(today)} of {phase.weeks()}"


def status_lines(state: State, v: View, today: date) -> list[str]:
    """The status box at the top of the Board section."""
    rm = v.roadmap
    lines = [f"🎯 {rm.goal}" + (f" · {rm.target}" if rm.target else "")]
    phase = v.current
    if phase is None:
        lines.append("Every step is done. 🎉 Tell Ultron what's next.")
        return lines
    left = v.open_steps(phase.phase)
    done = [i for i in v.steps if i.status == "done" and v.stage_of(i).phase == phase.phase]
    lines.append(
        f"Now: {phase.name} · {phase_progress(phase, today)} · "
        f"{len(done)} of {len(done) + len(left)} steps done"
    )
    lines.append(f"Checkpoint: {phase.checkpoint}")
    others = [v.phase(n).name for n in v.open_phases[1:]]
    if others:
        lines.append("Also under way: " + ", ".join(others))
    items = ItemStore(state).all()
    if today < rm.starts:
        lines.append(
            f"Daily time starts {rm.starts:%a %d %b}: {fmt_minutes(rm.daily_minutes)} a day, "
            f"{fmt_minutes(rm.short_minutes)} on short days"
        )
    else:
        start = monday(today)
        lines.append(
            f"This week: {fmt_minutes(spent(state, v, start, today))} of "
            f"{fmt_minutes(week_target(v, items, start, profile.load(state)))} done"
        )
    late = overdue_stages(v, today)
    if late:
        stage, steps = late[0]
        left = sum(len(s) for _, s in late)
        what = f"{stage.title} was due {stage.due:%a %d %b}"
        if len(late) > 1:
            what = f"{len(late)} stages past their date, from {what}"
        lines.append(f"⚠️ Behind: {what} ({left} step{'s' if left != 1 else ''} left)")
    return lines


def overdue_stages(v: View, today: date) -> list[tuple[Stage, list[Item]]]:
    return [(s, left) for s in v.stages if s.due and s.due < today and (left := v.stage_left(s))]


# --- what the Board shows ---


@dataclass(frozen=True)
class BoardPlan:
    """Everything board.py needs to draw the roadmap section, worked out beforehand."""

    title: str  # the section heading
    status: str  # the status box
    groups: dict[int, str]  # stage id -> "stage:<id>" (open) or "phase:<n>" (folded)
    stages: list[Stage]  # in order
    stage_headings: dict[int, str]  # stage id -> its heading or label text
    folded: dict[int, tuple[str, str]]  # folded phase -> (toggle text, its goal line)
    multi_stage: frozenset[int]  # phases with more than one stage: labels inside the toggle


def board_plan(state: State, today: date) -> BoardPlan | None:
    v = view(state, today)
    if v is None:
        return None
    groups = v.groups()
    folded = {
        p.phase: (phase_heading(p, today), f"Goal: {p.goal} Checkpoint: {p.checkpoint}")
        for p in v.phases
        if p.phase not in v.open_phases
    }
    counts: dict[int, int] = {}
    for s in v.stages:
        counts[s.phase] = counts.get(s.phase, 0) + 1
    return BoardPlan(
        title=v.roadmap.title,
        status="\n".join(status_lines(state, v, today)).removeprefix("🎯 "),
        groups=groups,
        stages=v.stages,
        stage_headings={s.id: stage_heading(s, today) for s in v.stages},
        folded=folded,
        multi_stage=frozenset(n for n, c in counts.items() if c > 1),
    )


# --- the planner ---


def next_steps(v: View) -> list[Item]:
    """What the planner should work on: the first few steps of each phase under way."""
    out: list[Item] = []
    for n, phase in enumerate(v.open_phases):
        out += v.open_steps(phase)[: NEXT_STEPS if n == 0 else NEXT_STEPS_OTHER]
    return out


def context_lines(state: State, now: datetime, day: date) -> list[str]:
    """The roadmap for the planner and tonight's suggestions ([] if there isn't one)."""
    v = view(state, now.date())
    return planner_lines(state, v, day) if v else []


def day_line(state: State, now: datetime, day: date) -> str | None:
    """'AI career: at least 110 min this day …' for one day of the planner's context."""
    v = view(state, now.date())
    need = need_on(state, v, day, now) if v else 0
    if not v or not need:
        return None
    return f"{v.roadmap.project}: at least {need} min this day, on its next steps"


def planner_lines(state: State, v: View, day: date) -> list[str]:
    rm = v.roadmap
    lines = [
        f'Roadmap "{rm.project}" (the owner\'s main long-term plan): {rm.goal}'
        + (f" · target {rm.target}" if rm.target else "")
    ]
    phase = v.current
    if phase:
        lines.append(
            f"Now: {phase.name} ({phase.span}; {phase_progress(phase, day)}). "
            f"Checkpoint: {phase.checkpoint}"
        )
    for n in v.open_phases[1:]:
        lines.append(f"Also under way: {v.phase(n).name} ({v.phase(n).span})")
    if rm.notes:
        lines.append(f"How to spread the work: {rm.notes}")
    lines.append(
        f"Daily time: {rm.daily_minutes} min, {rm.short_minutes} min on a day with under "
        f"{fmt_minutes(rm.short_below)} free, from {rm.starts:%a %d %b}. Blocks for its steps "
        f'and its habits count. Title them "{rm.project}: <step>".'
    )
    steps = next_steps(v)
    done = sessions(state, [i.id for i in steps])
    lines.append(
        "Its next steps, in order (a step may take several sessions; finish each stage by "
        "its date; set item_id to the step's id):"
    )
    for item in steps:
        stage = v.stage_of(item)
        bits = [stage.title]
        if stage.due:
            bits.append(f"stage due {stage.due.isoformat()}")
        if item.due:
            bits.append(f"{'ON' if item.due_on else 'due'} {item.due}")
        if item.id in done:
            n, minutes = done[item.id]
            bits.append(f"worked on {n}x, {fmt_minutes(minutes)} so far")
        lines.append(f"- [#{item.id}] {item.title} · " + " · ".join(bits))
    late = overdue_stages(v, day)
    if late:
        lines.append(
            "Behind plan: "
            + "; ".join(f"{s.title} was due {s.due.isoformat()}" for s, _ in late)
            + ". Catch up on these first."
        )
    return lines


def shortfalls(
    v: View, by_day: dict[date, list[Any]], needed: dict[date, int], many: bool
) -> list[str]:
    """Planned days that don't give the roadmap its time."""
    ids = v.linked_ids()
    problems = []
    for day, blocks in by_day.items():
        need = needed.get(day, 0)
        if need <= 0:
            continue
        got = sum(block_minutes(_as_dict(b)) for b in blocks if _item_id(b) in ids)
        if got < need:
            prefix = f"{day:%a}: " if many else ""
            problems.append(
                f"{prefix}{v.roadmap.project} gets {got} of the {need} min it needs. Add "
                f"blocks for its next steps (with their item_id), unless the owner asked for "
                f"less"
            )
    return problems


def _as_dict(b: Any) -> dict[str, Any]:
    return b if isinstance(b, dict) else {"start": b.start, "end": b.end, "minutes": None}


def _item_id(b: Any) -> int | None:
    return b["item_id"] if isinstance(b, dict) else b.item_id


def short_note(v: View, by_day: dict[date, list[Any]], needed: dict[date, int]) -> str | None:
    """A line under the draft when a day still falls short after the retry."""
    ids = v.linked_ids()
    short = []
    for day, blocks in by_day.items():
        need = needed.get(day, 0)
        got = sum(block_minutes(_as_dict(b)) for b in blocks if _item_id(b) in ids)
        if need and got < need:
            short.append(f"{day:%a} {fmt_minutes(got)} of {fmt_minutes(need)}")
    if not short:
        return None
    return f"⚠️ {v.roadmap.project} gets less than it needs: " + ", ".join(short)


def chat_lines(state: State, today: date) -> list[str]:
    """The roadmap for chat: where things stand and what's next."""
    v = view(state, today)
    if v is None:
        return []
    rm = v.roadmap
    lines = [
        f'🚀 Roadmap "{rm.project}": its own Board section ({rm.title}). Its steps are '
        f'to-dos in project "{rm.project}"; ticking one finishes the step.'
    ]
    lines += [f"  {line}" for line in status_lines(state, v, today)]
    steps = next_steps(v)[:4]
    if steps:
        lines.append("  Next steps: " + "; ".join(f"#{i.id} {i.title}" for i in steps))
    return lines


# --- new to-dos for the roadmap ---


def adopt(state: State, today: date, typed: dict[str, str | None] | None = None) -> int:
    """Give a stage to roadmap to-dos that have none yet. A line typed under a stage heading
    (or inside a folded phase) goes in that stage; another to-do in the roadmap's project
    (from chat, a note or a follow-up) goes in the first stage with steps left."""
    v = view(state, today)
    if v is None or not v.stages:
        return 0
    typed = typed or {}
    stages = {s.id: s for s in v.stages}
    first_of_phase = {}
    for s in v.stages:
        first_of_phase.setdefault(s.phase, s)
    store = ItemStore(state)
    adopted = 0
    for item in store.all():
        if item.kind != "task" or item.status != "open" or item.stage_id is not None:
            continue
        stage = None
        for block_id in item.source_blocks:
            group = typed.get(block_id) or ""
            kind, _, ref = group.partition(":")
            if kind == "stage" and ref.isdigit() and int(ref) in stages:
                stage = stages[int(ref)]
            elif kind == "phase" and ref.isdigit() and int(ref) in first_of_phase:
                stage = first_of_phase[int(ref)]
            elif kind == "roadmap":  # under the section's own heading
                stage = v.current_stage()
        if stage is None and item.project == v.roadmap.project:
            stage = v.current_stage()
        if stage is None:
            continue
        last = state.db.execute(
            "SELECT MAX(step) FROM items WHERE stage_id = ?", (stage.id,)
        ).fetchone()[0]
        store.update(item.id, stage_id=stage.id, step=(last or 0) + 1, project=v.roadmap.project)
        adopted += 1
    return adopted


# --- the weekly review ---


def weekly_lines(state: State, start: date, end: date) -> list[str]:
    v = view(state, end)
    if v is None or end < v.roadmap.starts:
        return []
    items = ItemStore(state).all()
    rm = v.roadmap
    got = spent(state, v, start, end)
    target = week_target(v, items, start, profile.load(state))
    pct = f" ({round(100 * got / target)}%)" if target else ""
    lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
    finished = [i for i in v.steps if i.status == "done" and lo <= (i.done_at or "") < hi]
    lines = [
        f"🚀 {rm.project}: {fmt_minutes(got)} of {fmt_minutes(target)}{pct} · "
        f"{len(finished)} step{'s' if len(finished) != 1 else ''} finished"
    ]
    phase = v.current
    if phase:
        lines.append(f"  Now: {phase.name}, {phase_progress(phase, end)}")
        lines.append(f"  Checkpoint: {phase.checkpoint}")
    for stage, left in overdue_stages(v, end):
        lines.append(f"  Behind: {stage.title} (due {stage.due:%a %d %b}, {len(left)} left)")
    for n in v.open_phases:
        p = v.phase(n)
        if end > p.ends + LATE_PHASE:
            lines.append(
                f"  ⚠️ {p.name} is over 4 weeks behind its plan: cut scope rather than stop."
            )
    return lines


# --- loading a roadmap from a file ---


class StepIn(BaseModel):
    title: str
    details: str | None = None
    link: str | None = None


class StageIn(BaseModel):
    title: str
    due: date | None = None
    steps: list[StepIn]


class PhaseIn(BaseModel):
    phase: int
    title: str
    span: str
    starts: date
    ends: date
    goal: str
    checkpoint: str
    stages: list[StageIn]


class HabitIn(BaseModel):
    title: str
    days: str | None = Field(None, description='"mon,thu"; none = every day')
    starts: date | None = None
    details: str | None = None


class RoadmapIn(BaseModel):
    project: str
    title: str
    goal: str
    target: str | None = None
    area: str = "Career"
    starts: date
    daily_minutes: int
    short_minutes: int
    short_below: int
    notes: str | None = None
    phases: list[PhaseIn]
    habits: list[HabitIn] = []


@dataclass
class Loaded:
    roadmap: Roadmap
    steps_added: int = 0
    steps_kept: int = 0
    habits_added: int = 0


def load(state: State, data: RoadmapIn) -> Loaded:
    """Save a roadmap. Safe to run again: stages and steps already there keep their ids,
    ticks and the owner's wording; only notes and links are refreshed, and anything new
    is added."""
    db = state.db
    now = datetime.now(UTC).isoformat()
    db.execute("BEGIN")
    try:
        db.execute(
            """INSERT INTO roadmaps (project, title, goal, target, starts, daily_minutes,
                   short_minutes, short_below, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (project) DO UPDATE SET title = excluded.title,
                   goal = excluded.goal, target = excluded.target, starts = excluded.starts,
                   daily_minutes = excluded.daily_minutes,
                   short_minutes = excluded.short_minutes,
                   short_below = excluded.short_below, notes = excluded.notes""",
            (
                data.project,
                data.title,
                data.goal,
                data.target,
                data.starts.isoformat(),
                data.daily_minutes,
                data.short_minutes,
                data.short_below,
                data.notes,
                now,
            ),
        )
        rm = RoadmapStore(state).by_project(data.project)
        assert rm is not None
        db.execute("DELETE FROM roadmap_phases WHERE roadmap_id = ?", (rm.id,))
        store = ItemStore(state)
        result = Loaded(rm)
        for p in data.phases:
            db.execute(
                "INSERT INTO roadmap_phases (roadmap_id, phase, title, span, starts, ends, goal, "
                "checkpoint) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rm.id,
                    p.phase,
                    p.title,
                    p.span,
                    p.starts.isoformat(),
                    p.ends.isoformat(),
                    p.goal,
                    p.checkpoint,
                ),
            )
            for seq, s in enumerate(p.stages, 1):
                db.execute(
                    """INSERT INTO roadmap_stages (roadmap_id, phase, seq, title, due)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (roadmap_id, phase, seq)
                       DO UPDATE SET title = excluded.title, due = excluded.due""",
                    (rm.id, p.phase, seq, s.title, s.due.isoformat() if s.due else None),
                )
                stage_id = db.execute(
                    "SELECT id FROM roadmap_stages WHERE roadmap_id = ? AND phase = ? AND seq = ?",
                    (rm.id, p.phase, seq),
                ).fetchone()[0]
                for n, step in enumerate(s.steps, 1):
                    row = db.execute(
                        "SELECT id FROM items WHERE stage_id = ? AND step = ?", (stage_id, n)
                    ).fetchone()
                    if row:
                        store.update(row[0], details=step.details, link=step.link)
                        result.steps_kept += 1
                        continue
                    store.add(
                        kind="task",
                        title=step.title,
                        details=step.details,
                        project=data.project,
                        area=data.area,
                        due=None,
                        question=None,
                        stage_id=stage_id,
                        step=n,
                        link=step.link,
                    )
                    result.steps_added += 1
        have = {
            i.title.casefold()
            for i in store.all(include_dropped=True)
            if i.kind == "habit" and i.project == data.project
        }
        for h in data.habits:
            if h.title.casefold() in have:
                continue
            store.add(
                kind="habit",
                title=h.title,
                details=h.details,
                project=data.project,
                area=data.area,
                due=None,
                question=None,
                days=h.days,
                paused_until=h.starts.isoformat() if h.starts else None,
            )
            result.habits_added += 1
        store.upsert_project(data.project, data.area, f"roadmap: see {data.title} on the Board")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    result.roadmap = RoadmapStore(state).by_project(data.project) or rm
    return result
