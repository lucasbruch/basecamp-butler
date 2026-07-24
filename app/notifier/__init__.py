"""Notification surface with pluggable channels (ntfy | telegram | none).

Pick the channel with NOTIFY_CHANNEL in .env. The classifier/poller call these
functions without caring which channel is active.
"""
from __future__ import annotations

import logging

from ..config import settings
from ..db import session_scope
from ..models import Reminder, Todo
from ..util import utcnow
from . import ntfy, telegram

log = logging.getLogger(__name__)


def notify_new_todo(todo_id: int) -> None:
    ch = settings.notify_channel
    if ch == "ntfy":
        ntfy.notify_new_todo(todo_id)
    elif ch == "telegram":
        telegram.notify_new_todo(todo_id)


def notify_reminder(todo_id: int) -> None:
    ch = settings.notify_channel
    if ch == "ntfy":
        ntfy.notify_reminder(todo_id)
    elif ch == "telegram":
        telegram.notify_reminder(todo_id)


def notify_text(title: str, message: str) -> bool:
    """Push a plain-text message over the active channel. Returns whether a
    channel was configured to send it (so callers can tell the user)."""
    ch = settings.notify_channel
    if ch == "ntfy" and settings.ntfy_enabled:
        ntfy.send_text(title, message)
        return True
    if ch == "telegram" and settings.telegram_enabled:
        telegram.send_text(title, message)
        return True
    return False


def start_listener():
    """Only Telegram needs an inbound listener; ntfy buttons hit /api directly."""
    if settings.notify_channel == "telegram":
        return telegram.start_listener()
    return None


def send_due_reminders() -> None:
    """Scheduler job: fire any reminders whose time has come (channel-agnostic).

    Semantics are at-most-once: we mark `sent=True` and commit before pushing, so
    a crash in the send window drops that one reminder rather than risking a
    duplicate on the next sweep. For a personal nudge that's the right trade — the
    to-do itself is never lost (it stays on the dashboard) and a due date still
    shows there; only the transient push can go missing.
    """
    now = utcnow()
    to_send: list[int] = []
    with session_scope() as db:
        rows = (
            db.query(Reminder)
            .filter(Reminder.sent.is_(False), Reminder.remind_at <= now)
            .all()
        )
        for r in rows:
            todo = db.get(Todo, r.todo_id)
            # Skip reminders for to-dos the user already dealt with.
            if todo and todo.status in ("suggested", "confirmed"):
                to_send.append(r.todo_id)
            r.sent = True
    for todo_id in to_send:
        notify_reminder(todo_id)


__all__ = [
    "notify_new_todo",
    "notify_reminder",
    "notify_text",
    "send_due_reminders",
    "start_listener",
]
