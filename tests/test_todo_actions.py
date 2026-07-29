"""The shared action layer: what ✅/✖/✔/💤 mean, wherever they're pressed.

The web UI, the ntfy buttons and the Telegram keyboard all route through
`todos.apply_action`, so a press on the phone and a click in the browser have to
land in exactly the same state.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import runtime, todos as todo_actions
from app.models import Reminder

from helpers import make_todo


def cfg(**over):
    return runtime.RuntimeConfig(**{**runtime.defaults(), "timezone": "UTC", **over})


def test_confirm_sets_status(db):
    t = make_todo(db, status="suggested")
    todo_actions.apply_action(db, t.id, "confirm", cfg())
    assert t.status == "confirmed"
    assert t.completed_at is None


def test_done_stamps_completed_at(db):
    """Without this there's no way to answer "what did I close this week"."""
    t = make_todo(db, status="confirmed")
    todo_actions.apply_action(db, t.id, "done", cfg())
    assert t.status == "done"
    assert t.completed_at is not None


def test_reopening_clears_completed_at(db):
    t = make_todo(db, status="done")
    t.completed_at = datetime.now(timezone.utc)
    todo_actions.apply_action(db, t.id, "reopen", cfg())
    assert t.status == "suggested"
    assert t.completed_at is None


def test_acting_on_a_snoozed_todo_ends_the_snooze(db):
    t = make_todo(db, status="suggested",
                  snoozed_until=datetime.now(timezone.utc) + timedelta(days=3))
    todo_actions.apply_action(db, t.id, "confirm", cfg())
    assert t.snoozed_until is None


def test_snooze_sets_a_future_wake_time(db):
    t = make_todo(db, status="suggested")
    todo_actions.apply_action(db, t.id, "snooze-1h", cfg())
    assert t.snoozed_until > datetime.now(timezone.utc)
    assert t.status == "suggested"  # snoozing is not a decision


def test_snooze_queues_the_reminder_that_wakes_it(db):
    """A snooze with no nudge is just hiding it."""
    t = make_todo(db, status="suggested")
    todo_actions.apply_action(db, t.id, "snooze-3h", cfg())
    reminders = db.query(Reminder).filter(Reminder.todo_id == t.id).all()
    assert len(reminders) == 1
    # SQLite has no tz-aware storage, so the round-tripped value comes back
    # naive; Postgres keeps the offset. Compare the instant, not the tzinfo.
    assert reminders[0].remind_at.replace(tzinfo=None) == t.snoozed_until.replace(tzinfo=None)


def test_unknown_action_is_a_no_op(db):
    t = make_todo(db, status="suggested")
    assert todo_actions.apply_action(db, t.id, "detonate", cfg()) is None
    assert t.status == "suggested"


def test_missing_todo_returns_none(db):
    assert todo_actions.apply_action(db, 4242, "confirm", cfg()) is None


# ── snooze arithmetic ────────────────────────────────────────────────────────
def test_tomorrow_lands_on_the_next_morning_local():
    c = cfg(timezone="Europe/Berlin")
    when = todo_actions.snooze_until(c, "snooze-tomorrow")
    local = when.astimezone(c.tz)
    assert local.hour == todo_actions.WAKE_HOUR
    assert local.date() > datetime.now(c.tz).date()


def test_next_week_lands_on_a_monday():
    c = cfg(timezone="UTC")
    when = todo_actions.snooze_until(c, "snooze-week")
    local = when.astimezone(c.tz)
    assert local.weekday() == 0          # Monday
    assert local.hour == todo_actions.WAKE_HOUR
    assert when > datetime.now(timezone.utc)


@pytest.mark.parametrize("action", sorted(todo_actions.SNOOZE_ACTIONS))
def test_every_preset_is_in_the_future(action):
    assert todo_actions.snooze_until(cfg(), action) > datetime.now(timezone.utc)


def test_all_actions_covers_both_families():
    assert set(todo_actions.ALL_ACTIONS) == (
        set(todo_actions.STATUS_ACTIONS) | set(todo_actions.SNOOZE_ACTIONS)
    )
