"""What the four buttons actually do, in one place.

The web UI, the ntfy action buttons and the Telegram inline keyboard all move
to-dos between the same states. They used to each poke `todo.status` directly,
which is how Telegram ended up not clearing a snooze and not stamping
`completed_at` — the same action meaning subtly different things depending on
which surface you pressed it from.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from .models import Reminder, Todo
from .runtime import RuntimeConfig
from .util import utcnow

# action name -> resulting status
STATUS_ACTIONS = {
    "confirm": "confirmed",
    "dismiss": "dismissed",
    "done": "done",
    "reopen": "suggested",
}

# action name -> (label, fixed offset or None for "next <landmark> morning")
SNOOZE_ACTIONS = {
    "snooze-1h": ("1 hour", timedelta(hours=1)),
    "snooze-3h": ("3 hours", timedelta(hours=3)),
    "snooze-tomorrow": ("tomorrow morning", None),
    "snooze-week": ("next week", None),
}

ALL_ACTIONS = tuple(STATUS_ACTIONS) + tuple(SNOOZE_ACTIONS)

# When a "tomorrow"/"next week" snooze should surface, in local time.
WAKE_HOUR = 9


def snooze_until(cfg: RuntimeConfig, action: str):
    """When a snooze preset should wake the to-do back up (as a UTC datetime)."""
    _label, delta = SNOOZE_ACTIONS[action]
    now = utcnow()
    if delta is not None:
        return now + delta
    local = now.astimezone(cfg.tz)
    if action == "snooze-tomorrow":
        target = local + timedelta(days=1)
    else:  # next week — the coming Monday
        target = local + timedelta(days=(7 - local.weekday()) or 7)
    target = target.replace(hour=WAKE_HOUR, minute=0, second=0, microsecond=0)
    return target.astimezone(now.tzinfo)


def apply_action(db: Session, todo_id: int, action: str, cfg: RuntimeConfig) -> Todo | None:
    """Apply one action. Returns the updated row, or None if unknown/missing."""
    todo = db.get(Todo, todo_id)
    if todo is None:
        return None
    now = utcnow()

    if action in STATUS_ACTIONS:
        todo.status = STATUS_ACTIONS[action]
        todo.completed_at = now if todo.status == "done" else None
        if todo.status in ("suggested", "confirmed"):
            todo.snoozed_until = None  # acting on it ends any snooze
        return todo

    if action in SNOOZE_ACTIONS:
        until = snooze_until(cfg, action)
        todo.snoozed_until = until
        # A snooze without a nudge is just hiding it. Queue the reminder that
        # brings it back, on whichever channel is active right now.
        db.add(Reminder(todo_id=todo.id, remind_at=until, channel=cfg.notify_channel))
        # Flush so the reminder is visible to anything reading in this session
        # before the caller commits (sessions here run with autoflush off).
        db.flush()
        return todo

    return None
