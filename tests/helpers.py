"""Factories for the DB-backed tests. Shapes mirror what the poller stores."""
from datetime import datetime, timedelta, timezone

from app import models


def make_event(db, *, event_id=None, etype="chat", basecamp_id=1, project_id=100,
               chat_id=7, body="hello", who="Ana", who_id=2, when=None,
               processed=False, **payload_extra):
    """A RawEvent shaped like the ones the poller stores."""
    payload = {"content": body, "creator": {"id": who_id, "name": who}}
    if chat_id is not None:
        payload["_chat_id"] = chat_id
    payload.update(payload_extra)
    ev = models.RawEvent(
        id=event_id,
        type=etype,
        basecamp_id=basecamp_id,
        project_id=project_id,
        updated_at=when or datetime.now(timezone.utc),
        payload=payload,
        processed=processed,
    )
    db.add(ev)
    db.flush()
    return ev


def make_todo(db, *, title="A task", status="suggested", thread_key=None,
              created_at=None, updated_at=None, source_event_id=None,
              project_id=100, reason="ping", snoozed_until=None):
    todo = models.Todo(
        title=title,
        status=status,
        thread_key=thread_key,
        reason=reason,
        project_id=project_id,
        source_event_id=source_event_id,
        snoozed_until=snoozed_until,
        created_at=created_at or datetime.now(timezone.utc),
        updated_at=updated_at or created_at or datetime.now(timezone.utc),
    )
    db.add(todo)
    db.flush()
    return todo


def identity(db, user_id=99, name="Sam"):
    """Record who "me" is, which the classifier keys its rules off."""
    db.merge(models.AppState(key="my_user_id", value=str(user_id)))
    db.merge(models.AppState(key="my_name", value=name))
    db.flush()


def ago(hours=0, days=0):
    return datetime.now(timezone.utc) - timedelta(hours=hours, days=days)
