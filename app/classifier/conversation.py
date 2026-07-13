"""Group Ping (direct-message) events into conversations for the classifier.

Pings are ingested one RawEvent per chat line, but a single ask often spans
several lines ("hey" / "can you look at the storyboard" / "need it by Friday").
Judging each line alone loses that context — no line on its own reads as an
actionable request. So here we bucket a poll's new ping lines by thread and hand
the classifier the whole conversation at once.

The grouping/transcript helpers are pure (they take plain event objects), so they
can be unit-tested without a database. `prior_context` is the only DB-touching
helper and is best-effort.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import RawEvent

_TAG_RE = re.compile(r"<[^>]+>")

# How many already-seen lines of a thread to prepend as context.
CONTEXT_LINES = 8


def _text(html: str | None) -> str:
    if not html:
        return ""
    return _TAG_RE.sub(" ", html).replace("&nbsp;", " ").strip()


def _body(event) -> str:
    p = event.payload or {}
    return _text(p.get("content") or p.get("content_excerpt"))


def chat_id_of(event) -> int | None:
    return (event.payload or {}).get("_chat_id")


def group_by_thread(events: list) -> list[tuple[int | None, list]]:
    """Bucket ping events by their chat thread, preserving arrival order.

    `events` must already be chronological (updated_at asc); each returned group
    keeps that order. Groups come back in first-seen order so the result is
    deterministic. Returns a list of (chat_id, [events])."""
    groups: dict = {}
    for ev in events:
        groups.setdefault(chat_id_of(ev), []).append(ev)
    return list(groups.items())


def _speaker(event, my_id: int | None) -> str:
    creator = (event.payload or {}).get("creator") or {}
    name = (creator.get("name") or "Someone").strip()
    if my_id is not None and creator.get("id") == my_id:
        return f"{name} (you)"
    return name


def _render_line(event, my_id: int | None) -> str:
    body = _body(event)
    return f"{_speaker(event, my_id)}: {body}" if body else ""


def combined_text(events: list) -> str:
    """Every message body in a group joined, for keyword matching."""
    return " ".join(b for b in (_body(e) for e in events) if b).strip()


def latest_text(events: list) -> str:
    """The most recent non-empty message body — the punchiest to-do label."""
    for ev in reversed(events):
        body = _body(ev)
        if body:
            return body
    return ""


def render_transcript(new_events: list, my_id: int | None, context_events: list = ()) -> str:
    """A plain-text transcript of a thread: any prior context lines, a divider,
    then the new lines — each as 'Name: text'. Own lines are tagged '(you)' so
    the classifier can tell who is asking whom."""
    context = [ln for ln in (_render_line(e, my_id) for e in context_events) if ln]
    fresh = [ln for ln in (_render_line(e, my_id) for e in new_events) if ln]
    if not fresh and not context:
        return ""
    if context and fresh:
        return "\n".join([*context, "--- new messages ---", *fresh])
    return "\n".join([*context, *fresh])


def prior_context(db: Session, chat_id: int | None, before_id: int | None,
                  *, event_type: str = "ping", limit: int = CONTEXT_LINES) -> list:
    """Recent already-seen lines of a thread (oldest→newest) for context.

    Best-effort: returns [] on any error (e.g. a backend without JSONB path
    support in tests), so context is a bonus and never a failure mode."""
    if chat_id is None or before_id is None:
        return []
    try:
        rows = (
            db.execute(
                select(RawEvent)
                .where(
                    RawEvent.type == event_type,
                    RawEvent.id < before_id,
                    RawEvent.payload["_chat_id"].astext == str(chat_id),
                )
                .order_by(RawEvent.id.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
    except Exception:
        return []
    return list(reversed(rows))
