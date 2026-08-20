"""A burst of Ping messages must be read in the order it was said.

Basecamp serves chat lines newest-first. The poller stores one row per line as
it walks that list, so a burst arriving in a single poll landed with row ids
running *backwards* against the clock. Everything downstream that treated the
last row as the last thing said then read the conversation upside down — and the
common shape of a real exchange (you ask, they answer) left your own line at the
bottom, so the pass concluded "your own message is the newest one here" and
watermarked past a reply that was waiting on an answer.
"""
import httpx
import pytest

from app import autoreply, runtime
from app.basecamp.client import BasecampClient
from app.classifier import conversation, ollama
from app.models import AppState, AutoReply, AutoReplyRule, RawEvent

from helpers import identity, make_event

MY_ID = 99


# ── the client hands lines back in the order they were said ─────────────────
def _client(pages):
    def handle(request):
        page = int(dict(request.url.params).get("page", 1))
        body = pages[page - 1] if page - 1 < len(pages) else []
        headers = {}
        if page < len(pages):
            headers["Link"] = f'<https://api.test/lines.json?page={page + 1}>; rel="next"'
        return httpx.Response(200, json=body, headers=headers)

    client = BasecampClient("tok", 1, "https://api.test")
    client._http = httpx.Client(transport=httpx.MockTransport(handle))
    return client


def _lines(*ids):
    return [{"id": i, "content": f"line {i}"} for i in ids]


def test_chat_lines_come_back_oldest_first():
    """Two newest-first pages, stitched into one chronological list."""
    client = _client([_lines(30, 29), _lines(28, 27)])
    try:
        got, _ = client.chat_lines(1, 2, since_id=27)
    finally:
        client.close()
    assert [line["id"] for line in got] == [27, 28, 29, 30]


# ── grouping puts a thread back in order whatever the row ids say ───────────
def test_group_by_thread_sorts_by_basecamp_line_id(db):
    """Row ids ascending, line ids descending — what a newest-first page wrote."""
    for row_id, line_id, body in [(1, 300, "newest"), (2, 299, "middle"),
                                  (3, 298, "oldest")]:
        make_event(db, etype="ping", event_id=row_id, basecamp_id=line_id,
                   chat_id=7, body=body, who="Alex", who_id=2)

    rows = db.query(RawEvent).order_by(RawEvent.id).all()
    (_chat, group), = conversation.group_by_thread(rows)
    assert [conversation._body(e) for e in group] == ["oldest", "middle", "newest"]
    assert conversation.latest_text(group) == "newest"


# ── and the pass answers the reply instead of writing it off ────────────────
def cfg(**over):
    return runtime.RuntimeConfig(
        **{**runtime.defaults(), "autoreply_enabled": True, **over}
    )


@pytest.fixture
def drafts(monkeypatch):
    monkeypatch.setattr(
        ollama,
        "draft_reply",
        lambda transcript, **kw: {
            "reply": True, "text": "On it.", "why": "asked", "prompt": "",
        },
    )


def _run(db, **over):
    saved = runtime.load
    runtime.load = lambda _db: cfg(**over)
    try:
        return autoreply._run(db)
    finally:
        runtime.load = saved


def _line(db, row_id, line_id, who, who_id, body):
    return make_event(
        db, etype="ping", project_id=None, event_id=row_id, basecamp_id=line_id,
        chat_id=7, body=body, who=who, who_id=who_id, _circle_id=555,
        app_url="https://3.basecamp.com/1/buckets/555/chats/7",
    )


def test_a_burst_ingested_newest_first_is_still_answered(db, drafts):
    """The real shape: you ask twice, they answer three times, one poll takes
    the lot — and the row the poller wrote last is your *oldest* message."""
    identity(db, MY_ID)
    db.add(AutoReplyRule(name="Alex Nindl", tone="warm", mode="draft", enabled=True))
    db.merge(AppState(key=f"{autoreply.CP_PREFIX}7", value="0"))
    db.flush()

    # Row ids count up in ingest order; Basecamp line ids count down, because
    # that is the order the API served them.
    _line(db, 10, 203, "Alex Nindl", 2, "can you send the budget?")
    _line(db, 11, 202, "Alex Nindl", 2, "feeling better, thanks")
    _line(db, 12, 201, "Lucas Bruchhage", MY_ID, "how are you feeling today?")
    _line(db, 13, 200, "Alex Nindl", 2, "let's wait in case they change direction")
    _line(db, 14, 199, "Lucas Bruchhage", MY_ID, "does it make sense to start now?")

    assert _run(db) == 1, "the last word is Alex's, not ours"
    reply = db.query(AutoReply).one()
    assert reply.person == "Alex Nindl"
    # The model was shown the exchange the right way round.
    assert reply.incoming.index("does it make sense") < reply.incoming.index(
        "can you send the budget?"
    )


def test_our_own_last_word_is_still_left_alone(db, drafts):
    """The guard the bug was hiding behind has to keep working."""
    identity(db, MY_ID)
    db.add(AutoReplyRule(name="Alex Nindl", tone="warm", mode="draft", enabled=True))
    db.merge(AppState(key=f"{autoreply.CP_PREFIX}7", value="0"))
    db.flush()

    _line(db, 10, 200, "Lucas Bruchhage", MY_ID, "thanks, that's all I needed")
    _line(db, 11, 199, "Alex Nindl", 2, "sent it over")

    assert _run(db) == 0
    assert db.query(AutoReply).count() == 0
