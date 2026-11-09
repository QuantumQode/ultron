from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import CONFIGURED

from ultron.features.board import parse_edit, render_item
from ultron.features.email_board import (
    add_email_items as _add,
)
from ultron.features.email_board import (
    amount_key,
    same_subject,
    sender_key,
    sender_label,
    similar,
    subject_key,
)
from ultron.features.items import ItemStore
from ultron.features.triage import EmailItem, Triaged
from ultron.integrations.mail import Email
from ultron.state import State

LONDON = ZoneInfo("Europe/London")
TODAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=LONDON)


def add_email_items(store, saved, now=NOW):
    return _add(store, saved, now=now)


@pytest.fixture
def store():
    state = State(":memory:")
    yield ItemStore(state)
    state.close()


def found(title, *, due=None, amount=None, area="Wealth", done=False, kind="bill"):
    return EmailItem(kind=kind, title=title, due=due, amount=amount, area=area, already_done=done)


_ids = iter(range(1, 10_000))


def triaged(*items, sender="tax@council.gov.uk", name="Council"):
    email = Email(
        1, f"<{next(_ids)}@x>", sender, name, "subject", datetime(2026, 10, 19, tzinfo=UTC), ""
    )
    return Triaged(email, "important", "money", "summary", False, list(items))


def own_task(store, title, **kw):
    values = dict(
        kind="task", title=title, details=None, project=None, area="Admin", due=None, question=None
    )
    return store.add(**{**values, **kw})


def test_new_bill_is_added_as_a_tagged_todo(store):
    result = add_email_items(
        store, [(7, triaged(found("Pay council tax", due="2026-11-01", amount="£120.00")))]
    )
    [item] = result.added
    assert item.kind == "task" and item.area == "Wealth" and item.due == "2026-11-01"
    assert item.source == "Council" and item.source_key == "council.gov.uk"
    assert item.email_id == 7 and item.amount == "£120.00"
    block, text = render_item(item, TODAY, LONDON, routine_minutes=CONFIGURED.routine_minutes)
    assert text == "Pay council tax · £120.00 · 📧 Council · due Sun 01 Nov"
    assert block["type"] == "to_do"


def test_amount_already_in_title_is_not_repeated(store):
    [item] = add_email_items(
        store, [(1, triaged(found("Pay £120 council tax", amount="£120")))]
    ).added
    assert (
        render_item(item, TODAY, LONDON, routine_minutes=CONFIGURED.routine_minutes)[1]
        == "Pay £120 council tax · 📧 Council"
    )


def test_owner_rename_keeps_the_email_tag_out_of_the_title(store):
    [item] = add_email_items(
        store, [(1, triaged(found("Pay council tax", amount="£120.00")))]
    ).added
    _, old = render_item(item, TODAY, LONDON, routine_minutes=CONFIGURED.routine_minutes)
    new = old.replace("Pay council tax", "Pay council tax online")
    assert parse_edit(item, old, new) == ("Pay council tax online", False)


def test_reminder_from_same_sender_and_amount_merges(store):
    add_email_items(store, [(1, triaged(found("Pay council tax", amount="£120.00")))])
    result = add_email_items(
        store,
        [
            (
                2,
                triaged(
                    found("Council tax payment overdue", due="2026-11-01", amount="120"),
                    sender="reminders@mail.council.gov.uk",
                    name="Council Tax Team",
                ),
            )
        ],
    )
    assert result.added == [] and [m.due for m in result.merged] == ["2026-11-01"]
    assert len(store.all()) == 1


def test_same_wording_matches_a_task_jotted_in_notion(store):
    mine = own_task(store, "pay the council tax")
    result = add_email_items(store, [(1, triaged(found("Pay council tax", amount="£120")))])
    assert result.added == [] and result.merged[0].id == mine.id
    item = store.get(mine.id)
    assert item.title == "pay the council tax" and item.amount == "£120"  # owner's wording kept
    assert item.source is None  # still the owner's own to-do


def test_duplicate_with_nothing_new_changes_nothing(store):
    own_task(store, "Pay council tax", due="2026-11-01", amount="£120")
    result = add_email_items(store, [(1, triaged(found("Pay council tax", due="2026-11-05")))])
    assert not result.changed and store.all()[0].due == "2026-11-01"


def test_different_bills_from_the_same_sender_stay_separate(store):
    add_email_items(store, [(1, triaged(found("Pay council tax", amount="£120")))])
    add_email_items(store, [(2, triaged(found("Pay parking fine", amount="£35")))])
    assert len(store.all()) == 2


def test_done_email_asks_instead_of_ticking(store):
    add_email_items(store, [(1, triaged(found("Pay council tax", amount="£120")))])
    result = add_email_items(
        store, [(2, triaged(found("Pay council tax", amount="£120", done=True)))]
    )
    [item] = result.asked
    assert item.status == "open"
    assert item.question == "Email from Council says this is done. Tick it?"
    _, text = render_item(item, TODAY, LONDON, routine_minutes=CONFIGURED.routine_minutes)
    assert text.endswith("(?) Email from Council says this is done. Tick it?")


