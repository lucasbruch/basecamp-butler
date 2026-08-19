"""Ping discovery: scan the notifications feed for active ping threads, then map
them to unique (circle, chat) conversations. The actual message ingestion reads
chat-lines per thread (like Campfire), so discovery only needs to find threads."""
from app.poller.poller import (
    _PING_FEED_MAX_PAGES,
    _fetch_ping_notifications,
    _ping_conversations,
)


class _FakeClient:
    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def my_readings(self, page=1):
        self.calls.append(page)
        return self._pages[page - 1] if page - 1 < len(self._pages) else {}


def _ping(pid, sub_url):
    return {"id": pid, "section": "pings", "subscription_url": sub_url}


def test_collects_only_ping_section_across_pages():
    pages = [
        {"unreads": [_ping(3, "/buckets/1/recordings/9/subscription.json"),
                     {"id": 99, "section": "comments"}]},   # non-ping filtered out
        {"reads": [_ping(2, "/buckets/1/recordings/8/subscription.json")]},
        {},  # empty page → stop
    ]
    client = _FakeClient(pages)
    got = _fetch_ping_notifications(client)
    assert [n["id"] for n in got] == [3, 2]
    assert client.calls == [1, 2, 3]  # stopped at the empty page


def test_respects_feed_page_cap():
    pages = [{"unreads": [_ping(i, f"/buckets/1/recordings/{i}/subscription.json")]}
             for i in range(50)]
    client = _FakeClient(pages)
    _fetch_ping_notifications(client)
    assert client.calls == list(range(1, _PING_FEED_MAX_PAGES + 1))


def test_conversations_map_dedups_by_thread():
    notifs = [
        _ping(10, "/buckets/42/recordings/100/subscription.json"),
        _ping(11, "/buckets/42/recordings/100/subscription.json"),  # same thread → dedup
        _ping(12, "/buckets/43/recordings/200/subscription.json"),
        {"id": 13, "section": "pings", "subscription_url": "garbage"},  # unparseable → skipped
    ]
    convos = _ping_conversations(notifs)
    assert set(convos.keys()) == {(42, 100), (43, 200)}
    # Keeps the *latest* notification for a thread — and the feed is newest-first
    # (page 1 leads, see the test above), so that's the one seen first. Taking
    # the last one instead stored the stalest deep link we could find.
    assert convos[(42, 100)]["id"] == 10


def _noise(n=2):
    return [{"id": 900 + i, "section": "comments"} for i in range(n)]


def test_keeps_looking_past_pages_with_no_pings():
    """The busy-account case the old three-page scan lost: a ping sent minutes
    ago sitting below a wall of project notifications sent seconds ago."""
    pages = [
        {"unreads": _noise()},
        {"unreads": _noise()},
        {"unreads": _noise()},
        {"unreads": [_ping(7, "/buckets/1/recordings/9/subscription.json")]},
    ]
    client = _FakeClient(pages)

    got = _fetch_ping_notifications(client)

    assert [n["id"] for n in got] == [7]
    assert client.calls[:4] == [1, 2, 3, 4]


def test_stops_once_the_pings_have_run_out():
    """Having found them, two ping-free pages means we're into older news."""
    pages = [
        {"unreads": [_ping(1, "/buckets/1/recordings/9/subscription.json")]},
        {"reads": _noise()},
        {"reads": _noise()},
        {"reads": [_ping(2, "/buckets/1/recordings/8/subscription.json")]},  # unread
    ]
    client = _FakeClient(pages)

    got = _fetch_ping_notifications(client)

    assert [n["id"] for n in got] == [1]
    assert client.calls == [1, 2, 3]


def test_one_quiet_page_is_not_enough_to_stop():
    pages = [
        {"unreads": [_ping(1, "/buckets/1/recordings/9/subscription.json")]},
        {"reads": _noise()},
        {"reads": [_ping(2, "/buckets/1/recordings/8/subscription.json")]},
        {},
    ]
    client = _FakeClient(pages)

    got = _fetch_ping_notifications(client)

    assert [n["id"] for n in got] == [1, 2]
