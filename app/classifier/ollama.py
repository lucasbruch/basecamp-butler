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
from ..util import safe_url, utcnow
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

# Each LLM call is a blocking HTTP round-trip (up to 120s). Cap how many events a
# single sweep drains so a large backlog can't hold the classify lock for a very
# long time — the 1-min sweep just picks up where it left off next pass.
MAX_EVENTS_PER_PASS = 100

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

Given one item of Basecamp activity (a to-do, message, comment, or a chat \
conversation shown as a transcript), decide whether it implies an actionable \
to-do for the account owner. When it's a transcript, read the whole exchange — \
the ask may build up across several lines — and judge it as a single request. \
Group chat is noisier than a direct message, so only flag a clear ask or a \
message aimed at the owner. Respond ONLY with a compact JSON object:
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
            proxy=settings.ollama_proxy or None,
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
            source_url=safe_url(p.get("app_url") or p.get("url")),
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


def _classify_threads(
    db: Session,
    events: list[RawEvent],
    my_id: int | None,
    system_prompt: str,
    *,
    event_type: str,
    header: str,
    kind_label: str,
) -> tuple[list[int], bool]:
    """Classify a chat-style source a conversation at a time. Returns (created
    ids, reachable).

    A single ask often spans several messages, so we hand the model the whole
    thread as a transcript (with a little prior context) and let it judge the
    exchange as one request. Per "new to-do per burst", each thread's batch of new
    lines can raise its own suggestion. If Ollama goes unreachable mid-way we stop
    and leave the rest queued (reachable=False)."""
    created: list[int] = []
    for chat_id, group in conversation.group_by_thread(events):
        if _is_disabled(db, group[0]):  # room's project toggled off in Settings
            for ev in group:
                ev.processed = True
            continue
        # A burst of only our own lines isn't a request to us — skip it (but keep
        # our lines as tagged context when others are in the burst too).
        fresh = [e for e in group if not conversation.is_own(e, my_id)]
        if not fresh:
            for ev in group:
                ev.processed = True
            continue
        newest = fresh[-1]
        ctx = conversation.prior_context(db, chat_id, group[0].id, event_type=event_type)
        transcript = conversation.render_transcript(group, my_id, ctx)
        item_text = f"{header}\n{transcript}"[:4000]

        verdict = _ask_ollama(item_text, system_prompt)
        if verdict is _UNREACHABLE:
            _mark_unreachable(db)
            return created, False

        _mark_ok(db)
        for ev in group:
            ev.processed = True
        created += _record_verdict(
            db, newest, item_text, verdict, f"{kind_label} ({len(fresh)} msg)"
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
            .limit(MAX_EVENTS_PER_PASS)
        )
        .scalars()
        .all()
    )
    if len(events) == MAX_EVENTS_PER_PASS:
        log.info(
            "Classifying the oldest %d queued event(s) this pass; the rest drain "
            "on the next sweep.",
            MAX_EVENTS_PER_PASS,
        )

    created: list[int] = []
    # Pings and Campfire chat are classified per conversation (below); everything
    # else per event.
    ping_events: list[RawEvent] = []
    chat_events: list[RawEvent] = []
    for event in events:
        if event.type == "ping":
            ping_events.append(event)
            continue
        if event.type == "chat":
            chat_events.append(event)
            continue
        if _is_disabled(db, event):
            event.processed = True
            continue
        # Don't ask the LLM about our own outgoing posts.
        if event.type in ("comment", "message") and conversation.is_own(event, my_id):
            event.processed = True
            continue
        if _already_have_todo(db, event):
            event.processed = True
            continue

        item_text = _summarise_event(event)
        verdict = _ask_ollama(item_text, system_prompt)
        if verdict is _UNREACHABLE:
            _mark_unreachable(db)
            # Stop; leave the rest (incl. threads) unprocessed so they retry.
            db.flush()
            return created

        _mark_ok(db)
        event.processed = True
        created += _record_verdict(db, event, item_text, verdict, f"a {event.type}")

    # Conversations run last; if the LLM drops out mid-thread the remaining groups
    # stay queued for the next sweep. Skip chat if pings already hit an outage.
    ping_created, reachable = _classify_threads(
        db, ping_events, my_id, system_prompt,
        event_type="ping",
        header="type=ping (direct-message conversation)",
        kind_label="a Ping conversation",
    )
    created += ping_created
    if reachable:
        chat_created, _reachable = _classify_threads(
            db, chat_events, my_id, system_prompt,
            event_type="chat",
            header="type=chat (Campfire group chat)",
            kind_label="a Campfire conversation",
        )
        created += chat_created

    db.flush()
    if created:
        log.info("Ollama classifier created %d suggestion(s).", len(created))
    return created
