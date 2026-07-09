"""Rule-based (v1) classifier. Deterministic, zero extra infra."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AppState, Project, RawEvent, Reminder, Todo
from ..util import parse_bc_datetime, utcnow
from .vocab import ACTION_SIGNALS, DOMAIN_TERMS, contains_any, matched_terms

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _text(html: str | None) -> str:
    if not html:
        return ""
    return _TAG_RE.sub(" ", html).replace("&nbsp;", " ").strip()


def _my_user_id(db: Session) -> int | None:
    row = db.get(AppState, "my_user_id")
    if row and row.value and row.value.isdigit():
        return int(row.value)
    return None


def _my_name(db: Session) -> str:
    row = db.get(AppState, "my_name")
    return (row.value or "").strip() if row else ""


def _auto_add(db: Session, project_id: int | None) -> bool:
    if project_id is None:
        return False
    proj = db.get(Project, project_id)
    return bool(proj and proj.auto_add)


def _already_have_todo(db: Session, event: RawEvent) -> bool:
    """True if we've already raised a to-do for this Basecamp recording.

    Keyed on the Basecamp id (not our internal event id) so a re-updated
    recording — which lands as a fresh raw_event — doesn't spawn a duplicate.
    """
    stmt = (
        select(Todo.id)
        .join(RawEvent, Todo.source_event_id == RawEvent.id)
        .where(RawEvent.type == event.type, RawEvent.basecamp_id == event.basecamp_id)
    )
    return db.execute(stmt).first() is not None


def _make_todo(
    db: Session,
    event: RawEvent,
    title: str,
    reason: str,
    *,
    notes: str | None = None,
    due_date=None,
) -> int:
    status = "confirmed" if _auto_add(db, event.project_id) else "suggested"
    payload = event.payload or {}
    todo = Todo(
        source_event_id=event.id,
        project_id=event.project_id,
        title=title[:1000],
        notes=notes,
        status=status,
        reason=reason,
        due_date=due_date,
        source_url=payload.get("app_url") or payload.get("url"),
    )
    db.add(todo)
    db.flush()

    # If it carries a real due date, seed a reminder for the day before (>= now).
    if due_date is not None:
        remind_at = max(due_date - timedelta(days=1), utcnow())
        db.add(Reminder(todo_id=todo.id, remind_at=remind_at, channel="telegram"))
    return todo.id


def _classify_todo(db: Session, event: RawEvent, my_id: int | None) -> list[int]:
    payload = event.payload or {}
    if payload.get("completed"):
        return []
    title = _text(payload.get("content") or payload.get("title") or "To-do")
    assignees = payload.get("assignees") or []
    assignee_ids = {a.get("id") for a in assignees}
    due = parse_bc_datetime(payload.get("due_on"))

    created: list[int] = []

    # Rule: a to-do assigned to me.
    if my_id is not None and my_id in assignee_ids:
        created.append(
            _make_todo(db, event, f"Assigned to you: {title}", "todo:assigned-to-me", due_date=due)
        )
        return created

    # Rule: due soon and unassigned (nobody's clearly on the hook).
    if due is not None and not assignee_ids:
        within = utcnow() + timedelta(days=settings.due_soon_days)
        if due <= within:
            created.append(
                _make_todo(
                    db, event, f"Due soon / unassigned: {title}",
                    "todo:due-soon-unassigned", due_date=due,
                )
            )
    return created


def _classify_comment_or_message(
    db: Session, event: RawEvent, my_id: int | None, my_name: str
) -> list[int]:
    payload = event.payload or {}
    subject = _text(payload.get("subject") or payload.get("title") or "")
    body = _text(payload.get("content") or "")
    full = f"{subject} {body}".strip()
    if not full:
        return []

    kind = "message" if event.type == "message" else "comment"
    label = subject or (body[:80] + "…" if len(body) > 80 else body)

    # Rule: it names me → strong signal I'm being addressed.
    if my_name and re.search(rf"\b{re.escape(my_name.split()[0])}\b", full, re.I):
        return [
            _make_todo(
                db, event, f"You were mentioned in a {kind}: {label}",
                "mention:by-name", notes=body[:2000],
            )
        ]

    # Rule: an action signal alongside real pipeline vocabulary.
    if contains_any(full, ACTION_SIGNALS) and contains_any(full, DOMAIN_TERMS):
        terms = ", ".join(matched_terms(full, DOMAIN_TERMS)[:4])
        return [
            _make_todo(
                db, event, f"Possible task in a {kind}: {label}",
                f"keyword:{terms}", notes=body[:2000],
            )
        ]
    return []


def classify_events(db: Session) -> list[int]:
    """Process all unprocessed raw events; return ids of created to-dos."""
    my_id = _my_user_id(db)
    my_name = _my_name(db)

    events = (
        db.execute(
            select(RawEvent)
            .where(RawEvent.processed.is_(False))
            .order_by(RawEvent.updated_at.asc())
        )
        .scalars()
        .all()
    )

    created: list[int] = []
    for event in events:
        try:
            if not _already_have_todo(db, event):
                if event.type == "todo":
                    created += _classify_todo(db, event, my_id)
                elif event.type in ("comment", "message"):
                    created += _classify_comment_or_message(db, event, my_id, my_name)
        finally:
            event.processed = True

    db.flush()
    if created:
        log.info("Rule classifier created %d suggestion(s).", len(created))
    return created
