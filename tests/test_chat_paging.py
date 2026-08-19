"""Chat-line fetching stops at the caller's watermark.

The old `chat_lines` paged blindly to `max_pages`. Because a room with any
history always advertises a `Link: next`, that meant five HTTP requests per
Campfire room *and* per Ping thread on every poll — almost all of them
re-fetching lines already in the database.
"""
import httpx

from app.basecamp.client import BasecampClient


class _FakeTransport:
    """Serves paginated lines.json and records how many pages were asked for."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        # Page number rides in the query string after the first request.
        page = int(dict(request.url.params).get("page", 1))
        self.requested.append(page)
        body = self.pages[page - 1] if page - 1 < len(self.pages) else []
        headers = {}
        if page < len(self.pages):
            headers["Link"] = f'<https://api.test/lines.json?page={page + 1}>; rel="next"'
        return httpx.Response(200, json=body, headers=headers)


def _client(transport: _FakeTransport) -> BasecampClient:
    client = BasecampClient("tok", 1, "https://api.test")
    client._http = httpx.Client(transport=httpx.MockTransport(transport.handle))
    return client


def _lines(*ids):
    return [{"id": i, "content": f"line {i}"} for i in ids]


def test_stops_once_a_page_reaches_the_watermark():
    # Newest first: page 1 is all new, page 2 straddles the watermark.
    fake = _FakeTransport([_lines(30, 29), _lines(28, 27), _lines(26, 25)])
    client = _client(fake)
    try:
        got, complete = client.chat_lines(1, 2, since_id=27)
    finally:
        client.close()

    assert fake.requested == [1, 2]  # page 3 never fetched
    assert {line["id"] for line in got} == {30, 29, 28, 27}


def test_first_sight_reads_a_single_page():
    """No watermark means the caller is only seeding one, so one page is plenty."""
    fake = _FakeTransport([_lines(9, 8), _lines(7, 6), _lines(5, 4)])
    client = _client(fake)
    try:
        got, complete = client.chat_lines(1, 2, since_id=None)
    finally:
        client.close()

    assert fake.requested == [1]
    assert len(got) == 2


def test_pages_to_the_cap_when_everything_is_new():
    """A genuinely busy room still gets paged, just bounded."""
    fake = _FakeTransport([_lines(i, i - 1) for i in range(40, 20, -2)])
    client = _client(fake)
    try:
        got, complete = client.chat_lines(1, 2, since_id=0, max_pages=3)
    finally:
        client.close()

    assert fake.requested == [1, 2, 3]
    assert len(got) == 6


def test_stops_on_an_empty_page():
    fake = _FakeTransport([_lines(5, 4), []])
    client = _client(fake)
    try:
        got, complete = client.chat_lines(1, 2, since_id=1)
    finally:
        client.close()
    assert len(got) == 2
