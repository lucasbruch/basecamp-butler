"""Ping feed paging: it should walk back past a single page when a burst of DMs
spans pages, but stop once a page is entirely older than the watermark."""
from datetime import datetime, timezone

from app.poller.poller import _PING_MAX_PAGES, _fetch_ping_notifications


class _FakeClient:
    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def my_readings(self, page=1):
        self.calls.append(page)
        # 1-indexed pages; empty dict once we run past the end.
        return self._pages[page - 1] if page - 1 < len(self._pages) else {}


def _ping(pid, created):
    return {"id": pid, "section": "pings", "created_at": created}


def test_pages_until_older_than_watermark():
    watermark = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
    pages = [
        {"unreads": [_ping(3, "2024-01-03T10:00:00Z")]},   # newer
        {"reads": [_ping(2, "2024-01-02T12:00:00Z")]},     # newer
        {"reads": [_ping(1, "2024-01-01T09:00:00Z")]},     # OLDER → stop after this
        {"reads": [_ping(0, "2023-12-31T09:00:00Z")]},     # must not be fetched
    ]
    client = _FakeClient(pages)
    got = _fetch_ping_notifications(client, watermark)
    assert [p["id"] for p in got] == [3, 2, 1]
    assert client.calls == [1, 2, 3]  # stopped once page 3 predated the watermark


def test_first_run_reads_one_page_then_stops_on_empty():
    # watermark=None (first run): no early-stop, but it stops at the first empty page.
    pages = [{"unreads": [_ping(9, "2024-01-05T10:00:00Z")]}, {}]
    client = _FakeClient(pages)
    got = _fetch_ping_notifications(client, None)
    assert [p["id"] for p in got] == [9]
    assert client.calls == [1, 2]


def test_respects_page_cap():
    # Every page is "newer" than the watermark and never empty → cap kicks in.
    watermark = datetime(2020, 1, 1, tzinfo=timezone.utc)
    pages = [{"unreads": [_ping(i, "2024-01-05T10:00:00Z")]} for i in range(50)]
    client = _FakeClient(pages)
    _fetch_ping_notifications(client, watermark)
    assert client.calls == list(range(1, _PING_MAX_PAGES + 1))
