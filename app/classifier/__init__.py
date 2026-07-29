"""Classifier dispatch: turn unprocessed raw events into suggested to-dos.

Pick the tier with the CLASSIFIER env var ("rules" | "ollama").
"""
from __future__ import annotations

import logging
import threading

from sqlalchemy import select

from .. import activity, notifier, runtime
from ..db import session_scope
from ..models import Todo
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
    with session_scope() as db:
        cfg = runtime.load(db)
        if cfg.classifier == "ollama":
            from . import ollama

            created = ollama.classify_events(db)
        else:
            created = rules.classify_events(db)

    if not created:
        return

    # Notify outside the DB transaction so a slow/failed push never blocks
    # commit. `dispatch` applies quiet hours and digesting for the whole batch.
    try:
        notifier.dispatch(created)
    except Exception:  # notifications are best-effort
        log.exception("Failed to dispatch alerts for %d todo(s)", len(created))
        with session_scope() as db:
            activity.record(
                db, "error", f"Failed to send a {cfg.notify_channel} alert."
            )
    else:
        if cfg.notify_channel in ("ntfy", "telegram"):
            with session_scope() as db:
                activity.record(
                    db,
                    "notify",
                    f"Sent {len(created)} {cfg.notify_channel} alert(s)."
                    + (" (held for quiet hours)" if cfg.is_quiet_now() else ""),
                )

    # Anything that landed already-confirmed (a project with auto-add on) can go
    # straight into Basecamp; the rest waits for the user to press ✅.
    if cfg.writeback_enabled:
        _writeback_confirmed(created)


def _writeback_confirmed(todo_ids: list[int]) -> None:
    from .. import writeback

    with session_scope() as db:
        auto = [
            t.id
            for t in db.execute(
                select(Todo).where(Todo.id.in_(todo_ids), Todo.status == "confirmed")
            ).scalars()
        ]
    for todo_id in auto:
        try:
            writeback.push(todo_id)
        except Exception:
            log.exception("Write-back failed for todo %s", todo_id)
