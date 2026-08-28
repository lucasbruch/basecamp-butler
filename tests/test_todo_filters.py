"""The /todos filter bar has to survive its own empty fields.

"All projects" and "Any status" submit as empty strings, so every search typed
into the box arrived with `project=` attached. Declaring that parameter as an
int made FastAPI reject the empty value outright, and the page answered a 422
JSON blob instead of the search results.
"""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.models import Project
from app.web import routes

from helpers import make_todo


@pytest.fixture
def db():
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

    @contextmanager
    def scope():
        yield db
        db.flush()

    monkeypatch.setattr(routes, "session_scope", scope)
    return TestClient(routes.create_app())


def test_search_with_the_project_box_left_alone(client, db):
    """What the form actually sends when you only type in the search field."""
    make_todo(db, title="Fix blue outline on Alysha's head")
    make_todo(db, title="Re-render Grove 3301")
    resp = client.get("/todos", params={"q": "alysha", "status": "", "project": ""})
    assert resp.status_code == 200
    assert "Alysha" in resp.text
    assert "Grove 3301" not in resp.text


def test_a_chosen_project_still_filters(client, db):
    db.add(Project(id=7, name="Laika Wildwood"))
    db.add(Project(id=8, name="Playstation"))
    db.flush()
    make_todo(db, title="Roto scope check", project_id=7)
    make_todo(db, title="Endcard sequences", project_id=8)
    body = client.get("/todos", params={"q": "", "status": "", "project": "7"}).text
    assert "Roto scope check" in body
    assert "Endcard sequences" not in body


def test_a_junk_project_is_ignored_not_fatal(client, db):
    """A hand-edited URL shouldn't be able to 422 the page either."""
    make_todo(db, title="Roto scope check")
    resp = client.get("/todos", params={"project": "not-a-number"})
    assert resp.status_code == 200
    assert "Roto scope check" in resp.text
