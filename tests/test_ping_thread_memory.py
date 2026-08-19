"""Ping threads are remembered, so the notifications feed isn't the only way in.

Discovery reads a few pages of /my/readings.json. On a busy account a very much
alive conversation can be pushed past those pages, at which point its new
messages stop being ingested at all — and a butler that never sees the message
can't answer it. Once a thread is known it gets polled directly for a week.
"""
import json
from datetime import timedelta

from app.models import AppState
from app.poller import poller
from app.util import utcnow


def _record(db, chat_id, circle=555, url=None, seen=None):
    db.merge(AppState(key=f"{poller.KNOWN_THREAD_PREFIX}{chat_id}", value=json.dumps(
        {"circle": circle, "seen": (seen or utcnow()).isoformat(), "url": url}
    )))
    db.flush()


def test_a_thread_we_have_seen_is_polled_without_the_feed(db):
    poller._remember_ping_thread(db, 555, 7, "https://3.basecamp.com/1/x")
    db.flush()
    known = poller._known_ping_threads(db)
    assert list(known) == [(555, 7)]
    assert known[(555, 7)]["app_url"] == "https://3.basecamp.com/1/x"


def test_a_thread_that_went_quiet_for_a_week_drops_off(db):
    _record(db, 7, seen=utcnow() - timedelta(days=poller.KNOWN_THREAD_DAYS + 1))
    assert poller._known_ping_threads(db) == {}


def test_only_the_most_recent_threads_are_chased(db):
    for chat in range(poller.MAX_KNOWN_THREADS + 5):
        _record(db, chat + 1, seen=utcnow() - timedelta(minutes=chat))
    known = poller._known_ping_threads(db)
    assert len(known) == poller.MAX_KNOWN_THREADS
    assert (555, 1) in known  # newest kept
    assert (555, poller.MAX_KNOWN_THREADS + 5) not in known  # oldest dropped


def test_the_deep_link_survives_a_poll_that_had_no_notification(db):
    """A thread reached directly carries no notification, and blanking the URL
    would strip the "open in Basecamp" link off everything ingested that way."""
    poller._remember_ping_thread(db, 555, 7, "https://3.basecamp.com/1/x")
    poller._remember_ping_thread(db, 555, 7, None)
    db.flush()
    assert poller._known_ping_threads(db)[(555, 7)]["app_url"] == (
        "https://3.basecamp.com/1/x"
    )


def test_a_hand_edited_row_does_not_break_the_poll(db):
    db.merge(AppState(key=f"{poller.KNOWN_THREAD_PREFIX}7", value="not json"))
    db.merge(AppState(key=f"{poller.KNOWN_THREAD_PREFIX}oops", value="{}"))
    db.flush()
    assert poller._known_ping_threads(db) == {}
