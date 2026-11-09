"""A fake Yahoo mailbox that mimics the parts of imap_tools.MailBox Ultron uses, and
fails the test on anything that could change the mailbox."""

from email.message import EmailMessage
from types import SimpleNamespace

from imap_tools import MailMessage


def raw_email(
    sender,
    subject,
    *,
    text=None,
    html=None,
    to="me@yahoo.co.uk",
    date_header="Mon, 19 Oct 2026 09:00:00 +0100",
    message_id=None,
    in_reply_to=None,
):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = date_header
    message["Message-ID"] = message_id or f"<{abs(hash(subject))}@example.com>"
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if text is not None:
        message.set_content(text)
    if html is not None:
        if text is None:
            message.set_content(html, subtype="html")
        else:
            message.add_alternative(html, subtype="html")
    if text is None and html is None:
        message.set_content("")
    return message.as_bytes()


def inbox(*emails):
    return {uid: (raw, len(raw)) for uid, raw in enumerate(emails, start=1)}


class FakeFolders:
    def __init__(self, box):
        self.box = box

    def list(self):
        return [
            SimpleNamespace(name=name, flags=("\\Sent",) if name == self.box.sent_name else ())
            for name in self.box.folders
        ]

    def status(self, folder, options):
        return {
            "UIDVALIDITY": self.box.uidvalidity,
            "UIDNEXT": max(self.box.folders[folder], default=0) + 1,
            "MESSAGES": len(self.box.folders[folder]),
            "UNSEEN": 0,
        }

    def set(self, folder, readonly=False):
        assert readonly, "mailboxes must only be opened read-only (EXAMINE)"
        self.box.current = folder


class FakeMailBox:
    def __init__(self, messages, uidvalidity=1, since_uids=None, sent=None, sent_name="Sent"):
        self.folders = {"INBOX": messages}
        if sent is not None:
            self.folders[sent_name] = sent
        self.sent_name = sent_name if sent is not None else None
        self.uidvalidity = uidvalidity
        self.since_uids = since_uids  # what a SINCE search on INBOX returns (default: all)
        self.current = None
        self.searches = []
        self.fetched_bodies = []
        self.folder = FakeFolders(self)

    @property
    def messages(self):
        return self.folders[self.current]

    # MailBox(host, timeout).login(...) as a context manager
    def __call__(self, host, timeout):
        return self

    def login(self, user, password, initial_folder):
        assert initial_folder is None
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.current = None
        return False

    def uids(self, criteria):
        assert self.current, "searched without opening a folder"
        self.searches.append(criteria)
        if criteria.startswith("UID "):
            start = int(criteria.split()[1].split(":")[0])
            found = [u for u in self.messages if u >= start] or list(self.messages)[-1:]
        elif self.current == "INBOX" and self.since_uids is not None:
            found = list(self.since_uids)
        else:
            found = list(self.messages)
        return [str(u) for u in found]

    def fetch(self, *, uid_list, mark_seen, bulk, headers_only=False):
        assert mark_seen is False, "fetching must never mark mail as read"
        assert uid_list, "an empty uid_list makes imap-tools fetch everything"
        for uid in map(int, uid_list):
            raw, size = self.messages[uid]
            if headers_only:
                raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
            else:
                assert self.current == "INBOX", "never download sent mail"
                self.fetched_bodies.append(uid)
            yield MailMessage([(f"{uid} (UID {uid} RFC822.SIZE {size} FLAGS ())".encode(), raw)])
