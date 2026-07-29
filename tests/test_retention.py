"""The retention sweep — the only thing standing between `raw_events` and a
full NAS volume, since it grows by one JSONB row per Campfire line forever."""
from app import retention
from app.models import RawEvent, Todo
from app.runtime import RuntimeConfig, defaults

from helpers import ago, make_event, make_todo


def cfg(**over):
    return RuntimeConfig(
        **{**defaults(), "raw_event_retention_days": 90, "todo_retention_days": 180, **over}
    )


def test_old_processed_events_are_removed(db):
    make_event(db, basecamp_id=1, when=ago(days=200), processed=True)
    make_event(db, basecamp_id=2, when=ago(days=1), processed=True)
    removed, _ = retention.sweep(db, cfg())
    assert removed == 1
    assert [e.basecamp_id for e in db.query(RawEvent).all()] == [2]


def test_unprocessed_events_survive_regardless_of_age(db):
    """An event still queued behind an unreachable LLM must not be deleted —
    that would silently drop the very backlog the retry sweep exists to drain."""
    make_event(db, basecamp_id=1, when=ago(days=400), processed=False)
    removed, _ = retention.sweep(db, cfg())
    assert removed == 0
    assert db.query(RawEvent).count() == 1


def test_events_a_live_todo_points_at_survive(db):
    """Otherwise "open in Basecamp" on an old but still-open to-do would rot."""
    ev = make_event(db, basecamp_id=1, when=ago(days=400), processed=True)
    make_todo(db, source_event_id=ev.id, status="suggested")
    removed, _ = retention.sweep(db, cfg())
    assert removed == 0


def test_resolved_todos_are_removed_after_their_window(db):
    make_todo(db, title="old", status="done", created_at=ago(days=300),
              updated_at=ago(days=300))
    make_todo(db, title="recent", status="dismissed", created_at=ago(days=2),
              updated_at=ago(days=2))
    _, removed = retention.sweep(db, cfg())
    assert removed == 1
    assert [t.title for t in db.query(Todo).all()] == ["recent"]


def test_open_todos_are_never_removed(db):
    make_todo(db, status="suggested", created_at=ago(days=900), updated_at=ago(days=900))
    make_todo(db, status="confirmed", created_at=ago(days=900), updated_at=ago(days=900))
    _, removed = retention.sweep(db, cfg())
    assert removed == 0
    assert db.query(Todo).count() == 2


def test_zero_disables_each_sweep(db):
    make_event(db, basecamp_id=1, when=ago(days=999), processed=True)
    make_todo(db, status="done", created_at=ago(days=999), updated_at=ago(days=999))
    events, todos = retention.sweep(db, cfg(raw_event_retention_days=0, todo_retention_days=0))
    assert (events, todos) == (0, 0)


def test_sweep_is_batched(db, monkeypatch):
    """A year of accumulation shouldn't become one enormous locking delete."""
    monkeypatch.setattr(retention, "BATCH", 3)
    for i in range(10):
        make_event(db, basecamp_id=i, when=ago(days=300), processed=True)
    removed, _ = retention.sweep(db, cfg())
    assert removed == 3
    assert db.query(RawEvent).count() == 7
