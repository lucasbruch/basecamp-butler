"""Due dates: a calendar day and an instant are not the same thing.

Basecamp's ``due_on`` is a bare date ("2026-08-05"), which we store as midnight
UTC. Running that through a timezone conversion moved every due date onto the
day before for anyone west of UTC, while a schedule entry's ``starts_at`` is a
real instant that *must* be converted or a late-evening meeting lands on the
wrong day. Same column, opposite handling — `due_all_day` is what tells them
apart, and these tests pin both directions.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.util import due_on, parse_bc_datetime
from app.web.routes import _duedate

BARE = parse_bc_datetime("2026-08-05")                 # 2026-08-05T00:00:00Z
LATE = parse_bc_datetime("2026-08-05T23:30:00.000Z")   # a 01:30 Berlin meeting


class FakeTodo:
    def __init__(self, due_date, due_all_day):
        self.due_date = due_date
        self.due_all_day = due_all_day


@pytest.mark.parametrize("tz", [
    "UTC", "Europe/Berlin", "America/New_York", "America/Los_Angeles",
    "Pacific/Honolulu", "Pacific/Auckland",
])
def test_a_bare_due_date_names_the_same_day_everywhere(tz):
    assert due_on(BARE, ZoneInfo(tz), all_day=True).isoformat() == "2026-08-05"


def test_a_real_instant_is_converted_into_the_zone():
    assert due_on(LATE, ZoneInfo("UTC")).isoformat() == "2026-08-05"
    # 23:30Z is already tomorrow in Berlin, and still today in Los Angeles.
    assert due_on(LATE, ZoneInfo("Europe/Berlin")).isoformat() == "2026-08-06"
    assert due_on(LATE, ZoneInfo("America/Los_Angeles")).isoformat() == "2026-08-05"


def test_a_naive_value_is_read_as_utc():
    naive = datetime(2026, 8, 5, 0, 0)
    assert due_on(naive, ZoneInfo("America/New_York"), all_day=True).isoformat() == "2026-08-05"


def test_no_due_date_is_none():
    assert due_on(None, ZoneInfo("UTC")) is None


# ── the template filter, which is what the dashboard actually calls ──────────
def test_the_filter_does_not_shift_an_all_day_date_west_of_utc():
    assert _duedate(FakeTodo(BARE, True), "America/New_York") == "2026-08-05"


def test_the_filter_still_localises_a_meeting():
    assert _duedate(FakeTodo(LATE, False), "Europe/Berlin") == "2026-08-06"


def test_the_filter_handles_a_missing_date_and_a_junk_zone():
    assert _duedate(FakeTodo(None, False)) == "—"
    assert _duedate(FakeTodo(LATE, False), "Nowhere/Real") == "2026-08-05"


# ── what the classifier records ──────────────────────────────────────────────
def test_a_basecamp_todo_due_date_is_flagged_all_day(db):
    from app.classifier import rules
    from tests.helpers import identity, make_event

    identity(db, user_id=99)
    cfg = _cfg()
    ev = make_event(
        db, etype="todo", chat_id=None,
        content="Ship the cut", due_on="2026-08-05",
        assignees=[{"id": 99, "name": "Sam"}],
    )
    ids = rules._classify_todo(db, ev, my_id=99, cfg=cfg)

    from app.models import Todo
    todo = db.get(Todo, ids[0])
    assert todo.due_all_day is True
    assert _duedate(todo, "America/New_York") == "2026-08-05"


def test_a_meeting_start_is_not_flagged_all_day(db):
    from app.classifier import rules
    from tests.helpers import identity, make_event

    identity(db, user_id=99)
    ev = make_event(
        db, etype="schedule", chat_id=None, title="Dailies",
        starts_at="2026-08-05T23:30:00.000Z",
        participants=[{"id": 99, "name": "Sam"}],
    )
    ids = rules._classify_shared_item(db, ev, my_id=99, my_name="Sam", cfg=_cfg())

    from app.models import Todo
    todo = db.get(Todo, ids[0])
    assert todo.due_all_day is False
    assert _duedate(todo, "Europe/Berlin") == "2026-08-06"


def _cfg(**over):
    from app import runtime

    return runtime.RuntimeConfig(**{**runtime.defaults(), **over})
