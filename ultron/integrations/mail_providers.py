"""Email providers Ultron can read over IMAP with an app password.

One place for each provider's server and the steps to get an app password, so settings,
error messages, `ultron.check` and the setup docs all say the same thing.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str  # EMAIL_PROVIDER value
    name: str  # shown to the owner
    host: str | None  # IMAP server (port 993, SSL); None: EMAIL_IMAP_HOST says
    app_password: str  # how to get an app password, one line
    domains: tuple[str, ...] = ()  # addresses that imply this provider
    supported: bool = True


PROVIDERS = {
    p.key: p
    for p in (
        Provider(
            "gmail",
            "Gmail",
            "imap.gmail.com",
            "Turn on 2-Step Verification (myaccount.google.com/security), then create an app "
            "password at myaccount.google.com/apppasswords",
            ("gmail.com", "googlemail.com"),
        ),
        Provider(
            "yahoo",
            "Yahoo Mail",
            "imap.mail.yahoo.com",
            "Yahoo Account → Account Security → Generate app password",
            ("yahoo.com", "yahoo.co.uk", "ymail.com", "rocketmail.com"),
        ),
        Provider(
            "icloud",
            "iCloud Mail",
            "imap.mail.me.com",
            "appleid.apple.com → Sign-In and Security → App-Specific Passwords",
            ("icloud.com", "me.com", "mac.com"),
        ),
        Provider(
            "outlook",
            "Outlook",
            None,
            "Microsoft no longer allows app passwords for IMAP on Outlook/Hotmail accounts, "
            "so Ultron can't read it yet",
            ("outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "live.co.uk", "msn.com"),
            supported=False,
        ),
        Provider(
            "other",
            "your email",
            None,
            "Use the app password your provider gives for mail apps, and set EMAIL_IMAP_HOST "
            "to its IMAP server (e.g. imap.example.com)",
        ),
    )
}


def guess(address: str) -> str:
    """The provider an address implies ('me@gmail.com' -> 'gmail'), else 'other'."""
    domain = address.strip().lower().rpartition("@")[2]
    if domain.startswith("yahoo."):  # yahoo.fr, yahoo.de, … : one per country
        return "yahoo"
    return next((p.key for p in PROVIDERS.values() if domain in p.domains), "other")
