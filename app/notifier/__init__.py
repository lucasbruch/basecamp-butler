"""Notification surface with pluggable channels (ntfy | telegram | none).

Pick the channel with NOTIFY_CHANNEL (or the Settings page). The classifier and
poller call these functions without caring which channel is active.

Two policies live here rather than in the channels, because they're about *when*
to interrupt someone, not how to reach them:

  * **Quiet hours** — nothing individual gets pushed inside the configured
    local-night window. Suggestions raised meanwhile aren't dropped, they're
    held and delivered as one digest when the window ends.
  * **Digesting** — a cycle that produces more than `digest_threshold`
    suggestions sends one summary push instead of a burst of individual ones.

Both exist for the same reason: an assistant that buzzes eleven times at 3am
gets muted, and a muted assistant is worth nothing.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from . import ntfy, telegram
from .. import runtime
from ..config import settings
from ..db import session_scope
from ..models import AppState, Reminder, Todo
from ..util import utcnow

log = logging.getLogger(__name__)

# app_state key holding the ids raised during quiet hours, awaiting delivery.
HELD_KEY = "notify_held_ids"
MAX_HELD = 200


def _channel(cfg: runtime.RuntimeConfig | None = None) -> str:
    return (cfg or runtime.current()).notify_channel


def notify_new_todo(todo_id: int, cfg: runtime.RuntimeConfig | None = None) -> None:
    ch = _channel(cfg)
    if ch == "ntfy":
        ntfy.notify_new_todo(todo_id)
    elif ch == "telegram":
        telegram.notify_new_todo(todo_id)


def notify_reminder(todo_id: int, channel: str | None = None) -> None:
    """Push a reminder. `channel` honours the one recorded when the reminder was
    queued, so switching channels doesn't retarget already-scheduled nudges."""
    ch = channel or _channel()
    if ch == "ntfy":
        ntfy.notify_reminder(todo_id)
    elif ch == "telegram":
        telegram.notify_reminder(todo_id)


def notify_text(title: str, message: str, cfg: runtime.RuntimeConfig | None = None) -> bool:
    """Push a plain-text message over the active channel. Returns whether a
    channel was configured to send it (so callers can tell the user)."""
    ch = _channel(cfg)
    if ch == "ntfy" and settings.ntfy_enabled:
        ntfy.send_text(title, message)
        return True
    if ch == "telegram" and settings.telegram_enabled:
        telegram.send_text(title, message)
        return True
    return False


def start_listener():
    """Only Telegram needs an inbound listener; ntfy buttons hit /api directly."""
    if _channel() == "telegram":
        return telegram.start_listener()
    return None


# ── held-during-quiet-hours queue ────────────────────────────────────────────
def _read_held(db) -> list[int]:
    row = db.get(AppState, HELD_KEY)
    if not row or not row.value:
        return []
    out = []
    for part in row.value.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _write_held(db, ids: list[int]) -> None:
    db.merge(AppState(key=HELD_KEY, value=",".join(str(i) for i in ids[-MAX_HELD:])))
    # Flush so a read-back in the same session sees it (autoflush is off here).
    db.flush()


def hold(todo_ids: list[int]) -> None:
    """Park suggestions raised during quiet hours for later delivery."""
    if not todo_ids:
        return
    with session_scope() as db:
        _write_held(db, _read_held(db) + list(todo_ids))


def flush_held() -> int:
    """Deliver anything held during quiet hours, once the window has passed.

    Runs on the same one-minute sweep as reminders. Returns how many were sent.
    """
    with session_scope() as db:
        cfg = runtime.load(db)
        if cfg.is_quiet_now():
            return 0
        held = _read_held(db)
        if not held:
            return 0
        # Only still-open items are worth waking someone for; anything they
        # already dealt with in the web UI overnight is dropped silently.
        rows = (
            db.execute(
                select(Todo)
                .where(Todo.id.in_(held), Todo.status.in_(("suggested", "confirmed")))
                .order_by(Todo.id.asc())
            )
            .scalars()
            .all()
        )
        titles = [t.title for t in rows]
        _write_held(db, [])

    if not titles:
        return 0
    _send_digest(titles, cfg, prefix="While you were away")
    return len(titles)


def _send_digest(titles: list[str], cfg: runtime.RuntimeConfig, *, prefix: str) -> None:
    head = f"🫖 {prefix}: {len(titles)} suggestion(s)"
    body = "\n".join(f"• {t[:120]}" for t in titles[:20])
    if len(titles) > 20:
        body += f"\n…and {len(titles) - 20} more"
    body += "\n\nOpen the dashboard to review."
    notify_text(head, body, cfg)


def dispatch(todo_ids: list[int]) -> None:
    """Deliver a classify pass's new suggestions, applying quiet hours + digest.

    This is the single entry point the classifier uses, so the policy lives in
    one place rather than being re-decided per channel.
    """
    if not todo_ids:
        return
    cfg = runtime.current()
    if cfg.notify_channel not in ("ntfy", "telegram"):
        return

    if cfg.is_quiet_now():
        hold(todo_ids)
        log.info("Quiet hours — holding %d suggestion(s) for later.", len(todo_ids))
        return

    if cfg.digest_threshold and len(todo_ids) > cfg.digest_threshold:
        with session_scope() as db:
            titles = [
                t.title
                for t in db.execute(
                    select(Todo).where(Todo.id.in_(todo_ids)).order_by(Todo.id.asc())
                ).scalars()
            ]
        if titles:
            _send_digest(titles, cfg, prefix="New activity")
        return

    for todo_id in todo_ids:
        notify_new_todo(todo_id, cfg)


def send_due_reminders() -> None:
    """Scheduler job: fire any reminders whose time has come (channel-agnostic).

    Semantics are at-most-once: we mark `sent=True` and commit before pushing, so
    a crash in the send window drops that one reminder rather than risking a
    duplicate on the next sweep. For a personal nudge that's the right trade — the
    to-do itself is never lost (it stays on the dashboard) and a due date still
    shows there; only the transient push can go missing.

    Reminders are also what wake a snoozed to-do back up, so this clears the
    snooze as it fires.
    """
    now = utcnow()
    to_send: list[tuple[int, str]] = []
    with session_scope() as db:
        cfg = runtime.load(db)
        quiet = cfg.is_quiet_now(now)
        rows = (
            db.query(Reminder)
            .filter(Reminder.sent.is_(False), Reminder.remind_at <= now)
            .all()
        )
        for r in rows:
            if quiet:
                continue  # leave queued; the next sweep after the window sends it
            todo = db.get(Todo, r.todo_id)
            # Skip reminders for to-dos the user already dealt with.
            if todo and todo.status in ("suggested", "confirmed"):
                to_send.append((r.todo_id, r.channel or cfg.notify_channel))
                # A snooze that has come due is over: let it show up again.
                if todo.snoozed_until is not None and todo.snoozed_until <= now:
                    todo.snoozed_until = None
            r.sent = True
    for todo_id, channel in to_send:
        notify_reminder(todo_id, channel)


__all__ = [
    "dispatch",
    "flush_held",
    "notify_new_todo",
    "notify_reminder",
    "notify_text",
    "send_due_reminders",
    "start_listener",
]
