"""Email over IMAP (Gmail, Yahoo, iCloud or any server with an app password). Read-only
by design.

- The inbox is only ever opened with EXAMINE (read-only), and every fetch uses BODY.PEEK,
  so nothing changes on the server: mail stays unread, nothing is moved or deleted.
- Blocklisted senders are spotted from headers alone; their bodies are never downloaded.
- Only new mail is read: a cursor (UIDVALIDITY + last UID seen) says where to resume.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser

from imap_tools import MailBox, MailMessage
from imap_tools.errors import MailboxLoginError

from ultron.config import Settings
from ultron.errors import UserFacingError

TIMEOUT_SECONDS = 20
FOLDER = "INBOX"
BACKFILL_DAYS = 30  # how far back the very first read goes
MAX_PER_RUN = 100  # emails per sync; the rest wait for the next one
MAX_CHARS = 3000  # text kept per email, enough to triage
MAX_BYTES = 5_000_000  # bigger emails (large attachments) are triaged on the subject only
BULK = 20  # emails per IMAP FETCH command


class MailError(UserFacingError):
    pass


@dataclass(frozen=True)
class Email:
    uid: int
    message_id: str
    sender: str  # address, lower case
    sender_name: str
    subject: str
    sent_at: datetime  # tz-aware
    text: str  # plain text, cleaned and trimmed; untrusted
    thread: tuple[str, ...] = ()  # Message-IDs this replies to (In-Reply-To, References)


@dataclass(frozen=True)
class Cursor:
    """Where to resume. A new UIDVALIDITY means the server renumbered the mailbox."""

    uidvalidity: int
    last_uid: int


@dataclass
class MailBatch:
    emails: list[Email]
    cursor: Cursor  # save this once the emails are handled
    blocked: int = 0  # skipped by the blocklist, bodies never fetched
    more: bool = False  # more new mail is waiting for the next run


# --- connecting ---


@contextmanager
def _login(settings: Settings) -> Iterator[MailBox]:
    assert settings.email_address and settings.email_app_password
    provider = settings.mail_provider
    if not provider.supported:
        raise MailError(f"{provider.app_password}. Remove EMAIL_ADDRESS to switch email off.")
    if not settings.imap_host:
        raise MailError(
            f"Set EMAIL_IMAP_HOST to your email's IMAP server. {provider.app_password}."
        )
    try:
        with MailBox(settings.imap_host, timeout=TIMEOUT_SECONDS).login(
            settings.email_address,
            settings.email_app_password.get_secret_value(),
            initial_folder=None,  # don't SELECT anything
        ) as mailbox:
            yield mailbox
    except MailboxLoginError as exc:
        raise MailError(
            f"{provider.name} rejected the login. Check EMAIL_ADDRESS, and that "
            f"EMAIL_APP_PASSWORD is an app password, not your normal one: "
            f"{provider.app_password}."
        ) from exc
    except OSError as exc:  # timeouts, DNS, dropped connections
        raise MailError(
            f"Couldn't reach {provider.name} ({exc}). I'll try again next sync."
        ) from exc


def check_login(settings: Settings) -> str:
    """Log in and read INBOX counts with IMAP STATUS, which doesn't open the mailbox."""
    with _login(settings) as mailbox:
        status = mailbox.folder.status(FOLDER, ["MESSAGES", "UNSEEN"])
        sent = find_sent_folder(mailbox)  # LIST doesn't open anything either
    found = f"Sent folder: {sent}" if sent else "no Sent folder found"
    return f"INBOX has {status['MESSAGES']} messages, {status['UNSEEN']} unread; {found}"


# --- blocklist ---


def is_blocked(address: str, blocklist: list[str]) -> bool:
    """Entries are full addresses ("a@b.com") or domains ("bank.co.uk", which also covers
    subdomains like "mail.bank.co.uk")."""
    address = address.strip().lower()
    domain = address.rpartition("@")[2]
    for entry in blocklist:
        entry = entry.strip().lower().lstrip("@")
        if not entry:
            continue
        if "@" in entry:
            if address == entry:
                return True
        elif domain == entry or domain.endswith("." + entry):
            return True
    return False


# --- text ---


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

    def __init__(self) -> None:
        super().__init__()  # convert_charrefs=True decodes &amp; etc.
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text only. Links keep their words, not their (tracking) URLs."""
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


# Soft hyphen, grapheme joiner, zero-width spaces/joiners, word joiners, byte-order mark
INVISIBLE = re.compile("[\u00ad\u034f\u200b-\u200f\u2060-\u2064\ufeff]")
REPLY_START = re.compile(r"^(On .{5,200} wrote:|-{2,} ?Original Message ?-{2,})$")


def clean_text(text: str, limit: int = MAX_CHARS) -> str:
    """Tidy an email body for triage: drop quoted replies and invisible padding,
    collapse whitespace, and trim to `limit` characters."""
    text = INVISIBLE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = text.replace("\u00a0", " ")  # non-breaking space
    lines: list[str] = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if REPLY_START.match(line):
            break  # the rest is the quoted conversation
        if line.startswith(">"):
            continue
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …[trimmed]"
    return text


def body_text(message: MailMessage) -> str:
    plain = message.text.strip()
    return clean_text(plain if plain else html_to_text(message.html))


# --- fetching ---

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(day: date) -> str:
    """IMAP dates are always English ("20-Oct-2026"), whatever the PC's locale."""
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


def _sent_at(message: MailMessage) -> datetime:
    sent = message.date
    if sent.tzinfo is None:  # missing or broken Date header
        sent = sent.replace(tzinfo=UTC)
    return sent


