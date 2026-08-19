"""Regression guards for the crash paths and dead ends found in review.

Each test here stands for a specific way the app used to stop working on input
it had no business trusting: a header, a Basecamp payload field, a hand-editable
`app_state` row, or a Basecamp send that didn't land.
"""
import base64

import pytest
from fastapi.testclient import TestClient

from app import activity, autoreply
from app.basecamp.auth import STATE_KEY, consume_state
from app.basecamp.client import _retry_after
from app.config import settings
from app.models import ActivityLog, AppState, AutoReply, Todo
from app.util import MAX_URL, as_html, parse_bc_datetime, safe_url
from app.web import routes


# ── the auth gate must answer 401, never 500 ─────────────────────────────────
class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeRequest:
    def __init__(self, headers=None, path="/"):
        self.headers = headers or {}
        self.query_params = {}
        self.url = _FakeURL(path)


def _basic(user_and_password: str) -> _FakeRequest:
    creds = base64.b64encode(user_and_password.encode("utf-8")).decode()
    return _FakeRequest(headers={"Authorization": f"Basic {creds}"})


def test_non_ascii_password_is_rejected_not_raised(monkeypatch):
    """`hmac.compare_digest` refuses two non-ASCII *strings* by raising, which
    turned any Basic header carrying an umlaut into a 500."""
    monkeypatch.setattr(settings, "web_auth_token", "s3cret", raising=False)
    assert routes._request_authorized(_basic("user:pässwört")) is False


def test_non_ascii_configured_token_still_works(monkeypatch):
    """The worse half of the same bug: one non-ASCII character in
    WEB_AUTH_TOKEN made every authorized request fail."""
    monkeypatch.setattr(settings, "web_auth_token", "gehe1mnïs", raising=False)
    assert routes._request_authorized(_basic("user:gehe1mnïs")) is True
    assert routes._request_authorized(_basic("user:something-else")) is False


def test_non_ascii_basic_header_gets_a_401(monkeypatch):
    monkeypatch.setattr(settings, "web_auth_token", "s3cret", raising=False)
    client = TestClient(routes.create_app())
    creds = base64.b64encode("user:pässwört".encode("utf-8")).decode()
    resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 401


# ── the OAuth `state` must actually be required ──────────────────────────────
def test_callback_without_a_stored_state_is_refused(db):
    """The state row is burned on every callback, so "nothing stored means fine"
    meant the check never applied twice — and any code offered to the callback
    was accepted."""
    assert consume_state(db, "anything") is False


def test_matching_state_is_accepted_exactly_once(db):
    db.merge(AppState(key=STATE_KEY, value="the-real-state"))
    db.flush()
    assert consume_state(db, "the-real-state") is True
    # Burned: a replay of the same value no longer works.
    assert consume_state(db, "the-real-state") is False


def test_wrong_state_is_refused(db):
    db.merge(AppState(key=STATE_KEY, value="the-real-state"))
    db.flush()
    assert consume_state(db, "guessed") is False


# ── external strings must not overflow the columns they land in ──────────────
def test_over_long_url_is_dropped_not_truncated():
    """These go into String(1000) columns. Truncating would only swap a broken
    link for a different broken link; the INSERT failing takes down the pass."""
    assert safe_url("https://x/" + "a" * MAX_URL) is None
    assert safe_url("https://x/" + "a" * 10) is not None


def test_sender_name_is_bounded_to_the_person_column():
    class _Event:
        payload = {"creator": {"name": "N" * 500}}

    assert len(autoreply._sender(_Event())) == 200


# ── unparseable stored values must not raise the wrong exception ─────────────
def test_non_string_timestamp_raises_value_error():
    """Callers reading a hand-editable app_state row catch ValueError; an
    AttributeError went straight through them and failed the whole pass."""
    for value in (123, ["2024-01-02"], {"at": "2024-01-02"}):
        with pytest.raises(ValueError):
            parse_bc_datetime(value)


def test_parse_stamp_survives_a_corrupt_value():
    assert autoreply._parse_stamp(123) is None
    assert autoreply._parse_stamp("not a date") is None


def test_retry_after_tolerates_a_date_and_clamps():
    assert _retry_after("12") == 12
    assert _retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 5.0
    assert _retry_after(None) == 5.0
    assert _retry_after("99999") == 60.0
    assert _retry_after("-3") == 0.0


# ── a failed activity write must not poison the caller's transaction ─────────
def test_activity_failure_leaves_the_session_usable(db):
    """`record` promises a logging failure never breaks polling. Catching the
    exception alone didn't keep that promise: a failed flush leaves the session
    refusing every later statement with PendingRollbackError, so the poll cycle
    died anyway — just further along, where nothing said why.

    `kind` is NOT NULL, so this is a real flush failure and not a stubbed one.
    """
    db.add(Todo(title="a real row the caller is mid-way through writing"))
    db.flush()

    activity.record(db, None, "this insert cannot land")

    # The caller's transaction survives: it can still write and still read back.
    activity.record(db, "poll", "this one lands")
    assert db.query(ActivityLog).count() == 1
    assert db.query(Todo).count() == 1


def test_activity_url_is_bounded(db):
    activity.record(db, "poll", "hello", url="https://x/" + "a" * 5000)
    row = db.query(ActivityLog).one()
    assert len(row.url) == 1000


# ── a reply whose send failed stays actionable ───────────────────────────────
def _failed_reply(db, chat_id=42):
    reply = AutoReply(
        chat_id=chat_id,
        circle_id=1,
        person="Ana",
        draft="Will do.",
        status="failed",
        error="ConnectError: no route to host",
    )
    db.add(reply)
    db.flush()
    return reply


def test_pending_draft_counts_a_failed_send(db):
    """It's an unsent reply sitting on /replies under a Send button — stacking a
    second draft on top of it is the thing `pending_draft` exists to stop."""
    _failed_reply(db)
    assert autoreply.pending_draft(db, 42) is True


def test_failed_is_listed_as_waiting_not_as_history():
    assert "failed" in autoreply.PENDING_STATUSES
    assert "sent" not in autoreply.PENDING_STATUSES
    assert "discarded" not in autoreply.PENDING_STATUSES


# ── what write-back posts into a shared workspace is escaped ─────────────────
def test_writeback_description_is_escaped():
    """A to-do's `description` is rich text on Basecamp's side. Notes are typed
    into a form or lifted out of somebody's message, so a bare "<" in one used
    to swallow the rest of the note once Basecamp rendered it — the same bug the
    reply path already escapes for."""
    posted = as_html("5 < 6 & counting\nsecond line")
    assert "&lt;" in posted and "&amp;" in posted
    assert "<br>" in posted
    assert "<script" not in as_html("<script>alert(1)</script>")
