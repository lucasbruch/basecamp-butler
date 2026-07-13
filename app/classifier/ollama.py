"""Local LLM (v2) classifier via Ollama. Optional — enable with CLASSIFIER=ollama.

Kept intentionally small: it summarises a batch of new activity and decides,
per item, whether it warrants a to-do. By default the system prompt frames the
model as a generic helpful assistant; give it a character and topics on the
Settings page to tailor its judgement to your own work.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import activity
from ..config import settings
from ..models import AppState, RawEvent, Todo
from ..util import utcnow
from . import conversation
from .rules import (  # reuse helpers
    _already_have_todo,
    _auto_add,
    _is_disabled,
    _my_user_id,
)

log = logging.getLogger(__name__)

# Sentinel: Ollama could not be reached (e.g. the GPU PC is asleep). We leave the
# event unprocessed so it's retried next cycle rather than silently dropped.
_UNREACHABLE = object()

_TAG_RE = re.compile(r"<[^>]+>")

# The assistant's personality is configurable from Settings (stored in app_state).
# These are the defaults — a plain, general-purpose assistant — used until changed.
# Anything more specific is defined by the user via the persona fields on Settings.
DEFAULT_ROLE = "a helpful personal assistant"
DEFAULT_TOPICS = (
    "everyday work: projects, tasks, deadlines, documents and deliverables, "
    "meetings, requests, questions aimed at you, and follow-ups"
)

# Only the {role} and {topics} lines change; the JSON contract stays fixed so the
# classifier keeps working no matter what character the user picks.
_PROMPT_TEMPLATE = """\
You are {role}. You are fluent in {topics}.

Given one item of Basecamp activity (a to-do, message, comment, or a \
direct-message conversation shown as a transcript), decide whether it implies an \
actionable to-do for the account owner. When it's a transcript, read the whole \
exchange — the ask may build up across several lines — and judge it as a single \
request. Respond ONLY with a compact JSON object:
  {{"todo": true|false, "title": "<short imperative to-do>", "reason": "<why>"}}
Use precise, domain-appropriate terminology. If it's just chatter/FYI, return \
todo=false.\
"""


def _state(db: Session, key: str) -> str | None:
    row = db.get(AppState, key)
    return row.value if row else None


def compose_prompt(
    role: str | None, topics: str | None, override: str | None
) -> str:
    """Build the system prompt from the user's settings (or the defaults)."""
    if override and override.strip():
        return override.strip()
    return _PROMPT_TEMPLATE.format(
        role=(role or "").strip() or DEFAULT_ROLE,
        topics=(topics or "").strip() or DEFAULT_TOPICS,
    )


def build_system_prompt(db: Session) -> str:
    """The active system prompt, from stored settings."""
    return compose_prompt(
        _state(db, "llm_role"),
        _state(db, "llm_topics"),
        _state(db, "llm_prompt_override"),
    )


def _text(html: str | None) -> str:
    if not html:
        return ""
    return _TAG_RE.sub(" ", html).replace("&nbsp;", " ").strip()


def _summarise_event(event: RawEvent) -> str:
    p = event.payload or {}
    subject = _text(p.get("subject") or p.get("title") or "")
    # Pings put their text in content_excerpt and carry a sender in `creator`.
    content = _text(p.get("content") or p.get("content_excerpt") or "")
    sender = (p.get("creator") or {}).get("name", "")
    from_line = f"\nfrom={sender}" if sender else ""
    return f"type={event.type}{from_line}\nsubject={subject}\nbody={content}"[:4000]


def _ask_ollama(item_text: str, system_prompt: str):
    """Return the parsed verdict dict, None (bad response, skip item), or
    _UNREACHABLE (transport failure — retry the whole batch later)."""
    try:
        resp = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "system": system_prompt,
                "prompt": item_text,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1},
            },
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        return json.loads(raw)
    except httpx.RequestError:
        # Connection refused / timeout / DNS — the PC is likely off or asleep.
        return _UNREACHABLE
    except Exception:
        log.exception("Ollama gave an unusable response; skipping this item.")
        return None


def test_prompt(
    sample_text: str,
    *,
    role: str | None = None,
    topics: str | None = None,
    override: str | None = None,
) -> dict:
    """Run one made-up message through the (possibly unsaved) persona, for the
    Settings "Test it" button. Never raises — returns a display-ready dict."""
    prompt = compose_prompt(role, topics, override)
    item = f"type=message\nsubject=\nbody={_text(sample_text)[:4000]}"
    verdict = _ask_ollama(item, prompt)
    if verdict is _UNREACHABLE:
        return {"ok": False, "error": f"Couldn't reach the LLM at {settings.ollama_url}. Is the host awake?", "prompt": prompt}
    if not verdict:
        return {"ok": False, "error": "The model returned an unusable response.", "prompt": prompt}
    return {"ok": True, "verdict": verdict, "prompt": prompt, "model": settings.ollama_model}


