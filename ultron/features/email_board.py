"""Put to-dos found in email onto the Board, without duplicates.

Matching is deliberately simple (no extra Claude call). An email item matches an open
to-do when:
- it's in the same email thread (reply/forward headers), or the subjects match once
  "Re:"/"Fwd:" are removed (one may contain the other, if long enough), or
- it's from the same sender domain and mentions the same amount (a bill and its reminder), or
- the wording is near-identical (e.g. "Pay council tax" vs "pay the council tax").

To-dos from emails more than a week old get a question instead of a stale due date:
"(?) From an email on 19 Sep (was due 21 Sep). Still needed?"

A match updates the existing to-do (fills in a missing due date or amount) instead of
adding a second one. The owner's wording always wins.

Email never ticks anything off by itself: if an email says a matched to-do is already
done, Ultron asks on the Board ("(?) Email from … says this is done. Tick it?").
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from ultron.features.items import Item, ItemStore
from ultron.features.triage import EmailItem, Triaged

SIMILAR = 0.85  # wording similarity (0–1) that counts as the same to-do
STOP_WORDS = {"the", "a", "an", "your", "my", "to", "for", "of", "on", "by", "please"}
MAX_LABEL = 30
OLD_AFTER = timedelta(days=7)  # older emails' to-dos are asked about, not trusted
MIN_SUBJECT = 20  # shorter subjects ("Hi", "Invoice") are too generic to match on
REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|tr)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


@dataclass
class EmailBoardResult:
    added: list[Item] = field(default_factory=list)
    merged: list[Item] = field(default_factory=list)  # existing to-dos updated
    asked: list[Item] = field(default_factory=list)  # "is this done?" questions added

    @property
    def changed(self) -> bool:
        return bool(self.added or self.merged or self.asked)


def sender_label(name: str, address: str) -> str:
    """What the Board shows after 📧: the sender's name, else their domain."""
    label = name.strip().strip('"') or address.rpartition("@")[2] or address
    return label if len(label) <= MAX_LABEL else label[: MAX_LABEL - 1].rstrip() + "…"


def sender_key(address: str) -> str:
    """Sender domain without common mail subdomains, so alerts@ and billing@ match."""
    domain = address.lower().rpartition("@")[2]
    return re.sub(r"^(mail|email|e|em|info|news|billing|alerts?|notifications?)\.", "", domain)


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.casefold()) if w not in STOP_WORDS]


def amount_key(amount: str | None) -> str | None:
    """'£120.00', '120', 'GBP 120.0' -> '120'. None if there's no number."""
    if not amount:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", amount)
    if not match:
        return None
    value = float(match.group().replace(",", ""))
    return f"{value:g}"


def similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if set(wa) == set(wb):
        return True
    return SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio() >= SIMILAR


def subject_key(subject: str) -> str:
    """'RE: FW: Water leak at Flat 3' -> 'water leak at flat 3'."""
    return " ".join(REPLY_PREFIX.sub("", subject).casefold().split())


def same_subject(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    short, long = sorted((a, b), key=len)
    return len(short) >= MIN_SUBJECT and short in long


def find_match(
    open_tasks: list[Item],
    found: EmailItem,
    key: str,
    *,
    thread: set[str] = frozenset(),
    subject: str | None = None,
    email_id: int | None = None,
) -> Item | None:
    amount = amount_key(found.amount)
    for task in open_tasks:
        if email_id is not None and task.email_id == email_id:
            continue  # two different to-dos from one email stay separate
        if thread & set((task.thread_ids or "").split()) or same_subject(subject, task.subject_key):
            return task
    for task in open_tasks:
        if amount and task.source_key == key and amount_key(task.amount) == amount:
            return task
    for task in open_tasks:
        if similar(task.title, found.title):
            return task
    return None


def old_email_question(sent: datetime, due: str | None, zone) -> str:
    was = f" (was due {datetime.fromisoformat(due):%d %b})" if due else ""
    return f"From an email on {sent.astimezone(zone):%d %b}{was}. Still needed?"


def add_email_items(
    store: ItemStore, saved: list[tuple[int, Triaged]], *, now: datetime
) -> EmailBoardResult:
    """Add or merge the items from newly triaged emails. Returns what changed."""
    result = EmailBoardResult()
    for email_id, triaged in saved:
        email = triaged.email
        label = sender_label(email.sender_name, email.sender)
        key = sender_key(email.sender)
        thread = {i for i in (email.message_id, *email.thread) if i}
        subject = subject_key(email.subject)
        old = now - email.sent_at > OLD_AFTER
        for found in triaged.items:
            open_tasks = [i for i in store.all() if i.kind == "task" and i.status == "open"]
            match = find_match(
                open_tasks, found, key, thread=thread, subject=subject, email_id=email_id
            )
            if found.already_done:
                if match and not match.question:
                    question = f"Email from {label} says this is done. Tick it?"
                    result.asked.append(store.update(match.id, question=question))
                continue  # nothing to do, and email never ticks things off itself
            if match:
                fields = {}
                if found.due and not match.due and not old:
                    fields["due"] = found.due
                if found.amount and not match.amount:
                    fields["amount"] = found.amount
                if match.email_id is not None:  # keep the thread linked for later replies
                    known = set((match.thread_ids or "").split())
                    if not thread <= known:
                        fields["thread_ids"] = " ".join(sorted(known | thread))
                if fields:
                    result.merged.append(store.update(match.id, **fields))
                continue
            stale = old and not (found.due and found.due >= now.date().isoformat())
            result.added.append(
                store.add(
                    kind="task",
                    title=found.title.strip(),
                    details=None,
                    project=None,
                    area=found.area,
                    due=None if stale else found.due,
                    question=old_email_question(email.sent_at, found.due, now.tzinfo)
                    if stale
                    else None,
                    amount=found.amount,
                    source=label,
                    source_key=key,
                    email_id=email_id,
                    thread_ids=" ".join(sorted(thread)) or None,
                    subject_key=subject or None,
                )
            )
    return result
