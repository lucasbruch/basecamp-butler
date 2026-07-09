"""Classifier dispatch: turn unprocessed raw events into suggested to-dos.

Pick the tier with the CLASSIFIER env var ("rules" | "ollama").
"""
from __future__ import annotations

import logging

from .. import activity
from ..config import settings
from ..db import session_scope
from ..models import Todo
from ..notifier import notify_new_todo
from . import rules

log = logging.getLogger(__name__)


def classify_new_events() -> None:
    """Process every unprocessed raw event and notify on newly created to-dos."""
    if settings.classifier == "ollama":
        from . import ollama

        classifier = ollama.classify_events
    else:
        classifier = rules.classify_events

    with session_scope() as db:
        created = classifier(db)

    # Notify outside the DB transaction so a slow/failed push never blocks commit.
    for todo_id in created:
        try:
            notify_new_todo(todo_id)
        except Exception:  # notifications are best-effort
            log.exception("Failed to notify for todo %s", todo_id)
            with session_scope() as db:
                activity.record(
                    db, "error", f"Failed to send a {settings.notify_channel} alert."
                )
            continue
        # Only note a push when a channel is actually configured to send one.
        if settings.notify_channel in ("ntfy", "telegram"):
            with session_scope() as db:
                todo = db.get(Todo, todo_id)
                if todo:
                    activity.record(
                        db,
                        "notify",
                        f"Sent a {settings.notify_channel} alert: “{todo.title}”",
                        url=todo.source_url,
                    )