def _to_email(message: MailMessage, text: str) -> Email:
    sender = message.from_values
    return Email(
        uid=int(message.uid),
        message_id=(message.headers.get("message-id") or ("",))[0].strip(),
        sender=(sender.email if sender else "").lower(),
        sender_name=sender.name if sender else "",
        subject=" ".join(message.subject.split()) or "(no subject)",
        sent_at=_sent_at(message),
        text=text,
        thread=_message_ids(
            message.headers.get("in-reply-to", ()) + message.headers.get("references", ())
        ),
    )


def _new_uids(
    mailbox: MailBox,
    folder: str,
    cursor: Cursor | None,
    *,
    today: date,
    backfill_days: int,
    limit: int,
) -> tuple[list[int], Cursor, bool]:
    """Open `folder` read-only and list UIDs not seen yet, oldest first.

    Returns (uids to read now, cursor if there's nothing to read, more waiting).
    """
    status = mailbox.folder.status(folder, ["UIDVALIDITY", "UIDNEXT"])
    uidvalidity, uidnext = int(status["UIDVALIDITY"]), int(status["UIDNEXT"])
    mailbox.folder.set(folder, readonly=True)  # EXAMINE, never SELECT

    resume = cursor is not None and cursor.uidvalidity == uidvalidity
    if resume:
        criteria = f"UID {cursor.last_uid + 1}:*"
        floor = cursor.last_uid
    else:
        criteria = f"SINCE {imap_date(today - timedelta(days=backfill_days))}"
        floor = 0
    # "n:*" always matches the newest message, even when it's older than n
    uids = sorted(u for u in map(int, mailbox.uids(criteria)) if u > floor)
    empty = Cursor(uidvalidity, cursor.last_uid if resume else uidnext - 1)
    return uids[:limit], empty, len(uids) > limit


def fetch_new(
    settings: Settings,
    cursor: Cursor | None,
    *,
    today: date,
    backfill_days: int = BACKFILL_DAYS,
    limit: int = MAX_PER_RUN,
) -> MailBatch:
    """Read new INBOX mail, oldest first, without changing anything on the server.

    With no cursor (or after the server renumbered the mailbox) it reads the last
    `backfill_days` days. Blocking: call it with `asyncio.to_thread` from async code.
    """
    with _login(settings) as mailbox:
        batch, empty, more = _new_uids(
            mailbox, FOLDER, cursor, today=today, backfill_days=backfill_days, limit=limit
        )
        if not batch:
            return MailBatch([], empty)
        headers = list(
            mailbox.fetch(
                uid_list=[str(u) for u in batch], headers_only=True, mark_seen=False, bulk=BULK
            )
        )
        allowed = [m for m in headers if not is_blocked(m.from_, settings.email_blocklist)]
        small = [str(m.uid) for m in allowed if m.size_rfc822 <= MAX_BYTES]
        bodies = {
            int(m.uid): body_text(m)
            for m in (mailbox.fetch(uid_list=small, mark_seen=False, bulk=BULK) if small else ())
        }

    emails = [
        _to_email(m, bodies.get(int(m.uid), "(Too large to read; triage on the subject only.)"))
        for m in allowed
    ]
    emails.sort(key=lambda e: e.uid)
    return MailBatch(
        emails,
        Cursor(empty.uidvalidity, batch[-1]),
        blocked=len(headers) - len(allowed),
        more=more,
    )


# --- Sent folder: headers only, to know what the owner has replied to ---

SENT_NAMES = ("Sent", "Sent Items", "Sent Messages")
MAX_SENT_PER_RUN = 300  # headers are tiny


@dataclass(frozen=True)
class SentMail:
    to: tuple[str, ...]  # recipient addresses, lower case
    replying_to: tuple[str, ...]  # Message-IDs from In-Reply-To and References
    sent_at: datetime


@dataclass
class SentBatch:
    sent: list[SentMail]
    cursor: Cursor
    folder: str | None  # None if no Sent folder was found


def find_sent_folder(mailbox: MailBox) -> str | None:
    folders = list(mailbox.folder.list())
    for f in folders:
        if any(flag.lower() == "\\sent" for flag in f.flags):
            return f.name
    names = {f.name.lower(): f.name for f in folders}
    for name in SENT_NAMES:
        if name.lower() in names:
            return names[name.lower()]
    return None


def _message_ids(header_values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(re.findall(r"<[^<>\s]+>", " ".join(header_values)))


def fetch_sent(
    settings: Settings,
    cursor: Cursor | None,
    *,
    today: date,
    backfill_days: int = BACKFILL_DAYS,
    limit: int = MAX_SENT_PER_RUN,
) -> SentBatch:
    """Read headers (never bodies) of new mail in the Sent folder, read-only.
    Nothing from here is ever sent to Claude."""
    with _login(settings) as mailbox:
        folder = find_sent_folder(mailbox)
        if folder is None:
            return SentBatch([], cursor or Cursor(0, 0), None)
        batch, empty, _ = _new_uids(
            mailbox, folder, cursor, today=today, backfill_days=backfill_days, limit=limit
        )
        if not batch:
            return SentBatch([], empty, folder)
        headers = list(
            mailbox.fetch(
                uid_list=[str(u) for u in batch], headers_only=True, mark_seen=False, bulk=BULK
            )
        )
    sent = [
        SentMail(
            to=tuple(a.lower() for a in (*m.to, *m.cc)),
            replying_to=_message_ids(
                m.headers.get("in-reply-to", ()) + m.headers.get("references", ())
            ),
            sent_at=_sent_at(m),
        )
        for m in headers
    ]
    return SentBatch(sent, Cursor(empty.uidvalidity, batch[-1]), folder)
