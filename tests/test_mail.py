from datetime import date

import pytest
from conftest import make_settings
from fake_mail import FakeMailBox, inbox, raw_email

from ultron.integrations import mail
from ultron.integrations.mail import Cursor, clean_text, fetch_new, html_to_text, is_blocked

TODAY = date(2026, 10, 20)


def settings(**kw):
    return make_settings(yahoo_user="me@yahoo.co.uk", yahoo_app_password="app", **kw)


def use(monkeypatch, box):
    monkeypatch.setattr(mail, "MailBox", box)
    return box


def test_first_run_backfills_30_days_read_only(monkeypatch):
    box = use(
        monkeypatch,
        FakeMailBox(
            inbox(
                raw_email(
                    "Council <tax@council.gov.uk>", "Council tax due", text="Pay £120 by 1 Nov."
                ),
                raw_email("Mum <mum@example.com>", "Sunday?", text="Lunch on Sunday?"),
            ),
            uidvalidity=7,
        ),
    )
    batch = fetch_new(settings(), None, today=TODAY)
    assert box.searches == ["SINCE 20-Sep-2026"]
    assert [e.subject for e in batch.emails] == ["Council tax due", "Sunday?"]
    first = batch.emails[0]
    assert first.sender == "tax@council.gov.uk" and first.sender_name == "Council"
    assert first.text == "Pay £120 by 1 Nov." and first.sent_at.tzinfo is not None
    assert first.message_id.endswith("@example.com>")
    assert batch.cursor == Cursor(7, 2) and not batch.more


def test_resumes_after_the_cursor_and_ignores_the_star_quirk(monkeypatch):
    box = use(
        monkeypatch,
        FakeMailBox(inbox(*(raw_email("a@x.com", f"m{n}", text="hi") for n in range(3)))),
    )
    batch = fetch_new(settings(), Cursor(1, 3), today=TODAY)
    # "UID 4:*" still returns message 3 on real servers; it must not be read again
    assert box.searches == ["UID 4:*"] and batch.emails == []
    assert batch.cursor == Cursor(1, 3) and box.fetched_bodies == []


def test_new_uidvalidity_starts_over_with_a_backfill(monkeypatch):
    box = use(
        monkeypatch, FakeMailBox(inbox(raw_email("a@x.com", "hello", text="hi")), uidvalidity=9)
    )
    batch = fetch_new(settings(), Cursor(1, 50), today=TODAY)
    assert box.searches[0].startswith("SINCE") and len(batch.emails) == 1
    assert batch.cursor == Cursor(9, 1)


def test_empty_backfill_moves_cursor_to_the_newest_uid(monkeypatch):
    use(monkeypatch, FakeMailBox(inbox(raw_email("a@x.com", "old", text="hi")), since_uids=[]))
    batch = fetch_new(settings(), None, today=TODAY)
    assert batch.emails == [] and batch.cursor == Cursor(1, 1)


def test_blocklisted_bodies_are_never_downloaded(monkeypatch):
    box = use(
        monkeypatch,
        FakeMailBox(
            inbox(
                raw_email("Bank <alerts@secure.mybank.co.uk>", "Statement", text="Balance £5"),
                raw_email("doctor@clinic.com", "Results", text="Private"),
                raw_email("friend@clinic.com", "Pub?", text="Pint later?"),
            )
        ),
    )
    batch = fetch_new(
        settings(email_blocklist="mybank.co.uk, doctor@clinic.com"), None, today=TODAY
    )
    assert [e.subject for e in batch.emails] == ["Pub?"]
    assert batch.blocked == 2 and box.fetched_bodies == [3]
    assert batch.cursor.last_uid == 3  # blocked mail still counts as seen


def test_limit_leaves_the_rest_for_next_time(monkeypatch):
    use(
        monkeypatch,
        FakeMailBox(inbox(*(raw_email("a@x.com", f"m{n}", text="hi") for n in range(5)))),
    )
    batch = fetch_new(settings(), None, today=TODAY, limit=2)
    assert [e.uid for e in batch.emails] == [1, 2] and batch.more
    assert batch.cursor.last_uid == 2


def test_huge_email_is_triaged_on_subject_only(monkeypatch):
    raw = raw_email("a@x.com", "Your invoice", text="see attached")
    box = use(monkeypatch, FakeMailBox({1: (raw, 9_000_000)}))
    [email] = fetch_new(settings(), None, today=TODAY).emails
    assert email.subject == "Your invoice" and "subject only" in email.text
    assert box.fetched_bodies == []


def test_html_only_email_becomes_text(monkeypatch):
    html = (
        "<html><head><style>p{color:red}</style></head><body><p>Your parcel arrives "
        '<b>Thursday</b>.</p><a href="https://track.example.com/abc?x=1">Track it</a>'
        "<script>alert(1)</script></body></html>"
    )
    use(monkeypatch, FakeMailBox(inbox(raw_email("shop@x.com", "Delivery", html=html))))
    [email] = fetch_new(settings(), None, today=TODAY).emails
    assert email.text == "Your parcel arrives Thursday.\nTrack it"


def test_login_failure_is_readable(monkeypatch):
    from imap_tools.errors import MailboxLoginError

    class Refuses(FakeMailBox):
        def login(self, *a, **k):
            raise MailboxLoginError(("NO", [b"bad"]), "OK")

    use(monkeypatch, Refuses({}))
    with pytest.raises(mail.MailError, match="app password"):
        fetch_new(settings(), None, today=TODAY)


@pytest.mark.parametrize(
    ("address", "blocked"),
    [
        ("alerts@mybank.co.uk", True),
        ("x@mail.mybank.co.uk", True),
        ("x@notmybank.co.uk", False),
        ("Doctor@Clinic.com", True),
        ("nurse@clinic.com", False),
        ("", False),
    ],
)
def test_is_blocked(address, blocked):
    assert is_blocked(address, ["mybank.co.uk", "doctor@clinic.com", "@", " "]) is blocked


def test_clean_text_drops_quotes_padding_and_trims():
    body = (
        "Hi,\r\n\r\n\r\n\r\nCan you   send it‌͏ by Friday? Thanks\n"
        "> old quoted line\n"
        "Sam\n"
        "On Mon, 19 Oct 2026 at 09:00, Me <me@yahoo.co.uk> wrote:\n"
        "earlier conversation\n"
    )
    assert clean_text(body) == "Hi,\n\nCan you send it by Friday? Thanks\nSam"
    long = clean_text("word " * 1000, limit=50)
    assert len(long) <= 50 + len(" …[trimmed]") and long.endswith("…[trimmed]")


def test_html_to_text_decodes_entities():
    assert html_to_text("<p>Fish &amp; chips</p>").strip() == "Fish & chips"


def test_imap_date_is_english_whatever_the_locale():
    assert mail.imap_date(date(2026, 3, 5)) == "05-Mar-2026"


def test_thread_headers_and_folded_subjects_are_read(monkeypatch):
    raw = raw_email(
        "Priya <b@x.com>",
        "RE: URGENT: Water leak into electrics and burning smell at Flat 3 Oak Court - "
        "immediate action required",
        text="any update?",
        in_reply_to="<root@x.com>",
    )
    assert b"\n " in raw.split(b"\n\n")[0]  # the Subject header really is folded
    use(monkeypatch, FakeMailBox(inbox(raw)))
    [email] = fetch_new(settings(), None, today=TODAY).emails
    assert email.thread == ("<root@x.com>",)
    assert email.subject.endswith("Flat 3 Oak Court - immediate action required")
    assert "\n" not in email.subject and "  " not in email.subject
