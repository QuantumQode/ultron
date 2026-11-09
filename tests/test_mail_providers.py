"""Email by provider: Gmail, Yahoo, iCloud or any IMAP server, chosen at setup."""

import pytest
from conftest import make_settings
from imap_tools.errors import MailboxLoginError

from ultron import check
from ultron.integrations import mail
from ultron.integrations.mail import MailError
from ultron.integrations.mail_providers import PROVIDERS, guess


@pytest.mark.parametrize(
    ("address", "provider"),
    [
        ("me@gmail.com", "gmail"),
        ("Me@GoogleMail.com", "gmail"),
        ("me@yahoo.co.uk", "yahoo"),
        ("me@yahoo.fr", "yahoo"),
        ("me@icloud.com", "icloud"),
        ("me@hotmail.co.uk", "outlook"),
        ("me@fastmail.com", "other"),
    ],
)
def test_the_address_says_which_provider(address, provider):
    assert guess(address) == provider


def test_old_yahoo_settings_still_work():
    s = make_settings(yahoo_user="me@yahoo.co.uk", yahoo_app_password="app")
    assert (s.email_provider, s.email_address, s.imap_host) == (
        "yahoo",
        "me@yahoo.co.uk",
        "imap.mail.yahoo.com",
    )
    assert s.mail_enabled and s.email_app_password.get_secret_value() == "app"


@pytest.mark.parametrize(
    ("fields", "host", "enabled"),
    [
        (dict(email_address="me@gmail.com"), "imap.gmail.com", True),
        (dict(email_address="me@icloud.com"), "imap.mail.me.com", True),
        (dict(email_address="me@work.com", email_provider="gmail"), "imap.gmail.com", True),
        (dict(email_address="me@fastmail.com"), None, False),  # other: needs a host
        (
            dict(email_address="me@fastmail.com", email_imap_host="imap.fastmail.com"),
            "imap.fastmail.com",
            True,
        ),
        (dict(email_address="me@outlook.com"), None, False),  # not supported yet
    ],
)
def test_provider_settings(fields, host, enabled):
    s = make_settings(email_app_password="app", **fields)
    assert s.imap_host == host and s.mail_enabled is enabled and s.mail_configured


def test_an_unknown_provider_is_refused():
    with pytest.raises(ValueError, match="must be one of"):
        make_settings(email_provider="aol")


def test_no_email_means_email_is_off():
    s = make_settings()
    assert not s.mail_configured and not s.mail_enabled


class FakeFolders:
    def status(self, folder, items):
        return {"MESSAGES": 4, "UNSEEN": 1}

    def list(self):
        from types import SimpleNamespace

        return [
            SimpleNamespace(name="INBOX", flags=()),
            SimpleNamespace(name="[Gmail]/Sent Mail", flags=("\\HasNoChildren", "\\Sent")),
        ]


def fake_mailbox(monkeypatch, *, reject=False):
    calls = {}

    class FakeMailBox:
        def __init__(self, host, timeout):
            calls["host"] = host

        def login(self, user, password, initial_folder):
            if reject:
                raise MailboxLoginError(("NO", [b"bad"]), "OK")
            calls["user"] = user
            self.folder = FakeFolders()
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mail, "MailBox", FakeMailBox)
    return calls


def test_gmail_logs_in_and_finds_its_sent_folder(monkeypatch):
    calls = fake_mailbox(monkeypatch)
    s = make_settings(email_address="me@gmail.com", email_app_password="app")
    assert mail.check_login(s).endswith("Sent folder: [Gmail]/Sent Mail")
    assert calls == {"host": "imap.gmail.com", "user": "me@gmail.com"}


def test_a_rejected_login_says_how_to_fix_it_for_that_provider(monkeypatch):
    fake_mailbox(monkeypatch, reject=True)
    s = make_settings(email_address="me@gmail.com", email_app_password="wrong")
    with pytest.raises(MailError, match="Gmail rejected the login.*2-Step Verification"):
        mail.check_login(s)


def test_outlook_says_plainly_it_isnt_supported(monkeypatch):
    calls = fake_mailbox(monkeypatch)
    s = make_settings(email_address="me@outlook.com", email_app_password="app")
    with pytest.raises(MailError, match="no longer allows app passwords"):
        mail.check_login(s)
    assert calls == {}  # never even connected


async def test_check_shows_an_unusable_email_as_failed_not_skipped(monkeypatch):
    fake_mailbox(monkeypatch)
    s = make_settings(email_address="me@outlook.com", email_app_password="app")
    [email] = [c for c in check.checks_for(s) if c.name == "Email"]
    result = await check.run_check(email, s)
    assert result.outcome is check.Outcome.FAIL and "Outlook" in result.detail


def test_every_provider_says_how_to_get_an_app_password():
    assert all(p.app_password and p.name for p in PROVIDERS.values())
