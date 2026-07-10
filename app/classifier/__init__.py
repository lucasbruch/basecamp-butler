"""Classifier dispatch: turn unprocessed raw events into suggested to-dos.

Pick the tier with the CLASSIFIER env var ("rules" | "ollama").
"""
from __future__ import annotations

import logging
import threading

from .. import activity
from ..config import settings
from ..db import session_scope
from ..models import Todo
from ..notifier import notify_new_todo
from . import rules

log = logging.getLogger(__name__)

# Both the poll cycle and the standalone retry sweep (main.py) call
# classify_new_events. This lock stops them running at once and double-
# processing the same unprocessed events.
_classify_lock = threading.Lock()


def classify_new_events() -> None:
    """Process unprocessed events, serialized across all callers.

    Non-blocking: if a classify pass is already running, this trigger simply
    returns — the in-flight pass is already draining the queue.
    """
    if not _classify_lock.acquire(blocking=False):
        log.debug("Classification already in progress; skipping this trigger.")
        return
    try:
        _classify_and_notify()
    finally:
        _classify_lock.release()


def _classify_and_notify() -> None:
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