def test_done_email_with_no_match_adds_nothing(store):
    result = add_email_items(store, [(1, triaged(found("Pay water bill", done=True)))])
    assert not result.changed and store.all() == []


def test_done_email_keeps_an_existing_question(store):
    own_task(store, "Pay council tax", question="Still relevant?")
    result = add_email_items(store, [(1, triaged(found("Pay council tax", done=True)))])
    assert result.asked == [] and store.all()[0].question == "Still relevant?"


def test_ticked_tasks_are_not_matched(store):
    old = own_task(store, "Pay council tax")
    store.set_status(old.id, "done")
    result = add_email_items(store, [(1, triaged(found("Pay council tax")))])
    assert len(result.added) == 1


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Pay council tax", "pay the council tax", True),
        ("Pay council tax", "Council tax: pay", True),
        ("Pay council tax", "Pay water bill", False),
        ("Call mum", "Call Sam", False),
        ("", "Pay", False),
    ],
)
def test_similar(a, b, same):
    assert similar(a, b) is same


def test_helpers():
    assert amount_key("£1,200.50") == "1200.5" and amount_key("GBP 120.00") == "120"
    assert amount_key("free") is None and amount_key(None) is None
    assert sender_key("alerts@email.bank.co.uk") == "bank.co.uk"
    assert sender_label("", "noreply@shop.com") == "shop.com"
    assert sender_label('"Council"', "x@y") == "Council"
    assert len(sender_label("A" * 50, "x@y")) == 30


def threaded(
    *items,
    subject="Water leak at Flat 3 Oak Court",
    msg="<a@x>",
    thread=(),
    sender="tax@council.gov.uk",
    name="Council",
    sent=None,
):
    email = Email(
        1, msg, sender, name, subject, sent or datetime(2026, 10, 19, tzinfo=UTC), "", thread
    )
    return Triaged(email, "important", "person", "summary", False, list(items))


def test_replies_in_a_thread_update_one_todo(store):
    add_email_items(store, [(1, threaded(found("Deal with water leak", kind="task")))])
    result = add_email_items(
        store,
        [
            (
                2,
                threaded(
                    found("Reply to Priya about the electrician", kind="task"),
                    subject="Something else entirely",
                    msg="<b@x>",
                    thread=("<a@x>",),
                    sender="priya@gmail.com",
                    name="Priya",
                ),
            )
        ],
    )
    assert result.added == [] and len(store.all()) == 1
    assert store.all()[0].thread_ids == "<a@x> <b@x>"  # later replies to <b@x> match too


def test_forwards_match_by_subject(store):
    add_email_items(
        store,
        [
            (
                1,
                threaded(
                    found("Deal with leak", kind="task"),
                    subject="URGENT: Water leak at Flat 3 Oak Court",
                ),
            )
        ],
    )
    for n, subject in enumerate(
        [
            "FW: URGENT: Water leak at Flat 3 Oak Court",
            "Flat 3 - Leak from flat 6 - URGENT: Water leak at Flat 3 Oak Court",
        ],
        start=2,
    ):
        add_email_items(
            store,
            [
                (
                    n,
                    threaded(
                        found(f"Other wording {n}", kind="task"),
                        subject=subject,
                        msg=f"<{n}@x>",
                        sender=f"p{n}@x.com",
                    ),
                )
            ],
        )
    assert len(store.all()) == 1


def test_two_todos_from_one_email_stay_separate(store):
    result = add_email_items(
        store,
        [(1, threaded(found("Pay invoice", amount="£50"), found("Return the form", kind="task")))],
    )
    assert len(result.added) == 2


def test_short_generic_subjects_dont_match(store):
    add_email_items(store, [(1, threaded(found("Pay invoice", amount="£50"), subject="Invoice"))])
    add_email_items(
        store,
        [
            (
                2,
                threaded(
                    found("Renew passport", kind="task"),
                    subject="Re: Invoice",
                    msg="<z@x>",
                    sender="x@gov.uk",
                ),
            )
        ],
    )
    assert len(store.all()) == 2


def test_old_email_todos_get_a_question_not_a_stale_date(store):
    sent = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
    [item] = add_email_items(
        store, [(1, threaded(found("Deal with leak", due="2026-09-21"), sent=sent))]
    ).added
    assert item.due is None
    assert item.question == "From an email on 20 Sep (was due 21 Sep). Still needed?"
    [later] = add_email_items(
        store,
        [
            (
                2,
                threaded(
                    found("Renew membership", due="2026-12-01"),
                    subject="Membership renewal is due soon",
                    msg="<m@x>",
                    sent=sent,
                ),
            )
        ],
    ).added
    assert later.due == "2026-12-01" and later.question is None  # still in the future


def test_subject_helpers():
    assert subject_key("RE: Fwd:  FW[2]: Water   Leak") == "water leak"
    assert same_subject("water leak at flat 3 oak court", "flat 3 - water leak at flat 3 oak court")
    assert not same_subject("invoice", "re invoice")
    assert not same_subject(None, "x")
