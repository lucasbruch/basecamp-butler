"""Confirming a suggestion has to land somewhere you can see.

The inline actions used to POST, then delete the row from the DOM and stop.
On the dashboard that meant "Add" made the to-do vanish — the "Confirmed
to-dos" list a few hundred pixels down never heard about it, and the only way
to see where the click had gone was to reload. These cover the two halves of
the fix: the endpoint hands back the re-rendered row, and the pages say which
list each row belongs in.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.web import routes

from helpers import make_todo

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db():
    """Like the shared fixture, but reachable from TestClient's worker thread —
    a default SQLite connection refuses to be used from anywhere but the thread
    that opened it."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(settings, "web_auth_token", "", raising=False)
    # Write-back needs Basecamp; the card is rendered from the row either way.
    monkeypatch.setattr(routes, "_maybe_writeback", lambda todo_id, action: None)

    @contextmanager
    def scope():
        yield db
        db.flush()

    monkeypatch.setattr(routes, "session_scope", scope)
    return TestClient(routes.create_app())


# ── the bucket a row lands in ────────────────────────────────────────────────
def test_bucket_follows_status(db):
    assert routes._todo_bucket(make_todo(db, status="suggested")) == "suggested"
    assert routes._todo_bucket(make_todo(db, status="confirmed")) == "confirmed"
    assert routes._todo_bucket(make_todo(db, status="dismissed")) == "dismissed"


def test_a_running_snooze_wins_over_the_status():
    """The dashboard hides snoozed rows in their own drawer, so that's where a
    just-snoozed card has to go — not back into the list it came from."""
    row = routes.Todo(status="confirmed", snoozed_until=NOW + timedelta(hours=3))
    assert routes._todo_bucket(row) == "snoozed"


def test_an_expired_snooze_is_back_under_its_status():
    row = routes.Todo(status="confirmed", snoozed_until=NOW - timedelta(hours=3))
    assert routes._todo_bucket(row) == "confirmed"


# ── the endpoint ─────────────────────────────────────────────────────────────
def test_confirm_returns_the_rerendered_row(client, db):
    todo = make_todo(db, title="Send the grading notes to Ana")
    resp = client.post(f"/api/todos/{todo.id}/confirm?card=1")
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["bucket"] == "confirmed"
    # The row comes back wearing its new state: the confirmed tag and the
    # "Done" action, not the "Add" it was rendered with a moment ago.
    assert f'data-todo-id="{todo.id}"' in body["card"]
    assert '<span class="tag confirmed">confirmed</span>' in body["card"]
    assert 'data-act="done"' in body["card"]
    assert 'data-act="confirm"' not in body["card"]


def test_the_card_is_opt_in(client, db):
    """ntfy's action buttons have no DOM to update, so they don't pay to render
    one."""
    todo = make_todo(db)
    body = client.post(f"/api/todos/{todo.id}/confirm").json()
    assert body["status"] == "confirmed"
    assert "card" not in body


def test_selectable_matches_the_page_that_asked(client, db):
    """/todos draws a bulk-select checkbox on every row and the dashboard
    doesn't, so a row moving on one page must not arrive dressed for the other."""
    todo = make_todo(db)
    plain = client.post(f"/api/todos/{todo.id}/confirm?card=1&selectable=0").json()
    picked = client.post(f"/api/todos/{todo.id}/reopen?card=1&selectable=1").json()
    assert "bulk-pick" not in plain["card"]
    assert "bulk-pick" in picked["card"]


def test_snoozing_reports_the_drawer_not_the_status(client, db):
    todo = make_todo(db, status="confirmed")
    body = client.post(f"/api/todos/{todo.id}/snooze-3h?card=1").json()
    assert body["status"] == "confirmed"
    assert body["bucket"] == "snoozed"
    assert "back in" in body["card"]


def test_bulk_returns_one_row_per_change(client, db):
    ids = [make_todo(db, title=f"Flood {i}").id for i in range(3)]
    body = client.post(
        "/api/todos/bulk",
        data={"ids": ",".join(str(i) for i in ids), "action": "confirm", "card": "1"},
    ).json()
    assert body["changed"] == 3
    assert [c["id"] for c in body["cards"]] == ids
    assert all(c["bucket"] == "confirmed" for c in body["cards"])


def test_bulk_without_the_flag_still_just_counts(client, db):
    ids = [make_todo(db).id]
    body = client.post(
        "/api/todos/bulk",
        data={"ids": str(ids[0]), "action": "dismiss"},
    ).json()
    assert body == {"ok": True, "changed": 1, "action": "dismiss"}
