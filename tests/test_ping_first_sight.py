"""What happens the first time we look at a Ping conversation.

Two situations that look identical in the database — a thread with no watermark
— and must not be treated the same:

  * the app's own first run, where *every* thread is unseen and reading them in
    would turn years of history into to-dos, and
  * a conversation that has just started, whose opening line is the entire
    reason anyone would want this feature.

Seeding both meant a new person's first ping was swallowed, and (because the
auto-reply watermark seeds on top of that) their second one got silence too.
"""
from datetime import timedelta

from app.models import AppState, RawEvent
from app.poller import poller
from app.util import utcnow


class FakeClient:
    """Just enough BasecampClient to drive `_ingest_ping_chat`."""

    def __init__(self, lines):
        self.lines = lines
        self.since_ids = []

    def chat_lines(self, bucket_id, chat_id, *, since_id=None, max_pages=5):
        self.since_ids.append(since_id)
        return self.lines


def line(lid, *, body="hello", who="Ana", age_hours=0):
    when = utcnow() - timedelta(hours=age_hours)
    return {
        "id": lid,
        "content": body,
        "creator": {"id": 9, "name": who},
        "created_at": when.isoformat(),
    }


def pings(db):
    return db.query(RawEvent).filter_by(type="ping").order_by(RawEvent.basecamp_id).all()


def watermark(db, chat_id=77):
    row = db.get(AppState, f"ping_cp_{chat_id}")
    return int(row.value) if row else None


# ── the app's first run ──────────────────────────────────────────────────────

def test_first_run_seeds_without_reading_anything_in(db):
    """Years of history must not become to-dos the moment the app is switched on."""
    client = FakeClient([line(101), line(102)])

    stored = poller._ingest_ping_chat(db, client, 5, 77, {}, first_run=True)

    assert stored == 0
    assert pings(db) == []
    assert watermark(db) == 102


def test_first_run_still_remembers_the_thread(db):
    """So it can be polled directly later, feed or no feed."""
    poller._ingest_ping_chat(db, FakeClient([line(101)]), 5, 77, {}, first_run=True)

    assert db.get(AppState, f"{poller.KNOWN_THREAD_PREFIX}77") is not None


# ── a conversation that has just started ─────────────────────────────────────

def test_a_brand_new_thread_has_its_first_message_read(db):
    """The regression this file exists for: line #1 used to be seeded away."""
    client = FakeClient([line(101, body="Can you look at the render?")])

    stored = poller._ingest_ping_chat(db, client, 5, 77, {}, first_run=False)

    assert stored == 1
    assert [e.basecamp_id for e in pings(db)] == [101]
    assert watermark(db) == 101


def test_every_recent_line_of_a_new_thread_is_read(db):
    """Someone who sends three messages before the first poll gets all three."""
    client = FakeClient([line(101), line(102), line(103)])

    stored = poller._ingest_ping_chat(db, client, 5, 77, {}, first_run=False)

    assert stored == 3
    assert [e.basecamp_id for e in pings(db)] == [101, 102, 103]


def test_a_new_thread_carries_the_circle_and_chat_ids(db):
    """The classifier and the reply pass both group on `_chat_id`."""
    poller._ingest_ping_chat(db, FakeClient([line(101)]), 5, 77, {}, first_run=False)

    payload = pings(db)[0].payload
    assert payload["_circle_id"] == 5
    assert payload["_chat_id"] == 77


# ── the bound on how far back a new thread may reach ─────────────────────────

def test_a_dormant_thread_resurfacing_does_not_dump_its_history(db):
    """Only the recent end is taken; the rest is walked past, not re-examined."""
    old = poller.NEW_THREAD_LOOKBACK_HOURS + 6
    client = FakeClient([line(101, age_hours=old), line(102, age_hours=old), line(103)])

    stored = poller._ingest_ping_chat(db, client, 5, 77, {}, first_run=False)

    assert stored == 1
    assert [e.basecamp_id for e in pings(db)] == [103]
    # The watermark clears the old lines too, so they aren't weighed every poll.
    assert watermark(db) == 103


def test_a_thread_that_is_entirely_old_is_still_remembered(db):
    """Nothing to ingest, but losing track of it would leave only the feed."""
    old = poller.NEW_THREAD_LOOKBACK_HOURS + 6
    client = FakeClient([line(101, age_hours=old)])

    stored = poller._ingest_ping_chat(db, client, 5, 77, {}, first_run=False)

    assert stored == 0
    assert watermark(db) == 101
    assert db.get(AppState, f"{poller.KNOWN_THREAD_PREFIX}77") is not None


# ── the ordinary case is unchanged ───────────────────────────────────────────

def test_an_established_thread_reads_only_what_is_new(db):
    db.merge(AppState(key="ping_cp_77", value="102"))
    db.flush()
    client = FakeClient([line(101), line(102), line(103)])

    stored = poller._ingest_ping_chat(db, client, 5, 77, {}, first_run=False)

    assert stored == 1
    assert [e.basecamp_id for e in pings(db)] == [103]
    assert client.since_ids == [102]


def test_an_established_thread_ignores_the_age_bound(db):
    """A watermark already says where we are; age is only a proxy for new threads."""
    db.merge(AppState(key="ping_cp_77", value="100"))
    db.flush()
    old = poller.NEW_THREAD_LOOKBACK_HOURS + 6
    client = FakeClient([line(101, age_hours=old)])

    stored = poller._ingest_ping_chat(db, client, 5, 77, {}, first_run=False)

    assert stored == 1
    assert [e.basecamp_id for e in pings(db)] == [101]


def test_a_failed_fetch_leaves_the_watermark_alone(db):
    class Broken:
        def chat_lines(self, *a, **kw):
            raise RuntimeError("Basecamp said 502")

    assert poller._ingest_ping_chat(db, Broken(), 5, 77, {}, first_run=False) == 0
    assert watermark(db) is None  # so the next poll tries again


# ── feed entries we can't parse ──────────────────────────────────────────────

def test_unparseable_feed_entries_are_reported(db, caplog):
    """Silence here used to be indistinguishable from 'nobody pinged you'."""
    notifications = [
        {"subscription_url": "https://3.basecamp.com/1/buckets/5/recordings/77"},
        {"subscription_url": "https://3.basecamp.com/1/something/else"},
        {},
    ]

    convos = poller._ping_conversations(notifications)

    assert list(convos) == [(5, 77)]
    assert "2 feed entr" in caplog.text
