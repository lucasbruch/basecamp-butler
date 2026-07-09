"""Local LLM (v2) classifier via Ollama. Optional — enable with CLASSIFIER=ollama.

Kept intentionally small: it summarises a batch of new activity and decides,
per item, whether it warrants a to-do. The system prompt frames the model as a
producer/coordinator-level expert in VFX, full-CG commercial production and DOOH
so its language and judgement match the pipeline.
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
from .rules import _already_have_todo, _auto_add, _is_disabled  # reuse helpers

log = logging.getLogger(__name__)

# Sentinel: Ollama could not be reached (e.g. the GPU PC is asleep). We leave the
# event unprocessed so it's retried next cycle rather than silently dropped.
_UNREACHABLE = object()

_TAG_RE = re.compile(r"<[^>]+>")

SYSTEM_PROMPT = """\
You are a senior VFX/CG producer-coordinator assistant. You are fluent in the \
pipeline and vocabulary of visual effects, full-CG commercial production, and \
specialty Digital Out-of-Home (DOOH): shots, sequences, comp, render passes, \
lookdev, lighting, FX/sim, color grade, conform, client review rounds, \
revisions, deliverables and delivery specs, DOOH loops/specs/resolutions.

Given one item of Basecamp activity (a to-do, message, or comment), decide \
whether it implies an actionable to-do for the account owner. Respond ONLY with \
a compact JSON object:
  {"todo": true|false, "title": "<short imperative to-do>", "reason": "<why>"}
Use precise pipeline terminology. If it's just chatter/FYI, return todo=false.\
"""


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


def _ask_ollama(item_text: str):
    """Return the parsed verdict dict, None (bad response, skip item), or
    _UNREACHABLE (transport failure — retry the whole batch later)."""
    try:
        resp = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "system": SYSTEM_PROMPT,
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


def classify_events(db: Session) -> list[int]:
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
        if _is_disabled(db, event):
            event.processed = True
            continue
        if _already_have_todo(db, event):
            event.processed = True
            continue

        item_text = _summarise_event(event)
        verdict = _ask_ollama(item_text)
        if verdict is _UNREACHABLE:
            log.warning(
                "Ollama unreachable at %s — leaving remaining events for the next "
                "cycle (is the LLM host on?).",
                settings.ollama_url,
            )
            db.merge(AppState(key="llm_status", value="unreachable"))
            db.merge(AppState(key="llm_checked_at", value=utcnow().isoformat()))
            activity.record(
                db,
                "error",
                f"Local LLM ({settings.ollama_model}) unreachable — will retry the "
                "pending items next cycle. Is the LLM host awake?",
                detail=f"url={settings.ollama_url}",
            )
            break  # stop; don't mark the rest processed, so they retry later

        event.processed = True
        db.merge(AppState(key="llm_status", value="ok"))
        db.merge(AppState(key="llm_checked_at", value=utcnow().isoformat()))

        # Always log what we sent and what came back, so the /activity page shows
        # the LLM's reasoning — whether or not it produced a to-do.
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
                f"LLM read a {event.type} → suggests to-do: “{title}”{reason_tail}",
                detail=sent_detail,
            )
        else:
            activity.record(
                db,
                "llm",
                f"LLM read a {event.type} → no action (chatter/FYI){reason_tail}",
                detail=sent_detail,
            )

        if verdict and verdict.get("todo"):
            p = event.payload or {}
            status = "confirmed" if _auto_add(db, event.project_id) else "suggested"
            todo = Todo(
                source_event_id=event.id,
                project_id=event.project_id,
                title=(verdict.get("title") or "Suggested to-do")[:1000],
                notes=verdict.get("reason"),
                status=status,
                reason="ollama",
                source_url=p.get("app_url") or p.get("url"),
            )
            db.add(todo)
            db.flush()
            created.append(todo.id)

    db.flush()
    if created:
        log.info("Ollama classifier created %d suggestion(s).", len(created))
    return created
