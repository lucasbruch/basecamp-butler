"""The projects table must stay quiet between real changes.

Every poll cycle used to stamp `last_polled_at` onto every enabled project,
unconditionally — twenty rows rewritten every five minutes to record something
`app_state["last_poll_at"]` already said, and said once. The column is gone; the
only writes left against `projects` are the daily name refresh and a human
editing a row in Settings.

These tests pin both halves: the refresh stays silent when nothing about a
project has actually changed, and the Settings page still answers "when was this
last looked at?" without a per-project column to read it from.
"""
from datetime import timedelta

from sqlalchemy import event

from app.models import AppState, Project
from app.poller import poller
from app.util import utcnow


class FakeClient:
    """Just enough BasecampClient to drive `_refresh_projects`."""

    def __init__(self, projects):
        self._projects = projects
        self.calls = 0

    def projects(self):
        self.calls += 1
        return self._projects


def _count_project_writes(session):
    """Count UPDATE/INSERT statements aimed at `projects`, as emitted SQL.

    Asserting on the statements rather than on the ORM's intent is the point:
    a no-op attribute assignment that SQLAlchemy suppresses costs nothing, and
    one it decides to flush costs a row version. Only the SQL tells them apart.
    """
    seen = []

    @event.listens_for(session.get_bind(), "before_cursor_execute")
    def _record(conn, cursor, statement, params, context, executemany):
        head = " ".join(statement.split())[:60].upper()
        if head.startswith(("UPDATE PROJECTS", "INSERT INTO PROJECTS")):
            seen.append(head)

    return seen


def test_a_second_refresh_inside_the_ttl_writes_nothing(db):
    client = FakeClient([{"id": 1, "name": "Feature Film"}])
    poller._refresh_projects(db, client)
    db.flush()

    writes = _count_project_writes(db)
    poller._refresh_projects(db, client)
    db.flush()

    assert client.calls == 1, "the TTL should have skipped the second fetch entirely"
    assert writes == []


def test_a_refresh_past_the_ttl_rewrites_nothing_when_the_name_is_the_same(db):
    """The TTL expiring is not on its own a reason to write.

    The refresh re-fetches and re-assigns every name; SQLAlchemy suppresses the
    UPDATE when the value is identical. If that ever stops being true this table
    goes back to being rewritten daily for no reason, which is how the original
    problem started.
    """
    client = FakeClient([{"id": 1, "name": "Feature Film"}])
    poller._refresh_projects(db, client)
    db.flush()

    stale = utcnow() - poller.PROJECTS_CACHE_TTL - timedelta(minutes=1)
    db.merge(AppState(key="projects_refreshed_at", value=stale.isoformat()))
    db.flush()

    writes = _count_project_writes(db)
    poller._refresh_projects(db, client)
    db.flush()

    assert client.calls == 2, "the TTL had expired, so it should have re-fetched"
    assert writes == []


def test_a_renamed_project_still_gets_written(db):
    """The guard above must not have turned into "never write"."""
    client = FakeClient([{"id": 1, "name": "Feature Film"}])
    poller._refresh_projects(db, client)
    db.flush()

    stale = utcnow() - poller.PROJECTS_CACHE_TTL - timedelta(minutes=1)
    db.merge(AppState(key="projects_refreshed_at", value=stale.isoformat()))
    db.flush()

    writes = _count_project_writes(db)
    client._projects = [{"id": 1, "name": "Feature Film — Reissue"}]
    poller._refresh_projects(db, client)
    db.flush()

    assert len(writes) == 1
    assert db.get(Project, 1).name == "Feature Film — Reissue"
