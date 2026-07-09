"""Notification surface. Currently Telegram; ntfy could slot in behind the same API."""
from __future__ import annotations

from .telegram import (
    notify_new_todo,
    notify_reminder,
    send_due_reminders,
    start_listener,
)

__all__ = [
    "notify_new_todo",
    "notify_reminder",
    "send_due_reminders",
    "start_listener",
]
