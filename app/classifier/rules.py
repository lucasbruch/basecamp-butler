"""Rule-based (v1) classifier. Deterministic, zero extra infra."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import activity
from ..config import settings
from ..models import AppState, Project, RawEvent, Reminder, Todo
from ..util import parse_bc_datetime, utcnow
from . import conversation
from .vocab import ACTION_SIGNALS, DOMAIN_TERMS, contains_any, matched_terms

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _text(html: str | None) -> str:
    if not html:
        return ""
    return _TAG_RE.sub(" ", html).replace("&nbsp;", " ").strip()


def _describe(event: RawEvent) -> str:
    """A short human label for an event, for the activity feed."""
    p = event.payload or {}
    sender = (p.get("creator") or {}).get("name")
    subject = _text(p.get("subject") or p.get("title") or "")
    label = subject or _text(p.get("content") or p.get("content_excerpt") or "")[:60]
    who = f" from {sender}" if sender else ""
    return f"{event.type}{who}" + (f" — “{label}”" if label else "")


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


def _is_disabled(db: Session, event: RawEvent) -> bool:
    """True if the event's project is toggled off in Settings — skip classifying it."""
    if event.project_id is None:
        return False
    proj = db.get(Project, event.project_id)
    return bool(proj and not proj.enabled)


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
    # Pings arrive as notification records whose text is in `content_excerpt`.
    body = _text(payload.get("content") or payload.get("content_excerpt") or "")
    full = f"{subject} {body}".strip()
    if not full:
        return []

    kind = {"message": "message", "chat": "chat", "comment": "comment"}.get(
        event.type, "comment"
    )
    label = subject or (body[:80] + "…" if len(body) > 80 else body)

    # Rule: it names me → strong signal I'm being addressed.
    if my_name and re.search(rf"\b{re.escape(my_name.split()[0])}\b", full, re.I):
        return [
            _make_todo(
                db, event, f"You were mentioned in a {kind}: {label}",
                "mention:by-name", notes=body[:2000],
            )
        ]

    # Rule: an action signal alongside a real "work" noun (document, ticket, …).
    if contains_any(full, ACTION_SIGNALS) and contains_any(full, DOMAIN_TERMS):
        terms = ", ".join(matched_terms(full, DOMAIN_TERMS)[:4])
        return [
            _make_todo(
                db, event, f"Possible task in a {kind}: {label}",
                f"keyword:{terms}", notes=body[:2000],
            )
        ]
    return []


def _classify_ping_threads(
    db: Session, ping_events: list[RawEvent], my_id: int | None
) -> list[int]:
    """Classify Pings a whole conversation at a time, not line by line.

    A single ask often spans several pings, so we bucket this poll's new lines by
    thread and match the combined text. That lets an action word in one line and
    its object in another finally count together. Per the "new to-do per burst"
    policy, each thread's batch of new lines can raise its own suggestion."""
    created: list[int] = []
    for _chat_id, group in conversation.group_by_thread(ping_events):
        try:
            newest = group[-1]
            full = conversation.combined_text(group)
            sender = (newest.payload.get("creator") or {}).get("name", "")
            who = f" from {sender}" if sender else ""
            new_ids: list[int] = []
            # Pings are aimed at you → higher signal: either gate is enough.
            if full and (contains_any(full, ACTION_SIGNALS) or contains_any(full, DOMAIN_TERMS)):
                latest = conversation.latest_text(group) or full
                label = latest[:80] + "…" if len(latest) > 80 else latest
                new_ids.append(
                    _make_todo(db, newest, f"Ping{who}: {label}", "ping", notes=full[:2000])
                )
            created += new_ids
            _log_rule_decision(
                db, newest, new_ids, kind=f"ping thread{who} ({len(group)} msg)"
            )
        finally:
            for ev in group:
                ev.processed = True
    return created


def _log_rule_decision(
    db: Session, event: RawEvent, new_ids: list[int], *, kind: str | None = None
) -> None:
    """Record what the rule classifier decided about one event, for /activity."""
    desc = kind or _describe(event)
    p = event.payload or {}
    url = p.get("app_url") or p.get("url")
    if new_ids:
        titles = ", ".join(
            t.title for t in (db.get(Todo, i) for i in new_ids) if t is not None
        )
        activity.record(db, "rule", f"Flagged {desc} → “{titles}”", url=url)
    else:
        activity.record(db, "rule", f"Looked at {desc} → no to-do (no rule matched).", url=url)


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
    # Pings are classified per conversation (below), not per line, so collect
    # them aside instead of judging each in the main per-event loop.
    ping_events: list[RawEvent] = []
    for event in events:
        if event.type == "ping":
            ping_events.append(event)
            continue
        try:
            if _is_disabled(db, event):
                continue
            if _already_have_todo(db, event):
                continue
            before = len(created)
            if event.type == "todo":
                created += _classify_todo(db, event, my_id)
            elif event.type in ("comment", "message", "chat"):
                created += _classify_comment_or_message(db, event, my_id, my_name)
            _log_rule_decision(db, event, created[before:])
        finally:
            event.processed = True

    created += _classify_ping_threads(db, ping_events, my_id)

    db.flush()
    if created:
        log.info("Rule classifier created %d suggestion(s).", len(created))
    return created
