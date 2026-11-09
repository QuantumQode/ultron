"""Turn core reports into Telegram message text (plain text, no markup to escape)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from ultron.features.email_report import ICONS, InboxDigest, who
from ultron.notify import Space
from ultron.ops.status import StatusReport, UsageReport

HELP = """\
Ultron — your personal assistant.

/status — integrations, job health and this chat's ids
/usage — Claude API spend this month
/sync — check email, sort new notes and update the Board now (also runs every hour)
/inbox — summary of the last 24 hours of email
/schedule — set up or redo your fixed schedule (work, workouts, sleep, commitments)
/focus — show or change your focus mode (e.g. /focus health 2w)
/plan — draft a two-day plan (before 14:00 today + tomorrow; /plan today … for one day)
/replan — reshuffle the rest of today (e.g. /replan meeting overran, lost an hour)
/review — run today's evening review now (it starts by itself at 21:00)
/brief — this morning's brief, now (it arrives by itself at 07:00)
/weekly — this week's review, now (it comes by itself on Sunday at 19:00)
/easy — no call-outs or check-ins for the rest of today
/reflect — talk through your thoughts and life with a coach; /done when finished
/help — this message

Just write to chat with me. I can look things up, tick off or add to-dos, and remember \
things about you. Start a message with ! for a more thoughtful answer. After /reflect I'm a \
thinking partner instead, until /done or 30 minutes of quiet. Your notes still go in your \
Notion Inbox."""

SPACE_LABELS = {Space.CHAT: "💬 Chat", Space.REFLECT: "🧠 Reflect", Space.BRIEFS: "📋 Briefs"}


def _when(moment: datetime | None, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%d %b %H:%M") if moment else "never"


def format_status(
    report: StatusReport,
    zone: ZoneInfo,
    *,
    chat_id: int,
    thread_id: int | None,
    space: Space | None,
) -> str:
    lines = ["Integrations"]
    for item in report.integrations:
        mark = "✅" if item.configured else f"⚪ not set up (phase {item.phase})"
        lines.append(f"  {item.name}: {mark}")

    lines += ["", "Jobs"]
    if not report.jobs:
        lines.append("  none have run yet")
    for job in report.jobs:
        health = "✅" if job.consecutive_failures == 0 else f"❌ ×{job.consecutive_failures}"
        lines.append(f"  {job.job}: {health} · last ok {_when(job.last_success, zone)}")
        if job.consecutive_failures and job.last_error:
            lines.append(f"    {job.last_error}")

    lines += [
        "",
        f"API spend: ${report.spent_usd:.2f} of ${report.cap_usd:.2f} this month",
        "",
        "This chat (for .env)",
        f"  chat id: {chat_id}",
        f"  topic id: {thread_id if thread_id is not None else '— (General / no topic)'}",
        f"  treated as: {SPACE_LABELS[space] if space else 'ignored'}",
    ]
    return "\n".join(lines)


def format_usage(report: UsageReport) -> str:
    lines = [
        f"Claude spend for {report.month:%B %Y}",
        f"${report.spent_usd:.2f} of ${report.cap_usd:.2f} ({report.fraction:.0%})",
    ]
    if report.by_purpose:
        lines.append("")
        lines += [f"  {purpose}: ${cost:.2f}" for purpose, cost in report.by_purpose.items()]
    return "\n".join(lines)


def format_inbox(digest: InboxDigest, zone: ZoneInfo) -> str:
    if digest.empty:
        return f"No new email in the last {digest.hours} hours."
    lines = [f"Email, last {digest.hours} hours"]
    if digest.important:
        lines += ["", "Important"]
        for e in digest.important:
            reply = " (waiting for your reply)" if e.needs_reply else ""
            lines.append(
                f"  {ICONS.get(e.reason or '', '📧')} {who(e)}: {e.summary or e.subject}{reply}"
            )
    if digest.fyi:
        lines += ["", "FYI"]
        lines += [f"  {who(e)}: {e.summary or e.subject}" for e in digest.fyi]
    rest = []
    if digest.promos:
        rest.append(f"{digest.promos} promotion{'s' if digest.promos != 1 else ''}")
    if digest.ignored:
        rest.append(f"{digest.ignored} automated")
    if rest:
        lines += ["", "…and " + ", ".join(rest) + " (not shown)."]
    return "\n".join(lines)
