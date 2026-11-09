import asyncio

from conftest import make_settings

from ultron import check
from ultron.check import Check, Outcome, run_check
from ultron.integrations import calendar, mail, notion
from ultron.integrations.calendar import CalendarError, find_calendar


async def test_skipped_when_not_configured(settings):
    result = await run_check(Check("Notion", lambda s: "x", False, "set NOTION_TOKEN"), settings)
    assert result.outcome is Outcome.SKIP and "NOTION_TOKEN" in result.detail


async def test_sync_and_async_probes(settings):
    async def async_probe(s):
        return "async ok"

    assert (await run_check(Check("A", async_probe, True), settings)).detail == "async ok"
    assert (await run_check(Check("S", lambda s: "sync ok", True), settings)).detail == "sync ok"


async def test_friendly_errors_shown_as_is(settings):
    def probe(s):
        raise CalendarError("no calendar named 'Ultron'")

    result = await run_check(Check("iCloud", probe, True), settings)
    assert result.outcome is Outcome.FAIL and result.detail == "no calendar named 'Ultron'"


async def test_unexpected_errors_include_type(settings):
    def probe(s):
        raise OSError("connection refused")

    result = await run_check(Check("Yahoo", probe, True), settings)
    assert result.detail == "OSError: connection refused"


async def test_timeout(settings):
    async def slow(s):
        await asyncio.sleep(5)

    result = await run_check(Check("Slow", slow, True), settings, timeout=0.01)
    assert result.outcome is Outcome.FAIL and "no answer" in result.detail


def test_only_configured_services_enabled(settings):
    enabled = {c.name: c.enabled for c in check.checks_for(settings)}
    assert enabled == {
        "Telegram": True,
        "Claude": True,
        "Notion": False,
        "Email": False,
        "iCloud Calendar": False,
    }
    full = make_settings(
        notion_token="n",
        notion_root_page_id="p",
        yahoo_user="u",
        yahoo_app_password="p",
        icloud_user="i",
        icloud_app_password="p",
    )
    assert all(c.enabled for c in check.checks_for(full))


# --- calendar ---


def test_find_calendar_ignores_case_and_spaces():
    assert find_calendar(["Home", " ultron "], "Ultron") == " ultron "
    assert find_calendar(["Home", "Work"], "Ultron") is None


class FakeCal:
    def __init__(self, name):
        self.name = name

    def get_display_name(self):
        return self.name


class FakeDAV:
    calendars: list[str] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def principal(self):
        names = self.calendars
        return type("P", (), {"calendars": lambda _self: [FakeCal(n) for n in names]})()


ICLOUD = dict(icloud_user="me@icloud.com", icloud_app_password="abcd-efgh")


def test_calendar_check_finds_ultron(monkeypatch):
    FakeDAV.calendars = ["Home", "Ultron"]
    monkeypatch.setattr(calendar, "DAVClient", FakeDAV)
    assert calendar.check_login(make_settings(**ICLOUD)) == "2 calendars, including 'Ultron'"


def test_calendar_check_reports_missing_calendar(monkeypatch):
    FakeDAV.calendars = ["Home", "Work"]
    monkeypatch.setattr(calendar, "DAVClient", FakeDAV)
    try:
        calendar.check_login(make_settings(**ICLOUD))
    except CalendarError as exc:
        assert "no calendar named 'Ultron'" in str(exc) and "Home, Work" in str(exc)
    else:
        raise AssertionError("expected CalendarError")


# --- mail: must never select a mailbox ---


def test_mail_check_uses_status_without_selecting(monkeypatch):
    calls = {}

    class FakeFolders:
        def list(self):
            from types import SimpleNamespace

            return [
                SimpleNamespace(name="INBOX", flags=()),
                SimpleNamespace(name="Sent", flags=("\\Sent",)),
            ]

        def status(self, folder, options):
            calls["status"] = (folder, options)
            return {"MESSAGES": 10, "UNSEEN": 3}

    class FakeMailBox:
        def __init__(self, host, timeout):
            calls["host"] = host

        def login(self, user, password, initial_folder):
            calls["initial_folder"] = initial_folder
            self.folder = FakeFolders()
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mail, "MailBox", FakeMailBox)
    settings = make_settings(yahoo_user="me@yahoo.co.uk", yahoo_app_password="app")
    assert mail.check_login(settings) == "INBOX has 10 messages, 3 unread; Sent folder: Sent"
    assert calls["initial_folder"] is None  # nothing was SELECTed
    assert calls["host"] == "imap.mail.yahoo.com"


# --- notion ---


def test_notion_root_page_accepts_url():
    url = "https://www.notion.so/Ultron-1a2b3c4d5e6f47a8b9c0d1e2f3a4b5c6"
    page_id = notion.root_page_id(make_settings(notion_token="n", notion_root_page_id=url))
    assert page_id.replace("-", "") == "1a2b3c4d5e6f47a8b9c0d1e2f3a4b5c6"


# --- claude ---


async def test_claude_unscoped_key_gets_friendly_message(monkeypatch):
    import anthropic
    import httpx2

    from ultron.brain import llm

    request = httpx2.Request("GET", "https://api.anthropic.com/v1/models/x")
    response = httpx2.Response(400, request=request)
    error = anthropic.BadRequestError(
        "This API key is not scoped to a workspace", response=response, body=None
    )

    class FakeModels:
        async def retrieve(self, model):
            raise error

    class FakeClient:
        models = FakeModels()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(llm, "make_client", lambda s: FakeClient())
    result = await run_check(Check("Claude", llm.check_access, True), make_settings())
    assert result.outcome is Outcome.FAIL and "Settings → Workspaces" in result.detail
