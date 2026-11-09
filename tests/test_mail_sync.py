from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import make_settings
from fake_mail import FakeMailBox, inbox, raw_email

from ultron.features import mail_sync
from ultron.features.items import ItemStore
from ultron.features.mail_sync import KEY_INBOX_CURSOR, run_mail
from ultron.features.triage import EmailItem, EmailStore, EmailTriage, TriageError, TriageResult
from ultron.integrations import mail
from ultron.integrations.mail import SentMail, fetch_sent
from ultron.notify import Space
from ultron.ops.guard import CostGuard
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=LONDON)  # 3h after the fake emails' Date


class FakeLLM:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []
        self.messages = self

    async def parse(self, **kwargs):
        self.requests.append(kwargs)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200)
        return SimpleNamespace(
            stop_reason="end_turn", parsed_output=self.results.pop(0), usage=usage
        )


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, space=Space.BRIEFS):
        self.sent.append((text, space))


@pytest.fixture
def env():
    state = State(":memory:")

    async def notify(_):
        pass

    yield SimpleNamespace(
        state=state,
        guard=CostGuard(state, cap_usd=20, zone=LONDON, notify=notify),
        notifier=FakeNotifier(),
        settings=make_settings(yahoo_user="me@yahoo.co.uk", yahoo_app_password="app"),
    )
    state.close()


async def go(env, llm, now=NOW):
    @asynccontextmanager
    async def make_llm():
        yield llm

    return await run_mail(env.settings, env.state, env.guard, env.notifier, make_llm, now=now)


def bill():
    return EmailItem(
        kind="bill",
        title="Pay council tax",
        due="2026-11-01",
        amount="£120.00",
        area="Wealth",
        already_done=False,
    )


def triage(*entries):
    return TriageResult(
        emails=[
            EmailTriage(n=n, category=c, reason=r, summary=s, needs_reply=reply, items=items)
            for n, c, r, s, reply, items in entries
        ]
    )


MUM_ID = "<mum-1@example.com>"


def mailbox(sent=None):
    return FakeMailBox(
        inbox(
            raw_email("Council <tax@council.gov.uk>", "Council tax", text="Pay £120 by 1 Nov."),
            raw_email("Mum <mum@example.com>", "Sunday?", text="Lunch?", message_id=MUM_ID),
        ),
        sent=sent if sent is not None else {},
    )


async def test_full_mail_step(env, monkeypatch):
    monkeypatch.setattr(mail, "MailBox", mailbox())
    llm = FakeLLM(
        triage(
            (1, "important", "money", "Council tax £120 due 1 Nov", False, [bill()]),
            (2, "important", "person", "Asked about Sunday lunch", True, []),
        )
    )
    report = await go(env, llm)
    assert report.read == 2 and report.important == 2 and report.added == 1
    assert report.pinged == 2 and report.cost_usd > 0
    [todo] = [i for i in ItemStore(env.state).all() if i.kind == "task"]
    assert todo.title == "Pay council tax" and todo.source == "Council"
    [(text, _)] = env.notifier.sent
    assert "💷 Council: Council tax £120 due 1 Nov" in text
    assert env.state.get(KEY_INBOX_CURSOR) == '{"uidvalidity": 1, "last_uid": 2}'
    assert "Email: read 2 new email(s), 2 important, 1 Board update(s)." == report.summary()

    # next hour: nothing new, no Claude call, no ping
    again = await go(env, FakeLLM())
    assert again.read == 0 and again.summary() == "No new email." and len(env.notifier.sent) == 1


async def test_budget_failure_keeps_the_cursor(env, monkeypatch):
    monkeypatch.setattr(mail, "MailBox", mailbox())
    env.state.record_usage(
        model="m",
        purpose="x",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_usd=25.0,
    )
    with pytest.raises(TriageError):
        await go(env, FakeLLM())
    assert env.state.get(KEY_INBOX_CURSOR) is None  # mail will be read next time


async def test_already_stored_mail_is_not_triaged_again(env, monkeypatch):
    monkeypatch.setattr(mail, "MailBox", mailbox())
    await go(
        env, FakeLLM(triage((1, "fyi", None, "a", False, []), (2, "fyi", None, "b", False, [])))
    )
    env.state.set(KEY_INBOX_CURSOR, "")  # e.g. the server renumbered the mailbox
    llm = FakeLLM()
    report = await go(env, llm)
    assert llm.requests == [] and report.read == 0


async def test_reply_in_sent_folder_clears_waiting(env, monkeypatch):
    reply = raw_email(
        "me@yahoo.co.uk",
        "Re: Sunday?",
        to="Mum <mum@example.com>",
        date_header="Mon, 19 Oct 2026 11:00:00 +0100",
        in_reply_to=MUM_ID,
    )
    box = mailbox(sent=inbox(reply))
    monkeypatch.setattr(mail, "MailBox", box)
    report = await go(
        env,
        FakeLLM(
            triage(
                (1, "fyi", None, "Council tax", False, []),
                (2, "important", "person", "Lunch?", True, []),
            )
        ),
    )
    assert report.replied == 1
    assert not any(
        e.needs_reply for e in EmailStore(env.state).since(datetime(2026, 1, 1, tzinfo=UTC))
    )
    assert env.state.get(mail_sync.KEY_SENT_CURSOR) == '{"uidvalidity": 1, "last_uid": 1}'


def test_fetch_sent_reads_headers_only(monkeypatch):
    reply = raw_email(
        "me@yahoo.co.uk", "Re: x", to="Mum <mum@example.com>", in_reply_to="<a@x> <b@x>"
    )
    box = FakeMailBox({}, sent=inbox(reply), sent_name="Sent Items")
    monkeypatch.setattr(mail, "MailBox", box)
    settings = make_settings(yahoo_user="me@yahoo.co.uk", yahoo_app_password="app")
    batch = fetch_sent(settings, None, today=date(2026, 10, 20))
    assert batch.folder == "Sent Items" and box.fetched_bodies == []
    [sent] = batch.sent
    assert sent.to == ("mum@example.com",) and sent.replying_to == ("<a@x>", "<b@x>")


def test_no_sent_folder_is_harmless(monkeypatch):
    monkeypatch.setattr(mail, "MailBox", FakeMailBox({}))
    settings = make_settings(yahoo_user="me@yahoo.co.uk", yahoo_app_password="app")
    assert fetch_sent(settings, None, today=date(2026, 10, 20)).folder is None


def test_mark_replied_by_recipient_only_after_the_email_arrived(env):
    from ultron.features.triage import Triaged
    from ultron.integrations.mail import Email

    store = EmailStore(env.state)
    arrived = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    store.save(
        [
            Triaged(
                Email(1, "<x>", "sam@x.com", "Sam", "Hi", arrived, ""),
                "important",
                "person",
                "s",
                True,
            )
        ]
    )
    earlier = SentMail(("sam@x.com",), (), datetime(2026, 10, 18, tzinfo=UTC))
    assert store.mark_replied([earlier]) == 0  # sent before Sam's email: not a reply
    later = SentMail(("sam@x.com",), (), datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    assert store.mark_replied([later]) == 1