def _mark_unreachable(db: Session) -> None:
    """Record an Ollama outage (edge-triggered) and leave the queue intact."""
    log.warning(
        "Ollama unreachable at %s — leaving the pending events queued; "
        "they'll be classified automatically once it's back.",
        settings.ollama_url,
    )
    was = _state(db, "llm_status")
    db.merge(AppState(key="llm_status", value="unreachable"))
    db.merge(AppState(key="llm_checked_at", value=utcnow().isoformat()))
    # Note the outage once, not on every retry — the sweep runs each minute.
    if was != "unreachable":
        activity.record(
            db,
            "error",
            f"Local LLM ({settings.ollama_model}) unreachable — pending items are "
            "queued and will be classified automatically once it's back. Is the "
            "LLM host awake?",
            detail=f"url={settings.ollama_url}",
        )


def _mark_ok(db: Session) -> None:
    """Record a healthy Ollama, announcing recovery once after an outage."""
    was = _state(db, "llm_status")
    db.merge(AppState(key="llm_status", value="ok"))
    db.merge(AppState(key="llm_checked_at", value=utcnow().isoformat()))
    if was == "unreachable":
        activity.record(
            db, "llm", "Local LLM reachable again — classifying the queued items."
        )


def _record_verdict(
    db: Session, source: RawEvent, item_text: str, verdict: dict, kind_label: str
) -> list[int]:
    """Log the LLM's decision for /activity and create a to-do if it said so.

    `source` is the event the to-do links back to (the newest line, for a ping
    conversation). Returns the created to-do ids (0 or 1)."""
    verdict_json = json.dumps(verdict, indent=2, ensure_ascii=False)
    sent_detail = (
        f"— Prompt sent to {settings.ollama_model} —\n{item_text}\n\n"
        f"— Decision —\n{verdict_json}"
    )
    reason = (verdict or {}).get("reason")
    reason_tail = f" — {reason}" if reason else ""
    if verdict and verdict.get("todo"):
        title = (verdict.get("title") or "Suggested to-do")[:1000]
        activity.record(
            db,
            "llm",
            f"LLM read {kind_label} → suggests to-do: “{title}”{reason_tail}",
            detail=sent_detail,
        )
        p = source.payload or {}
        status = "confirmed" if _auto_add(db, source.project_id) else "suggested"
        todo = Todo(
            source_event_id=source.id,
            project_id=source.project_id,
            title=title,
            notes=verdict.get("reason"),
            status=status,
            reason="ollama",
            source_url=p.get("app_url") or p.get("url"),
        )
        db.add(todo)
        db.flush()
        return [todo.id]

    activity.record(
        db,
        "llm",
        f"LLM read {kind_label} → no action (chatter/FYI){reason_tail}",
        detail=sent_detail,
    )
    return []


def _classify_ping_threads(
    db: Session, ping_events: list[RawEvent], my_id: int | None, system_prompt: str
) -> tuple[list[int], bool]:
    """Classify Pings a conversation at a time. Returns (created ids, reachable).

    A single ask often spans several pings, so we hand the model the whole thread
    as a transcript (with a little prior context) and let it judge the exchange as
    one request. Per "new to-do per burst", each thread's batch of new lines can
    raise its own suggestion. If Ollama goes unreachable mid-way we stop and leave
    the rest queued (reachable=False)."""
    created: list[int] = []
    for chat_id, group in conversation.group_by_thread(ping_events):
        newest = group[-1]
        ctx = conversation.prior_context(db, chat_id, group[0].id)
        transcript = conversation.render_transcript(group, my_id, ctx)
        item_text = f"type=ping (direct-message conversation)\n{transcript}"[:4000]

        verdict = _ask_ollama(item_text, system_prompt)
        if verdict is _UNREACHABLE:
            _mark_unreachable(db)
            return created, False

        _mark_ok(db)
        for ev in group:
            ev.processed = True
        created += _record_verdict(
            db, newest, item_text, verdict, f"a Ping conversation ({len(group)} msg)"
        )
    return created, True


def classify_events(db: Session) -> list[int]:
    system_prompt = build_system_prompt(db)  # the user's current persona
    my_id = _my_user_id(db)
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
    # Pings are classified per conversation (below); everything else per event.
    ping_events: list[RawEvent] = []
    for event in events:
        if event.type == "ping":
            ping_events.append(event)
            continue
        if _is_disabled(db, event):
            event.processed = True
            continue
        if _already_have_todo(db, event):
            event.processed = True
            continue

        item_text = _summarise_event(event)
        verdict = _ask_ollama(item_text, system_prompt)
        if verdict is _UNREACHABLE:
            _mark_unreachable(db)
            # Stop; don't mark the rest (incl. pings) processed, so they retry.
            db.flush()
            return created

        _mark_ok(db)
        event.processed = True
        created += _record_verdict(db, event, item_text, verdict, f"a {event.type}")

    # Pings run last; if the LLM drops out mid-thread the remaining groups stay
    # queued for the next sweep, so the reachable flag needs no further handling.
    ping_created, _reachable = _classify_ping_threads(
        db, ping_events, my_id, system_prompt
    )
    created += ping_created

    db.flush()
    if created:
        log.info("Ollama classifier created %d suggestion(s).", len(created))
    return created
