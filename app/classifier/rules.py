"""Rule-based (v1) classifier. Deterministic, zero extra infra."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import activity, runtime
from ..models import AppState, MutedSender, Project, RawEvent, Reminder, Todo
from ..runtime import RuntimeConfig
from ..util import parse_bc_datetime, safe_url, utcnow
from . import conversation
from .vocab import (
    ACTION_SIGNALS,
    DOMAIN_TERMS,
    contains_any,
    matched_terms,
    mentions_name,
)

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


def muted_senders(db: Session) -> set[str]:
    """Lower-cased names the user has muted, read once per classify pass."""
    return {
        (name or "").strip().lower()
        for name in db.execute(select(MutedSender.name)).scalars()
        if (name or "").strip()
    }


def _is_muted(event: RawEvent, muted: set[str]) -> bool:
    """True if the event's author is on the mute list.

    Aimed at the deploy bot that posts to Campfire all day and the colleague
    whose stream is pure chatter — both otherwise trip the keyword gate often
    enough to be the main source of noise."""
    if not muted:
        return False
    creator = (event.payload or {}).get("creator") or {}
    return (creator.get("name") or "").strip().lower() in muted


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


def thread_key_of(event: RawEvent) -> str | None:
    """The chat thread an event belongs to (Campfire room / Ping conversation)."""
    chat_id = (event.payload or {}).get("_chat_id")
    return str(chat_id) if chat_id is not None else None


def thread_has_open_todo(db: Session, thread_key: str | None, within_hours: int) -> bool:
    """True if this thread already has an unresolved suggestion from recently.

    A conversation arrives as a trickle of lines, so each poll sees a new burst
    in the *same* thread and used to raise its own suggestion — three messages
    over fifteen minutes became three near-identical to-dos. (The LLM path was
    worse: `prior_context` replays the earlier lines, so the model re-read the
    same ask and re-flagged it every time.)

    Suppression is scoped to *open* items — once you've dismissed or completed
    the thread's to-do, a genuinely new ask in that thread can raise a fresh
    one. The time bound stops a long-forgotten confirmed to-do from muting a
    thread permanently.
    """
    if not thread_key or within_hours <= 0:
        return False
    since = utcnow() - timedelta(hours=within_hours)
    stmt = (
        select(Todo.id)
        .where(
            Todo.thread_key == thread_key,
            Todo.status.in_(("suggested", "confirmed")),
            Todo.created_at >= since,
        )
        .limit(1)
    )
    return db.execute(stmt).first() is not None


def _make_todo(
    db: Session,
    event: RawEvent,
    title: str,
    reason: str,
    cfg: RuntimeConfig,
    *,
    notes: str | None = None,
    due_date=None,
    due_all_day: bool = False,
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
        due_all_day=due_all_day,
        source_url=safe_url(payload.get("app_url") or payload.get("url")),
        thread_key=thread_key_of(event),
    )
    db.add(todo)
    db.flush()

    # If it carries a real due date, seed a reminder for the day before (>= now).
    # The sweep honours the channel recorded here, so a channel switch doesn't
    # retarget reminders that were queued under the old one.
    if due_date is not None:
        remind_at = max(due_date - timedelta(days=1), utcnow())
        db.add(
            Reminder(
                todo_id=todo.id, remind_at=remind_at, channel=cfg.notify_channel
            )
        )
    return todo.id


def _classify_todo(
    db: Session, event: RawEvent, my_id: int | None, cfg: RuntimeConfig
) -> list[int]:
    payload = event.payload or {}
    if payload.get("completed"):
        return []
    title = _text(payload.get("content") or payload.get("title") or "To-do")
    assignees = payload.get("assignees") or []
    assignee_ids = {a.get("id") for a in assignees}
    # `due_on` is a bare date — it names a day, so it's flagged all-day and never
    # gets shifted into a display zone.
    due = parse_bc_datetime(payload.get("due_on"))

    created: list[int] = []

    # Rule: a to-do assigned to me.
    if my_id is not None and my_id in assignee_ids:
        created.append(
            _make_todo(
                db, event, f"Assigned to you: {title}", "todo:assigned-to-me", cfg,
                due_date=due, due_all_day=True,
            )
        )
        return created

    # Rule: due soon and unassigned (nobody's clearly on the hook).
    if due is not None and not assignee_ids:
        within = utcnow() + timedelta(days=cfg.due_soon_days)
        if due <= within:
            created.append(
                _make_todo(
                    db, event, f"Due soon / unassigned: {title}",
                    "todo:due-soon-unassigned", cfg, due_date=due, due_all_day=True,
                )
            )
    return created


def _classify_comment_or_message(
    db: Session, event: RawEvent, my_id: int | None, my_name: str, cfg: RuntimeConfig
) -> list[int]:
    # Don't flag our own outgoing posts as to-dos for ourselves.
    if conversation.is_own(event, my_id):
        return []

    payload = event.payload or {}
    subject = _text(payload.get("subject") or payload.get("title") or "")
    # Pings arrive as notification records whose text is in `content_excerpt`.
    body = _text(payload.get("content") or payload.get("content_excerpt") or "")
    full = f"{subject} {body}".strip()
    if not full:
        return []

    kind = {"message": "message", "comment": "comment"}.get(event.type, "comment")
    label = subject or (body[:80] + "…" if len(body) > 80 else body)

    # Rule: it names me → strong signal I'm being addressed.
    if mentions_name(full, my_name):
        return [
            _make_todo(
                db, event, f"You were mentioned in a {kind}: {label}",
                "mention:by-name", cfg, notes=body[:2000],
            )
        ]

    # Rule: an action signal alongside a real "work" noun (document, ticket, …).
    if contains_any(full, ACTION_SIGNALS) and contains_any(full, DOMAIN_TERMS):
        terms = ", ".join(matched_terms(full, DOMAIN_TERMS)[:4])
        return [
            _make_todo(
                db, event, f"Possible task in a {kind}: {label}",
                f"keyword:{terms}", cfg, notes=body[:2000],
            )
        ]
    return []


def _classify_shared_item(
    db: Session, event: RawEvent, my_id: int | None, my_name: str, cfg: RuntimeConfig
) -> list[int]:
    """Schedule entries (meetings), documents and uploads.

    These are lower-signal than a direct message — most of what lands in a
    project's Docs & Files is FYI. So the bar is: a meeting you're actually a
    participant in, or an item that names you / reads as a request.
    """
    if conversation.is_own(event, my_id):
        return []

    payload = event.payload or {}
    title = _text(payload.get("title") or payload.get("summary") or payload.get("filename") or "")
    body = _text(payload.get("description") or payload.get("content") or "")
    full = f"{title} {body}".strip()

    if event.type == "schedule":
        starts = parse_bc_datetime(payload.get("starts_at"))
        participants = payload.get("participants") or []
        # A meeting you're invited to is the one calendar item worth surfacing.
        if my_id is not None and my_id in {p.get("id") for p in participants}:
            # The title is baked at classify time and shown everywhere as-is, so
            # it carries the local wall-clock — "22:00 UTC" is not what anyone
            # wants to read on a meeting reminder.
            local = starts.astimezone(cfg.tz) if starts else None
            when = f" ({local:%Y-%m-%d %H:%M %Z})" if local else ""
            return [
                _make_todo(
                    db, event, f"Meeting: {title or 'untitled'}{when}",
                    "schedule:you-are-a-participant", cfg,
                    notes=body[:2000] or None, due_date=starts,
                )
            ]
        return []

    kind = "document" if event.type == "document" else "file"
    if not full:
        return []
    if mentions_name(full, my_name):
        return [
            _make_todo(
                db, event, f"You were named on a {kind}: {title or full[:80]}",
                "mention:by-name", cfg, notes=body[:2000] or None,
            )
        ]
    if contains_any(full, ACTION_SIGNALS) and contains_any(full, DOMAIN_TERMS):
        terms = ", ".join(matched_terms(full, DOMAIN_TERMS)[:4])
        return [
            _make_todo(
                db, event, f"Possible task on a {kind}: {title or full[:80]}",
                f"keyword:{terms}", cfg, notes=body[:2000] or None,
            )
        ]
    return []


def _ping_verdict(
    full: str, label: str, who: str, my_id: int | None, my_name: str
) -> tuple[str, str] | None:
    """A Ping (direct message) is aimed at you → higher signal: either gate is
    enough. Returns (title, reason) or None.

    Takes the same arguments as `_chat_verdict` (which does use the identity
    fields) so both can be passed as `decide` to `_classify_threads`."""
    if contains_any(full, ACTION_SIGNALS) or contains_any(full, DOMAIN_TERMS):
        return (f"Ping{who}: {label}", "ping")
    return None


def _chat_verdict(
    full: str, label: str, who: str, my_id: int | None, my_name: str
) -> tuple[str, str] | None:
    """A Campfire message is group chatter → needs a stronger signal: your name,
    or an action word alongside a real work noun. Returns (title, reason) or None."""
    if mentions_name(full, my_name):
        return (f"You were mentioned in a chat: {label}", "mention:by-name")
    if contains_any(full, ACTION_SIGNALS) and contains_any(full, DOMAIN_TERMS):
        terms = ", ".join(matched_terms(full, DOMAIN_TERMS)[:4])
        return (f"Possible task in a chat: {label}", f"keyword:{terms}")
    return None


def _classify_threads(
    db: Session,
    events: list[RawEvent],
    my_id: int | None,
    my_name: str,
    cfg: RuntimeConfig,
    muted: set[str],
    *,
    kind_word: str,
    decide,
) -> list[int]:
    """Classify a chat-style source a whole conversation at a time, not line by
    line. A single ask often spans several messages, so we bucket this poll's new
    lines by thread and judge the combined text — an action word in one line and
    its object in another finally count together.

    A thread that already has an open suggestion is left alone (see
    `thread_has_open_todo`), so an ongoing conversation produces one to-do rather
    than one per poll cycle.

    `decide(full, label, who, my_id, my_name)` returns (title, reason) or None."""
    created: list[int] = []
    for _chat_id, group in conversation.group_by_thread(events):
        try:
            if _is_disabled(db, group[0]):  # room's project toggled off in Settings
                continue
            # Judge only what other people said — our own lines shouldn't raise a
            # to-do aimed at ourselves — and drop anyone the user muted.
            fresh = [
                e
                for e in group
                if not conversation.is_own(e, my_id) and not _is_muted(e, muted)
            ]
            if not fresh:
                _log_rule_decision(
                    db, group[-1], [], kind=f"{kind_word} thread (nothing to judge)"
                )
                continue
            newest = fresh[-1]
            key = thread_key_of(newest)
            if thread_has_open_todo(db, key, cfg.thread_coalesce_hours):
                _log_rule_decision(
                    db, newest, [],
                    kind=f"{kind_word} thread (already has an open to-do)",
                )
                continue
            full = conversation.combined_text(fresh)
            sender = (newest.payload.get("creator") or {}).get("name", "")
            who = f" from {sender}" if sender else ""
            latest = conversation.latest_text(fresh) or full
            label = latest[:80] + "…" if len(latest) > 80 else latest
            new_ids: list[int] = []
            verdict = decide(full, label, who, my_id, my_name) if full else None
            if verdict:
                title, reason = verdict
                new_ids.append(
                    _make_todo(db, newest, title, reason, cfg, notes=full[:2000])
                )
            created += new_ids
            _log_rule_decision(
                db, newest, new_ids, kind=f"{kind_word} thread{who} ({len(fresh)} msg)"
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
    url = safe_url(p.get("app_url") or p.get("url"))
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
    cfg = runtime.load(db)
    muted = muted_senders(db)

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
    # Pings and Campfire chat are classified per conversation (below), not per
    # line, so collect them aside instead of judging each in the per-event loop.
    ping_events: list[RawEvent] = []
    chat_events: list[RawEvent] = []
    for event in events:
        if event.type == "ping":
            ping_events.append(event)
            continue
        if event.type == "chat":
            chat_events.append(event)
            continue
        try:
            if _is_disabled(db, event):
                continue
            if _is_muted(event, muted):
                continue
            if _already_have_todo(db, event):
                continue
            before = len(created)
            if event.type == "todo":
                created += _classify_todo(db, event, my_id, cfg)
            elif event.type in ("comment", "message"):
                created += _classify_comment_or_message(db, event, my_id, my_name, cfg)
            elif event.type in ("schedule", "document", "upload"):
                created += _classify_shared_item(db, event, my_id, my_name, cfg)
            _log_rule_decision(db, event, created[before:])
        finally:
            event.processed = True

    created += _classify_threads(
        db, ping_events, my_id, my_name, cfg, muted,
        kind_word="ping", decide=_ping_verdict,
    )
    created += _classify_threads(
        db, chat_events, my_id, my_name, cfg, muted,
        kind_word="campfire", decide=_chat_verdict,
    )

    db.flush()
    if created:
        log.info("Rule classifier created %d suggestion(s).", len(created))
    return created
