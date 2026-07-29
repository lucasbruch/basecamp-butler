"""Keep the database from growing without bound.

`activity_log` was already a rolling window, but `raw_events` never was — and
it's the table that actually grows: one row per Campfire line, each carrying the
full Basecamp JSONB payload. On a busy account with chat polling on, that's the
thing that quietly fills a NAS volume over a year.

What's safe to delete:

  * processed raw events past the retention window. Unprocessed ones are never
    touched — an event still queued behind an unreachable LLM must survive.
    Events that a surviving to-do points at are also kept, so "open in Basecamp"
    and the classifier's thread context don't rot out from under a live to-do.
  * resolved (dismissed / done) to-dos past their own, longer window. Open ones
    are never deleted regardless of age.

Both windows are configurable from Settings; 0 disables that sweep.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import RawEvent, Todo
from .runtime import RuntimeConfig
from .util import utcnow

log = logging.getLogger(__name__)

# Don't delete more than this per sweep. A first run against a database that has
# been accumulating for a year would otherwise be one enormous transaction that
# locks the table and blocks the poll behind it; instead it drains over a few
# cycles, which nobody notices.
BATCH = 5000


def sweep(db: Session, cfg: RuntimeConfig) -> tuple[int, int]:
    """Delete aged-out rows. Returns (raw_events removed, todos removed)."""
    events = _sweep_raw_events(db, cfg.raw_event_retention_days)
    todos = _sweep_todos(db, cfg.todo_retention_days)
    if events or todos:
        log.info(
            "Retention sweep removed %d raw event(s) and %d resolved to-do(s).",
            events,
            todos,
        )
    return events, todos


def _sweep_raw_events(db: Session, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    # Anything a surviving to-do still points at stays, whatever its age.
    referenced = select(Todo.source_event_id).where(Todo.source_event_id.isnot(None))
    doomed = (
        select(RawEvent.id)
        .where(
            RawEvent.processed.is_(True),
            RawEvent.updated_at < cutoff,
            RawEvent.id.notin_(referenced),
        )
        .limit(BATCH)
    )
    ids = list(db.execute(doomed).scalars())
    if not ids:
        return 0
    db.execute(delete(RawEvent).where(RawEvent.id.in_(ids)))
    return len(ids)


def _sweep_todos(db: Session, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    doomed = (
        select(Todo.id)
        .where(
            Todo.status.in_(("dismissed", "done")),
            # Fall back to created_at for rows written before updated_at existed.
            func.coalesce(Todo.updated_at, Todo.created_at) < cutoff,
        )
        .limit(BATCH)
    )
    ids = list(db.execute(doomed).scalars())
    if not ids:
        return 0
    # Reminders cascade at the FK level; deleting via the ORM would emit one
    # SELECT per row to populate the relationship first.
    db.execute(delete(Todo).where(Todo.id.in_(ids)))
    return len(ids)
