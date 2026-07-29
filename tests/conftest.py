"""A real (SQLite) database for the tests that need one.

The production schema is Postgres, but the only Postgres-specific piece is the
JSONB payload column, which carries a plain-JSON variant for other dialects.
That's enough to exercise the retention sweep, the thread-coalescing guard and
the classifier end to end without asking anyone to run a server to see the tests
pass.

Tests that would need Postgres semantics specifically (the `payload ->> '_chat_id'`
lookup in `prior_context`) are already safe — that helper returns [] on any
backend error by design.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401  (register tables on Base.metadata)
from app.db import Base


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
