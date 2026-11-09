"""The chat agent: one reply to one owner message, using tools, with Haiku→Sonnet escalation.

- 💬 Chat starts on the fast model, which can hand over with `think_harder`; a message
  starting with "!" goes straight to the smart model.
- 🧠 Reflect always uses the smart model.
- At the monthly cap, chat stays on the fast model and never escalates.
- History: the last 20 messages per space, plus a running summary of older ones. A Reflect
  session starts fresh: it sees only its own messages and is never folded.
"""

import logging
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ultron.brain import prompts
from ultron.brain.tools import (
    THINK_HARDER,
    TOOLS,
    EmailSearch,
    Suggest,
    ToolEffects,
    line,
    run_tool,
)
from ultron.config import Settings, week_ahead
from ultron.features import email_report, focus, reflect, roadmap, schedule, suggestions
from ultron.features.freetime import is_one_off
from ultron.features.items import ItemStore
from ultron.features.triage import EmailStore
from ultron.notify import Space
from ultron.ops import activity
from ultron.ops.guard import CostGuard
from ultron.ops.status import health_lines
from ultron.state import State

log = logging.getLogger(__name__)

HISTORY = 20  # messages re-read every turn
FOLD_AT = 30  # when a space has this many stored messages, fold the oldest into the summary
SESSION_HISTORY = 60  # messages of the current Reflect session re-read every turn
MAX_ROUNDS = 6  # tool-use rounds per reply
MAX_TOKENS = 4000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class ChatReply:
    text: str
    model: str  # model that produced the final answer
    cost_usd: float = 0.0
    escalated: bool = False
    effects: ToolEffects = field(default_factory=ToolEffects)
    note: str | None = None  # e.g. "budget used up, stayed on Haiku"
    offer_reflect: bool = False  # personal message in normal chat: offer a Reflect session


# --- history ---


