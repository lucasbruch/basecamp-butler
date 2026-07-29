"""One chat thread produces one suggestion, not one per poll cycle.

A conversation arrives as a trickle: "hey" at 10:00, "can you review the deck"
at 10:04, "before Friday" at 10:11. Each poll saw a fresh burst in the same
thread and raised its own to-do, so a single ask became three near-identical
suggestions — the app's most visible source of noise.
"""
from app.classifier import rules
from app.runtime import RuntimeConfig, defaults

from helpers import ago, identity, make_event, make_todo


def cfg(**over):
    return RuntimeConfig(**{**defaults(), "thread_coalesce_hours": 6, **over})


def test_open_suggestion_suppresses_a_second_one(db):
    make_todo(db, thread_key="7", status="suggested")
    assert rules.thread_has_open_todo(db, "7", 6)


def test_confirmed_also_suppresses(db):
    """You've already acted on it; a duplicate is still noise."""
    make_todo(db, thread_key="7", status="confirmed")
    assert rules.thread_has_open_todo(db, "7", 6)


def test_dismissed_thread_can_raise_again(db):
    """Once you've cleared it, a genuinely new ask in that thread should land."""
    make_todo(db, thread_key="7", status="dismissed")
    assert not rules.thread_has_open_todo(db, "7", 6)


def test_done_thread_can_raise_again(db):
    make_todo(db, thread_key="7", status="done")
    assert not rules.thread_has_open_todo(db, "7", 6)


def test_a_stale_open_todo_stops_muting_the_thread(db):
    """A confirmed to-do you forgot about months ago shouldn't silence the
    thread forever — the window bounds it."""
    make_todo(db, thread_key="7", status="confirmed", created_at=ago(hours=48))
    assert not rules.thread_has_open_todo(db, "7", 6)


def test_other_threads_are_unaffected(db):
    make_todo(db, thread_key="7", status="suggested")
    assert not rules.thread_has_open_todo(db, "8", 6)


def test_zero_window_disables_the_guard(db):
    make_todo(db, thread_key="7", status="suggested")
    assert not rules.thread_has_open_todo(db, "7", 0)


def test_missing_thread_key_never_suppresses(db):
    make_todo(db, thread_key=None, status="suggested")
    assert not rules.thread_has_open_todo(db, None, 6)


# ── end to end through the rule classifier ───────────────────────────────────
def test_second_burst_in_a_thread_raises_nothing(db):
    identity(db)
    # First burst: a clear ask, so it becomes a suggestion.
    make_event(db, basecamp_id=1, chat_id=7, etype="ping",
               body="can you review the deck", when=ago(hours=1))
    first = rules.classify_events(db)
    assert len(first) == 1

    # Second burst, same thread, still a clear ask.
    make_event(db, basecamp_id=2, chat_id=7, etype="ping",
               body="please send the budget too", when=ago(hours=0))
    second = rules.classify_events(db)
    assert second == []

    # And the event is still marked processed, so it doesn't retry forever.
    from app.models import RawEvent

    assert all(e.processed for e in db.query(RawEvent).all())


def test_a_different_thread_still_raises(db):
    identity(db)
    make_event(db, basecamp_id=1, chat_id=7, etype="ping", body="please review the deck")
    assert len(rules.classify_events(db)) == 1

    make_event(db, basecamp_id=2, chat_id=8, etype="ping", body="please review the budget")
    assert len(rules.classify_events(db)) == 1


def test_thread_key_is_recorded_on_the_todo(db):
    identity(db)
    make_event(db, basecamp_id=1, chat_id=42, etype="ping", body="please send the file")
    created = rules.classify_events(db)
    assert len(created) == 1

    from app.models import Todo

    assert db.get(Todo, created[0]).thread_key == "42"
