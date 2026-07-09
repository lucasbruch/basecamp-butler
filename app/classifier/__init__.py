"""Classifier dispatch: turn unprocessed raw events into suggested to-dos.

Pick the tier with the CLASSIFIER env var ("rules" | "ollama").
"""
from __future__ import annotations

import logging

from ..config import settings
from ..db import session_scope
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