class History:
    def __init__(self, state: State) -> None:
        self.state = state
        self.db = state.db

    def add(self, space: str, role: str, text: str, session_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO chat_messages (ts, space, role, text, session_id) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), space, role, text, session_id),
        )

    def recent(
        self, space: str, limit: int = HISTORY, session_id: int | None = None
    ) -> list[dict[str, str]]:
        if session_id is None:
            rows = self.db.execute(
                "SELECT role, text FROM chat_messages WHERE space = ? ORDER BY id DESC LIMIT ?",
                (space, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT role, text FROM chat_messages WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        messages = [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]
        while messages and messages[0]["role"] != "user":
            messages.pop(0)  # the API needs the conversation to start with the owner
        return messages

    def summary(self, space: str) -> str:
        return self.state.get(f"chat.summary.{space}", "") or ""

    def to_fold(self, space: str) -> list[tuple[int, str, str]]:
        count = self.db.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE space = ? AND session_id IS NULL", (space,)
        ).fetchone()[0]
        if count < FOLD_AT:
            return []
        rows = self.db.execute(
            "SELECT id, role, text FROM chat_messages WHERE space = ? AND session_id IS NULL "
            "ORDER BY id LIMIT ?",
            (space, count - HISTORY),
        ).fetchall()
        return [(r["id"], r["role"], r["text"]) for r in rows]

    def fold(self, space: str, ids: list[int], summary: str) -> None:
        self.state.set(f"chat.summary.{space}", summary)
        self.db.executemany("DELETE FROM chat_messages WHERE id = ?", [(i,) for i in ids])

    def forget(self, space: str | None = None) -> None:
        if space:
            self.db.execute("DELETE FROM chat_messages WHERE space = ?", (space,))
            self.state.set(f"chat.summary.{space}", "")
        else:
            self.db.execute("DELETE FROM chat_messages")
            for s in Space:
                self.state.set(f"chat.summary.{s.value}", "")


# --- context ---


def schedule_lines(state: State, now: datetime) -> list[str]:
    """Today's and tomorrow's 📅 Schedule as the owner sees it, plus one-off appointments
    in the next week, so 'what's on tomorrow?' gets the real plan."""
    view = schedule.build_view(state, now.date())
    lines = []
    for title, rows in list(schedule.render(view).values())[:2]:
        lines += [f"📅 {title}:"] + [f"  {line.sig}" for line in rows]
    lines += roadmap.chat_lines(state, now.date())
    week = now.date() + timedelta(days=schedule.WEEK_DAYS)
    one_offs = [
        i
        for i in ItemStore(state).all()
        if is_one_off(i) and i.status == "open" and i.due <= week.isoformat()
    ]
    if one_offs:
        lines.append("Appointments this week:")
        lines += [
            f"  #{i.id} {i.due} {i.start_time or ''}"
            + (f"–{i.end_time}" if i.end_time else " (length guessed)" if i.start_time else "")
            + f" {i.title}"
            for i in one_offs
        ]
    return lines


def build_context(
    store: ItemStore,
    history: History,
    space: str,
    now: datetime,
    emails: EmailStore | None = None,
    focus_info: list[str] | None = None,
    *,
    with_summary: bool = True,
    history_lines: list[str] | None = None,
    health: list[str] | None = None,
    plan_lines: list[str] | None = None,
) -> str:
    """Day-specific context for the system prompt. Sent after the cached part."""
    items = store.all()
    open_items = [i for i in items if i.status == "open"]
    tasks = [i for i in open_items if i.kind == "task" and not i.is_step]  # steps: see Roadmap
    overdue = [t for t in tasks if t.due and t.due < now.date().isoformat()]
    soon = sorted(
        (t for t in tasks if t.due and t.due >= now.date().isoformat()), key=lambda t: t.due
    )
    projects = sorted({t.project for t in tasks if t.project})

    def section(title: str, rows: list[str]) -> list[str]:
        return [f"{title}:"] + (rows or ["(none)"]) + [""]

    parts = [
        f"Now: {now:%A %d %B %Y, %H:%M} ({now.tzinfo})",
        f"Next 7 days: {week_ahead(now.date())}",
        "",
    ]
    parts += (focus_info or []) + ([""] if focus_info else [])
    parts += section("Fixed schedule", [line(i) for i in open_items if i.kind == "schedule"])
    if plan_lines:
        parts += plan_lines + [""]
    parts += section("Goals", [line(i) for i in open_items if i.kind == "goal"])
    parts += section("Habits", [line(i) for i in open_items if i.kind == "habit"])
    parts += section("Facts about the owner", [line(i) for i in open_items if i.kind == "fact"])
    parts += section("Projects with open to-dos", projects)
    parts += [
        f"Open to-dos: {len(tasks)} ({len(overdue)} overdue). Use find_items for details.",
        "",
    ]
    parts += section("Overdue", [line(t) for t in overdue[:10]])
    parts += section("Due next", [line(t) for t in soon[:10]])
    if emails is not None:
        parts += section(
            "Emails from people waiting for a reply (last 14 days)",
            email_report.context_lines(emails, now, now.tzinfo),
        )
    if history_lines:
        parts += history_lines + [""]
    if health:
        parts += section("Ultron's own jobs", health)
    summary = history.summary(space) if with_summary else ""
    if summary:
        parts += section("Summary of earlier conversation", [summary])
    text = "\n".join(parts).replace("</context>", "< /context>")
    return f"<context>\n{text}</context>"


# --- the agent loop ---


def _text_of(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


async def _create(llm: Any, *, model: str, smart: bool, **kwargs: Any) -> Any:
    kwargs.update(model=model, max_tokens=MAX_TOKENS, cache_control={"type": "ephemeral"})
    if smart:
        # Server-side fallback: if a safety filter wrongly declines, the API re-runs on a
        # fallback model within the same call.
        return await llm.beta.messages.create(
            betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}, **kwargs
        )
    return await llm.messages.create(**kwargs)


@dataclass
class _Run:
    text: str
    model: str
    cost: float
    escalate: bool = False
    refused: bool = False


async def _run(
    llm: Any,
    guard: CostGuard,
    store: ItemStore,
    effects: ToolEffects,
    *,
    model: str,
    smart: bool,
    system: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    effort: str,
    purpose: str,
    search_email: EmailSearch | None = None,
    suggest: Suggest | None = None,
) -> _Run:
    cost = 0.0
    messages = list(messages)
    for _ in range(MAX_ROUNDS):
        response = await _create(
            llm,
            model=model,
            smart=smart,
            system=system,
            messages=messages,
            tools=tools,
            output_config={"effort": effort},
        )
        served_by = getattr(response, "model", model) or model
        cost += await guard.record(model=served_by, purpose=purpose, usage=response.usage)
        if response.stop_reason == "refusal":
            return _Run("I can't help with that one.", served_by, cost, refused=True)
        uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if response.stop_reason != "tool_use" or not uses:
            text = _text_of(response.content)
            if response.stop_reason == "max_tokens":
                text += "\n\n(I ran out of room there. Ask me to continue.)"
            return _Run(text or "Done.", served_by, cost)
        if any(u.name == "think_harder" for u in uses):
            return _Run("", served_by, cost, escalate=True)
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for use in uses:
            with activity.caused_by(f"{purpose} tool {use.name}"):
                try:
                    output = run_tool(
                        store, effects, use.name, dict(use.input), search_email, suggest
                    )
                    is_error = False
                except (ValueError, KeyError) as exc:
                    output, is_error = f"Error: {exc}", True
                except Exception as exc:  # noqa: BLE001 - let the model recover, not the chat fail
                    log.exception("Tool %s failed", use.name)
                    output, is_error = f"Error: the tool failed ({type(exc).__name__}).", True
                if use.name != "read_log":  # reading the log isn't news; its output is the log
                    activity.record(
                        "tool",
                        f"{use.name}{' failed' if is_error else ''}: {activity.one_line(output)}",
                        text=output,
                        detail={"input": dict(use.input)},
                        db=store.db,
                    )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": output,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})
    return _Run("I got a bit lost doing that. Could you ask again more simply?", model, cost)


# --- card claims ---
# On 4 Nov a reply said "I've sent a card" without calling suggest_change, the next ones
# trusted it, and a typed "Apply" got "Applied." with nothing changed. Cards are buttons
# only: chat can't apply one, and a claimed card that doesn't exist gets a warning line
# (kept in history, so later replies don't trust the claim either).

BUTTON_WORDS = {"apply", "✅ apply", "tweak", "✏️ tweak", "dismiss"}
CARD_CLAIM = re.compile(
    r"\b(sent|send|made|added|created|raised)\b[^.\n]{0,30}\bcard\b"
    r"|\bwaiting as a card\b|\bcard\b[^.\n]{0,20}\b(waiting|below|above|ready)\b"
    r"|\btap\s+(✅\s*)?apply\b",
    re.IGNORECASE,
)
APPLIED_CLAIM = re.compile(
    r"^\W*applied\b|\b(i'?ve|i have|i|has been|have been|it's|that's)\s+applied\b",
    re.IGNORECASE,
)
NOT_APPLIED = "⚠️ Nothing has changed: a card only changes things when you tap ✅ Apply on it."
NO_CARD = "⚠️ No card was actually made, so nothing has changed. Ask me again to make one."


def button_word_reply(text: str, waiting: list[suggestions.Change]) -> str | None:
    """A typed "apply" or "dismiss": point at the card's buttons instead of guessing."""
    if text.strip().strip(".!").casefold() not in BUTTON_WORDS:
        return None
    if not waiting:
        return "No card is waiting, so there's nothing to apply. Tell me what to change."
    return f"Tap the button on the card itself: 💡 {waiting[-1].summary}"


def check_card_claims(answer: str, made_card: bool, waiting: bool) -> str:
    """Add a warning when the reply claims a card or an Apply that didn't happen."""
    if APPLIED_CLAIM.search(answer):
        warning = NOT_APPLIED if waiting or made_card else f"{NOT_APPLIED}\n{NO_CARD}"
    elif CARD_CLAIM.search(answer) and not (made_card or waiting):
        warning = NO_CARD
    else:
        return answer
    log.warning("Chat reply claimed a card that wasn't there: %r", answer[:200])
    return f"{answer}\n\n{warning}"


async def chat(
    settings: Settings,
    state: State,
    guard: CostGuard,
    make_llm: Callable[[], AbstractAsyncContextManager[Any]],
    text: str,
    space: Space,
    *,
    now: datetime | None = None,
    session_id: int | None = None,
) -> ChatReply:
    """`session_id`: the open 🧠 Reflect session this message belongs to, if any."""
    now = now or datetime.now(settings.zone)
    store = ItemStore(state)
    history = History(state)
    emails = EmailStore(state) if settings.mail_enabled else None

    def search_email(args: dict[str, Any]) -> str:
        assert emails is not None
        return email_report.search_emails(
            emails,
            now=now,
            zone=settings.zone,
            query=args.get("query"),
            days_back=args.get("days_back"),
            category=args.get("category"),
            needs_reply=args.get("needs_reply"),
        )

    key = Space.REFLECT.value if space is Space.REFLECT else Space.CHAT.value

    shortcut = button_word_reply(text, suggestions.waiting(state, now))
    if shortcut is not None:  # no Claude call
        history.add(key, "user", text, session_id)
        history.add(key, "assistant", shortcut, session_id)
        return ChatReply(text=shortcut, model="")

    force_smart = text.lstrip().startswith("!")
    text = text.lstrip().removeprefix("!").strip() or text
    within_budget = guard.allow(essential=False)
    reflect_on = space is Space.REFLECT
    smart = (reflect_on or force_smart) and within_budget
    note = None
    if (reflect_on or force_smart) and not within_budget:
        note = "This month's budget is used up, so I answered with the quicker model."

    stable = prompts.REFLECT if reflect_on else prompts.CHAT
    if not smart and within_budget:
        stable += prompts.ESCALATE
    system = [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        {
            "type": "text",
            "text": build_context(
                store,
                history,
                key,
                now,
                emails,
                focus.focus_lines(state, store),
                with_summary=session_id is None,
                history_lines=(
                    reflect.coach_context(state, now.date(), settings.zone) if reflect_on else []
                )
                + suggestions.context_lines(state, now),
                health=None if reflect_on else health_lines(state, now),
                plan_lines=None if reflect_on else schedule_lines(state, now),
            ),
        },
    ]
    past = history.recent(key, SESSION_HISTORY, session_id) if session_id else history.recent(key)
    messages = past + [{"role": "user", "content": text}]
    effects = ToolEffects()
    purpose = "reflect" if reflect_on else "chat"

    def suggest(args: dict[str, Any]) -> str:
        if effects.suggested:
            raise ValueError("One suggestion per reply; it's already sent.")
        proposal = suggestions.Proposal.model_validate(args)
        change = suggestions.propose(state, proposal, purpose, now)
        effects.suggested.append(change.id)
        return (
            f"Card sent: {change.summary}. Nothing changes until they tap Apply, so don't say "
            "it's done; mention the card in a few words at most."
        )

    async with make_llm() as llm:
        if smart:
            run = await _run(
                llm,
                guard,
                store,
                effects,
                model=settings.model_smart,
                smart=True,
                system=system,
                messages=messages,
                tools=TOOLS,
                effort="medium",
                purpose=purpose,
                search_email=search_email if emails else None,
                suggest=suggest,
            )
            escalated, total = False, run.cost
        else:
            tools = TOOLS + ([THINK_HARDER] if within_budget else [])
            run = await _run(
                llm,
                guard,
                store,
                effects,
                model=settings.model_fast,
                smart=False,
                system=system,
                messages=messages,
                tools=tools,
                effort="low",
                purpose=purpose,
                search_email=search_email if emails else None,
                suggest=suggest,
            )
            escalated, total = run.escalate, run.cost
            if run.escalate:
                system[0] = {
                    "type": "text",
                    "text": stable.replace(prompts.ESCALATE, ""),
                    "cache_control": {"type": "ephemeral"},
                }
                run = await _run(
                    llm,
                    guard,
                    store,
                    effects,
                    model=settings.model_smart,
                    smart=True,
                    system=system,
                    messages=messages,
                    tools=TOOLS,
                    effort="medium",
                    purpose=purpose,
                    search_email=search_email if emails else None,
                    suggest=suggest,
                )
                total += run.cost

        answer, offer = reflect.take_offer(run.text)
        offer = offer and not reflect_on
        answer = check_card_claims(
            answer, bool(effects.suggested), bool(suggestions.waiting(state, now))
        )
        history.add(key, "user", text, session_id)
        history.add(key, "assistant", answer, session_id)
        if session_id is None:
            await _fold_history(llm, guard, history, key, settings.model_fast)

    return ChatReply(
        text=answer,
        model=run.model,
        cost_usd=total,
        escalated=escalated,
        effects=effects,
        note=note,
        offer_reflect=offer,
    )


async def _fold_history(
    llm: Any, guard: CostGuard, history: History, space: str, model: str
) -> None:
    """Fold old messages into the running summary (one cheap call every ~10 messages)."""
    old = history.to_fold(space)
    if not old:
        return
    transcript = "\n".join(f"{role}: {text}" for _, role, text in old)
    previous = history.summary(space)
    content = (
        f"Earlier summary:\n{previous}\n\n" if previous else ""
    ) + f"<conversation>\n{transcript}\n</conversation>"
    try:
        response = await llm.messages.create(
            model=model,
            max_tokens=600,
            output_config={"effort": "low"},
            system=prompts.SUMMARISE,
            messages=[{"role": "user", "content": content}],
        )
        await guard.record(model=model, purpose="chat_summary", usage=response.usage)
        summary = _text_of(response.content)
    except Exception:  # noqa: BLE001 - a failed summary must never lose the reply
        log.exception("Couldn't fold chat history; will retry next time")
        return
    if summary:
        history.fold(space, [i for i, _, _ in old], summary)
