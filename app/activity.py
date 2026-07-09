"""Lightweight, human-readable activity log.

Instead of drowning the useful signal in APScheduler noise, we record a small
number of plain-English entries ("read a Ping from Ana", "asked the LLM about
it → no to-do") into the DB. The /activity page renders them. Writes are always
best-effort: a logging failure must never break polling or classification.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import ActivityLog

log = logging.getLogger(__name__)

# Keep the table bounded — this is a rolling window, not an audit trail.
MAX_ROWS = 1000


def record(
    db: Session,
    kind: str,
    summary: str,
    detail: str | None = None,
    url: str | None = None,
) -> None:
    """Append one activity entry within the caller's existing transaction."""
    try:
        db.add(
            ActivityLog(kind=kind, summary=summary[:1000], detail=detail, url=url)
        )
        db.flush()
    except Exception:
        log.exception("Failed to write activity log entry")


def prune(db: Session, keep: int = MAX_ROWS) -> None:
    """Drop everything older than the newest `keep` rows. Cheap enough per cycle."""
    try:
        ids = (
            db.execute(
                select(ActivityLog.id).order_by(ActivityLog.id.desc()).limit(keep)
            )
            .scalars()
            .all()
        )
        if len(ids) >= keep:
            cutoff = ids[-1]
            db.execute(delete(ActivityLog).where(ActivityLog.id < cutoff))
    except Exception:
        log.exception("Failed to prune activity log")
